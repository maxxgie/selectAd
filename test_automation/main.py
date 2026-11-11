"""Command line interface for signup automation."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

# Ensure the project root is importable when run as `python main.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from database.sqlite_manager import SQLiteManager  # noqa: E402
from exporters.csv_exporter import CSVExporter  # noqa: E402
from exporters.json_exporter import JSONExporter  # noqa: E402
from generators.user_generator import UserGenerator  # noqa: E402
from testing.signup_client import SignupClient  # noqa: E402
from utils.config import load_config, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)

# Global flag for graceful shutdown
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("Shutdown requested (signal %d); aborting current request...", signum)
    # Raise KeyboardInterrupt so any in-flight blocking call (network I/O, retry
    # backoff, proxy-validation thread pool, or the proxy reload loop) unwinds
    # immediately instead of waiting for the call's own timeout to elapse.
    # On Windows the real interrupt is CTRL_BREAK_EVENT (SIGBREAK); treat both
    # SIGINT and SIGBREAK as immediate aborts.
    if signum in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGINT)):
        raise KeyboardInterrupt


signal.signal(signal.SIGINT, _signal_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _signal_handler)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _signal_handler)


def cmd_generate(args: argparse.Namespace, cfg) -> int:
    count = args.count or cfg.users_to_generate
    generator = UserGenerator(country=cfg.country, default_password=cfg.default_password, username_length=cfg.username_length)
    users = generator.generate(count)

    with SQLiteManager(cfg.db_path) as db:
        inserted = db.insert_users(users)
    logger.info("Generated and stored %d users", inserted)

    CSVExporter(cfg.csv_path).export(users)
    JSONExporter(cfg.json_path).export(users)
    logger.info("Exported users to %s and %s", cfg.csv_path, cfg.json_path)
    return 0


def cmd_submit(args: argparse.Namespace, cfg) -> int:
    count = args.limit or cfg.users_to_generate

    # Delete old DB contents and start fresh
    with SQLiteManager(cfg.db_path) as db:
        db.delete_all_users()

    generator = UserGenerator(country=cfg.country, default_password=cfg.default_password, username_length=cfg.username_length)
    users = generator.generate(count)

    with SQLiteManager(cfg.db_path) as db:
        inserted = db.insert_users(users)
    logger.info("Generated %d users for submission", inserted)

    CSVExporter(cfg.csv_path).export(users)
    JSONExporter(cfg.json_path).export(users)
    logger.info("Exported users to %s and %s", cfg.csv_path, cfg.json_path)

    if not args.yes:
        print(f"About to submit {len(users)} registration(s) to: {cfg.api_url}")
        print(f"Sponsor: {cfg.sponsor or '(none)'}  Country: {cfg.country}")
        confirm = input("Type YES to continue: ").strip()
        if confirm != "YES":
            print("Cancelled.")
            return 1

    client = SignupClient(
        api_url=cfg.api_url,
        allow_remote=cfg.allow_remote_signup,
        sponsor=cfg.sponsor,
        delay_seconds=cfg.request_delay_seconds,
        max_retries=cfg.request_max_retries,
        batch_size=cfg.batch_size,
        batch_delay_seconds=cfg.batch_delay_seconds,
        network_reset_command=cfg.network_reset_command,
        network_reset_wait_seconds=cfg.network_reset_wait_seconds,
        source_url=cfg.source_url,
        db_path=cfg.db_path,
        proxies=cfg.proxies,
        verify_ssl=cfg.verify_ssl,
        country=cfg.country,
        default_password=cfg.default_password,
        username_length=cfg.username_length,
        proxy_check_timeout=getattr(cfg, "proxy_check_timeout", 5),
        proxy_check_url=getattr(cfg, "proxy_check_url", "https://api.ipify.org"),
        max_workers_proxy_check=getattr(cfg, "max_workers_proxy_check", 20),
        auto_validate_proxies=getattr(cfg, "auto_validate_proxies", True),
        persist_valid_proxies=getattr(cfg, "persist_valid_proxies", True),
    )
    # Set proxies file path for persistence
    proxies_path = getattr(cfg, "proxies_path", None)
    if proxies_path:
        client.set_proxies_file_path(Path(proxies_path) if not Path(proxies_path).is_absolute()
                                      else Path(proxies_path))

    results: list = []
    pre = None
    try:
        # Run submissions with graceful shutdown support.
        # The signal handler raises KeyboardInterrupt on SIGINT, so any
        # in-flight blocking call (network I/O, retry backoff, proxy-validation
        # thread pool, or the proxy reload loop) unwinds immediately instead
        # of waiting for the call's own timeout to elapse.
        if getattr(cfg, "preflight_before_submit", True):
            pre = client.preflight()
            logger.info("Preflight: %s", pre)

        for i, user in enumerate(users):
            if _shutdown_requested:
                logger.warning("Shutdown requested; stopping after %d submissions", i)
                break
            results.append(client.signup(user))
            if i < len(users) - 1:
                if cfg.batch_size and (i + 1) % cfg.batch_size == 0:
                    time.sleep(cfg.batch_delay_seconds)
                elif cfg.request_delay_seconds:
                    time.sleep(cfg.request_delay_seconds)
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received; stopping submissions after %d result(s)", len(results))
    finally:
        try:
            summary = client.summary(results)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to summarize results: %s", exc)
            summary = {"total": len(results), "successful": 0, "failed": len(results),
                       "successful_users": [], "failed_users": [], "errors": [str(exc)]}
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to close client: %s", exc)

        # Always write partial results so progress is not lost on shutdown.
        out = Path(cfg.results_path)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "summary": summary,
                "preflight": pre,
                "results": [
                    {
                        "username": r.user.username,
                        "email": r.user.email,
                        "phone": r.user.phone,
                        "country": r.user.country,
                        "success": r.success,
                        "status_code": r.status_code,
                        "response": r.response,
                        "error_message": r.error_message,
                    }
                    for r in results
                ],
            }
            out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write results file: %s", exc)

        logger.info(
            "Submission summary: %d total, %d successful, %d failed (details: %s)",
            summary["total"],
            summary["successful"],
            summary["failed"],
            out,
        )
    return 0 if summary["failed"] == 0 else 1


# Backward-compatible command name.
def cmd_test_signup(args: argparse.Namespace, cfg) -> int:
    return cmd_submit(args, cfg)


def cmd_export(args: argparse.Namespace, cfg) -> int:
    fmt = args.format.lower()
    with SQLiteManager(cfg.db_path) as db:
        users = db.get_all_users()
    if not users:
        logger.error("No users found in database. Run `generate` first.")
        return 1

    if fmt == "csv":
        path = CSVExporter(cfg.csv_path).export(users)
    elif fmt == "json":
        path = JSONExporter(cfg.json_path).export(users)
    else:
        logger.error("Unsupported format: %s (use csv or json)", fmt)
        return 1
    logger.info("Exported %d users to %s", len(users), path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Signup automation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="Generate user records")
    p_gen.add_argument(
        "--count", type=int, default=None, help="Number of users to generate"
    )
    p_gen.set_defaults(func=cmd_generate)

    p_sub = sub.add_parser("submit", help="Submit stored registrations to the configured endpoint")
    p_sub.add_argument("--limit", type=int, default=None, help="Submit only the first N users")
    p_sub.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    p_sub.set_defaults(func=cmd_submit)

    p_test = sub.add_parser(
        "test-signup", help="Alias for submit (backward compatible)"
    )
    p_test.add_argument("--limit", type=int, default=None)
    p_test.add_argument("--yes", action="store_true")
    p_test.set_defaults(func=cmd_test_signup)

    p_exp = sub.add_parser("export", help="Export stored users")
    p_exp.add_argument(
        "--format", choices=["csv", "json"], default="csv", help="Export format"
    )
    p_exp.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    setup_logging(cfg.log_level)
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())

