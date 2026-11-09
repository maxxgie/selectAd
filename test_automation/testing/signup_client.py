"""Signup client.

Submits signup/registration requests to the configured endpoint.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

import requests

from database.sqlite_manager import SQLiteManager
from generators.user_generator import TestUser

logger = logging.getLogger(__name__)


@dataclass
class SignupResult:
    """Outcome of a single signup/registration submission."""

    user: TestUser
    success: bool
    status_code: Optional[int]
    response: Optional[dict[str, Any]]
    error_message: Optional[str] = None


# Backward-compatible alias.
TestResult = SignupResult


class SignupClient:
    """Client for submitting signup requests to the configured endpoint."""

    def __init__(
        self,
        api_url: str,
        allow_remote: bool = False,
        timeout: int = 10,
        sponsor: str = "",
        delay_seconds: float = 0.5,
        max_retries: int = 4,
        batch_size: int = 0,
        batch_delay_seconds: float = 0.0,
        network_reset_command: str = "",
        network_reset_wait_seconds: float = 10.0,
        source_url: str = "",
        db_path: str = "output/test_users.db",
    ) -> None:
        self.api_url = (api_url or "").rstrip("/")
        self.timeout = timeout
        self.sponsor = sponsor
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.max_retries = max(0, int(max_retries))
        self.batch_size = max(0, int(batch_size))
        self.batch_delay_seconds = max(0.0, float(batch_delay_seconds))
        self.network_reset_command = (network_reset_command or "").strip()
        self.network_reset_wait_seconds = max(0.0, float(network_reset_wait_seconds))
        self.source_url = (source_url or "").strip() or (
            f"https://adselect.net/register?ref={sponsor}" if sponsor else "https://adselect.net/register"
        )
        self.db_path = db_path
        self._vpn_state_path = Path(self.db_path).parent / ".vpn_state.json"

        if not self.api_url:
            raise ValueError("api_url is required")
        if not allow_remote and self.api_url.lower().startswith(("http://", "https://")) and not any(
            h in self.api_url.lower() for h in ("localhost", "127.0.0.1", "::1", ".local", ".dev")
        ):
            raise ValueError(
                "Remote signup is disabled. Set ALLOW_REMOTE_SIGNUP=true to use a remote URL."
            )

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": self.source_url,
            }
        )

    def preflight(self) -> dict[str, Any]:
        """Check outbound connectivity before sending registrations."""
        info: dict[str, Any] = {"api_reachable": False, "public_ip": None}
        try:
            ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
            info["public_ip"] = ip
        except Exception as exc:  # noqa: BLE001
            info["public_ip_error"] = str(exc)
        try:
            r = requests.get(self.api_url.rsplit("/api/", 1)[0], timeout=10)
            info["api_reachable"] = r.status_code < 500
            info["api_status"] = r.status_code
        except Exception as exc:  # noqa: BLE001
            info["api_error"] = str(exc)

        # VPN/server IP change detection
        last_ip = None
        if self._vpn_state_path.exists():
            try:
                state = json.loads(self._vpn_state_path.read_text(encoding="utf-8"))
                last_ip = state.get("last_public_ip")
            except Exception:  # noqa: BLE001
                last_ip = None
        info["vpn_ip_changed"] = last_ip is not None and last_ip != info.get("public_ip")
        if info.get("vpn_ip_changed"):
            logger.warning(
                "VPN/server IP changed: %s -> %s", last_ip, info.get("public_ip")
            )
        try:
            self._vpn_state_path.write_text(
                json.dumps({"last_public_ip": info.get("public_ip")}, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

        return info

    def _reset_network(self, reason: str) -> bool:
        if not self.network_reset_command:
            logger.warning("Rate-limited (%s) and no network_reset_command is configured; cannot rotate network automatically.", reason)
            return False
        logger.warning("Rate-limited (%s); running configured network reset command.", reason)
        try:
            # Command is explicitly configured by the operator; run via shell to allow their chosen toolchain.
            completed = subprocess.run(
                self.network_reset_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                logger.error("Network reset command failed (%s): %s", completed.returncode, (completed.stderr or completed.stdout)[:500])
                return False
            logger.info("Network reset command completed; waiting %.1fs.", self.network_reset_wait_seconds)
            time.sleep(self.network_reset_wait_seconds)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Network reset command error: %s", exc)
            return False

    def signup(self, user: TestUser) -> SignupResult:
        """Submit a single signup request and return the result."""
        payload = {
            "name": user.full_name,
            "full_name": user.full_name,
            "username": user.username,
            "email": user.email,
            "country": user.country,
            "phone": user.phone,
            "password": user.password,
            "sponsor": self.sponsor,
            "website": "",
            "sourceUrl": self.source_url,
        }
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(self.api_url, json=payload, timeout=self.timeout)
                status_code = response.status_code
                try:
                    response_json: Optional[dict[str, Any]] = response.json()
                except ValueError:
                    response_json = {"raw": response.text}

                if 200 <= status_code < 300:
                    logger.info("Signup succeeded for %s (status %s)", user.username, status_code)
                    try:
                        with SQLiteManager(self.db_path) as db:
                            db.delete_user(user.username)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed to delete submitted user %s from DB: %s", user.username, exc)
                    return SignupResult(
                        user=user, success=True, status_code=status_code, response=response_json
                    )

                if status_code == 429 and attempt < self.max_retries and self._reset_network(str(response_json)):
                    continue
                if status_code in {429, 500, 502, 503, 504, 522, 524} and attempt < self.max_retries:
                    retry_after = response.headers.get("Retry-After")
                    wait = min(
                        float(retry_after) if retry_after and retry_after.isdigit() else (2.0 * (attempt + 1)),
                        20.0,
                    )
                    logger.warning(
                        "Signup retryable response for %s (status %s); waiting %.1fs",
                        user.username,
                        status_code,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                logger.warning(
                    "Signup rejected for %s (status %s): %s",
                    user.username,
                    status_code,
                    response_json,
                )
                return SignupResult(
                    user=user,
                    success=False,
                    status_code=status_code,
                    response=response_json,
                    error_message=str(response_json),
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = min(1.0 + (attempt * 1.5), 10.0)
                    logger.warning("Signup request error for %s: %s; retrying in %.1fs", user.username, exc, wait)
                    time.sleep(wait)
                    continue
                logger.error("Signup request failed for %s: %s", user.username, exc)
        return SignupResult(
            user=user,
            success=False,
            status_code=None,
            response=None,
            error_message=str(last_exc) if last_exc else "unknown error",
        )

    def run(self, users: List[TestUser]) -> List[SignupResult]:
        """Submit signup requests for all users and return results."""
        results: List[SignupResult] = []
        for i, user in enumerate(users):
            results.append(self.signup(user))
            if i < len(users) - 1:
                if self.batch_size and (i + 1) % self.batch_size == 0:
                    time.sleep(self.batch_delay_seconds)
                elif self.delay_seconds:
                    time.sleep(self.delay_seconds)
        return results

    # Backward-compatible alias.
    def run_tests(self, users: List[TestUser]) -> List[SignupResult]:
        return self.run(users)

    def summary(self, results: List[TestResult]) -> dict[str, Any]:
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        return {
            "total": len(results),
            "successful": len(successful),
            "failed": len(failed),
            "successful_users": [r.user.username for r in successful],
            "failed_users": [r.user.username for r in failed],
            "errors": [r.error_message for r in failed if r.error_message],
        }

    def close(self) -> None:
        self.session.close()
