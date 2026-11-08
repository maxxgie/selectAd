"""Command line interface for signup automation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
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


def cmd_generate(args: argparse.Namespace, cfg) -> int:
    count = args.count or cfg.users_to_generate
    generator = UserGenerator(country=cfg.country, default_password=cfg.default_password)
    users = generator.generate(count)

    with SQLiteManager(cfg.db_path) as db:
        inserted = db.insert_users(users)
    logger.info("Generated and stored %d users", inserted)

    CSVExporter(cfg.csv_path).export(users)
    JSONExporter(cfg.json_path).export(users)
    logger.info("Exported users to %s and %s", cfg.csv_path, cfg.json_path)
    return 0


def cmd_submit(args: argparse.Namespace, cfg) -> int:
    limit = args.limit
    with SQLiteManager(cfg.db_path) as db:
        users = db.get_all_users()
    if not users:
        logger.error("No users found in database. Run `generate` first.")
        return 1
    if limit is not None:
        users = users[: max(0, int(limit))]

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
    )
    try:
        if getattr(cfg, "preflight_before_submit", True):
            pre = client.preflight()
            logger.info("Preflight: %s", pre)
            if not pre.get("api_reachable"):
                logger.error("Preflight failed: AdSelect endpoint is not reachable from this network.")
                return 1
        results = client.run(users)
        summary = client.summary(results)
    finally:
        client.close()

    out = Path(cfg.results_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summary,
        "preflight": pre if 'pre' in locals() else None,
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
