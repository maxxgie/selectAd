"""Application configuration loader.

Reads settings from ``config.json`` and environment variables. Environment
variables take precedence over the JSON file so deployments can override
values without touching the file on disk.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional but recommended

    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_PROXIES_PATH = Path(__file__).resolve().parents[1] / "config/proxies.txt"


@dataclass
class Config:
    """Runtime configuration."""

    country: str = "United States"
    default_password: str = "12345678"
    users_to_generate: int = 100
    environment: str = "production"
    api_url: str = "https://adselect.net/api/auth/register"
    allow_remote_signup: bool = True
    sponsor: str = "ASD5437949"
    source_url: str = "https://adselect.net/register?ref=ASD5437949"
    person_config_path: str = "config/person.json"
    output_dir: str = "output"
    log_level: str = "INFO"
    db_path: str = "output/test_users.db"
    csv_path: str = "output/test_users.csv"
    json_path: str = "output/test_users.json"
    results_path: str = "output/signup_results.json"
    request_delay_seconds: float = 1.0
    request_max_retries: int = 2
    batch_size: int = 0
    batch_delay_seconds: float = 0.0
    network_reset_command: str = ""
    network_reset_wait_seconds: float = 10.0
    preflight_before_submit: bool = True
    username_length: int = 8
    proxies: list[str] | None = None
    verify_ssl: bool = True
    proxy_check_timeout: int = 5
    proxy_check_url: str = "https://api.ipify.org"
    max_workers_proxy_check: int = 20
    auto_validate_proxies: bool = True
    persist_valid_proxies: bool = True


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_proxies(proxies_path: str | Path | None, config_base: Path | None = None) -> list[str] | None:
    """Load proxy list from a file (one per line). Returns None if not found."""
    if proxies_path is None:
        return None
    path = Path(proxies_path)
    if not path.is_absolute() and config_base is not None:
        path = config_base / path
    if not path.exists():
        logger.warning("Proxies file %s not found; no proxies loaded", path)
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
        logger.info("Loaded %d proxies from %s", len(lines), path)
        return lines
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load proxies from %s: %s", path, exc)
        return None


def load_config(config_path: str | Path | None = None) -> Config:
    """Load configuration from config.json and environment variables."""
    load_dotenv(ENV_FILE)

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        logger.info("Loaded configuration from %s", path)
    else:
        logger.warning("Config file %s not found; using defaults", path)

    # Load person/sponsor config (editable file for targeting different people)
    person_data: dict[str, Any] = {}
    person_path = Path(data.get("person_config_path", "config/person.json"))
    if not person_path.is_absolute():
        person_path = path.parent / person_path
    if person_path.exists():
        with person_path.open("r", encoding="utf-8") as fh:
            person_data = json.load(fh)
        logger.info("Loaded person config from %s", person_path)
    else:
        logger.warning("Person config %s not found; using config.json defaults", person_path)

    # Load proxies from config file or default location
    proxies = _load_proxies(data.get("proxies_path"), path.parent)

    return Config(
        country=os.environ.get("COUNTRY", person_data.get("country", data.get("country", "United States"))),
        default_password=os.environ.get(
            "DEFAULT_PASSWORD", person_data.get("default_password", data.get("default_password", "12345678"))
        ),
        users_to_generate=int(
            os.environ.get("USERS_TO_GENERATE", data.get("users_to_generate", 100))
        ),
        environment=os.environ.get("ENVIRONMENT", data.get("environment", "production")),
        api_url=os.environ.get(
            "SIGNUP_API_URL", person_data.get("api_url", data.get("api_url", "https://adselect.net/api/auth/register"))
        ),
        allow_remote_signup=_parse_bool(
            os.environ.get("ALLOW_REMOTE_SIGNUP", data.get("allow_remote_signup", True))
        ),
        sponsor=os.environ.get("SPONSOR", person_data.get("sponsor_id", data.get("sponsor", "ASD5437949"))),
        source_url=os.environ.get(
            "SOURCE_URL",
            person_data.get("source_url", data.get("source_url", f"https://adselect.net/register?ref={os.environ.get('SPONSOR', data.get('sponsor', 'ASD5437949'))}")),
        ),
        output_dir=os.environ.get("OUTPUT_DIR", data.get("output_dir", "output")),
        log_level=os.environ.get("LOG_LEVEL", data.get("log_level", "INFO")),
        db_path=os.environ.get("DB_PATH", data.get("db_path", "output/test_users.db")),
        csv_path=os.environ.get("CSV_PATH", data.get("csv_path", "output/test_users.csv")),
        json_path=os.environ.get(
            "JSON_PATH", data.get("json_path", "output/test_users.json")
        ),
        results_path=os.environ.get(
            "RESULTS_PATH", data.get("results_path", "output/signup_results.json")
        ),
        request_delay_seconds=float(
            os.environ.get("REQUEST_DELAY_SECONDS", data.get("request_delay_seconds", 2.0))
        ),
        request_max_retries=int(
            os.environ.get("REQUEST_MAX_RETRIES", data.get("request_max_retries", 2))
        ),
        batch_size=int(
            os.environ.get("BATCH_SIZE", data.get("batch_size", 0))
        ),
        batch_delay_seconds=float(
            os.environ.get("BATCH_DELAY_SECONDS", data.get("batch_delay_seconds", 0.0))
        ),
        network_reset_command=os.environ.get(
            "NETWORK_RESET_COMMAND", data.get("network_reset_command", "")
        ),
        network_reset_wait_seconds=float(
            os.environ.get(
                "NETWORK_RESET_WAIT_SECONDS", data.get("network_reset_wait_seconds", 10.0)
            )
        ),
        preflight_before_submit=_parse_bool(
            os.environ.get("PREFLIGHT_BEFORE_SUBMIT", data.get("preflight_before_submit", True))
        ),
        username_length=int(os.environ.get("USERNAME_LENGTH", data.get("username_length", 8))),
        person_config_path=data.get("person_config_path", "config/person.json"),
        proxies=proxies,
        verify_ssl=data.get("verify_ssl", True),
        proxy_check_timeout=data.get("proxy_check_timeout", 5),
        proxy_check_url=data.get("proxy_check_url", "https://api.ipify.org"),
        max_workers_proxy_check=data.get("max_workers_proxy_check", 20),
        auto_validate_proxies=data.get("auto_validate_proxies", True),
        persist_valid_proxies=data.get("persist_valid_proxies", True),
    )


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger."""
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )