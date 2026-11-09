"""Signup client.

Submits signup/registration requests to the configured endpoint.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

import requests
import urllib3

from database.sqlite_manager import SQLiteManager
from generators.user_generator import TestUser, UserGenerator

# Suppress InsecureRequestWarning when verify_ssl=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
        proxies: list[str] | None = None,
        verify_ssl: bool = True,
        country: str = "United States",
        default_password: str = "12345678",
        username_length: int = 8,
        proxy_check_timeout: int = 5,
        proxy_check_url: str = "https://api.ipify.org",
        max_workers_proxy_check: int = 20,
        auto_validate_proxies: bool = True,
        persist_valid_proxies: bool = True,
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
        self._original_proxies = proxies or []
        self.proxies = list(self._original_proxies)
        self._proxy_index = 0
        self._failed_proxies: set[str] = set()
        self._validated_proxies: set[str] = set()
        self.verify_ssl = verify_ssl
        self.country = country
        self.default_password = default_password
        self.username_length = username_length
        self._user_generator = UserGenerator(
            country=country, default_password=default_password, username_length=username_length
        )
        self.proxy_check_timeout = proxy_check_timeout
        self.proxy_check_url = proxy_check_url
        self.max_workers_proxy_check = max_workers_proxy_check
        self.auto_validate_proxies = auto_validate_proxies
        self.persist_valid_proxies = persist_valid_proxies
        self._proxies_file_path: Path | None = None
        self._proxy_validation_done = False

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

        # Validate proxies on first preflight if enabled
        if self.auto_validate_proxies and not self._proxy_validation_done and self.proxies:
            logger.info("Running initial proxy validation...")
            self.validate_proxies()
            self._proxy_validation_done = True
            info["proxies_validated"] = len(self.proxies)
            info["proxies_removed"] = len(self._original_proxies) - len(self.proxies)

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

    def _check_proxy(self, proxy: str) -> tuple[str, bool]:
        """Check if a single proxy works by making a test request. Returns (proxy, is_valid)."""
        test_session = requests.Session()
        test_session.proxies = {"http": proxy, "https": proxy}
        test_session.headers.update(self.session.headers)
        try:
            response = test_session.get(self.proxy_check_url, timeout=self.proxy_check_timeout, verify=self.verify_ssl)
            if 200 <= response.status_code < 300:
                return proxy, True
        except requests.RequestException:
            pass
        return proxy, False

    def validate_proxies(self, proxies: list[str] | None = None, persist: bool = True) -> list[str]:
        """Validate a list of proxies in parallel. Returns list of working proxies."""
        target_proxies = proxies or self.proxies
        if not target_proxies:
            logger.warning("No proxies to validate")
            return []
        
        logger.info("Validating %d proxies (timeout=%ds, max_workers=%d)...", 
                    len(target_proxies), self.proxy_check_timeout, self.max_workers_proxy_check)
        
        valid = []
        invalid = []
        
        with ThreadPoolExecutor(max_workers=self.max_workers_proxy_check) as executor:
            future_to_proxy = {executor.submit(self._check_proxy, p): p for p in target_proxies}
            for future in as_completed(future_to_proxy):
                proxy, is_valid = future.result()
                if is_valid:
                    valid.append(proxy)
                    self._validated_proxies.add(proxy)
                else:
                    invalid.append(proxy)
        
        logger.info("Proxy validation complete: %d valid, %d invalid", len(valid), len(invalid))
        
        if invalid:
            logger.debug("Invalid proxies removed: %s", invalid)
        
        # Update internal proxy list to only valid ones
        self.proxies = valid
        
        # Persist valid proxies to file if configured
        if persist and self.persist_valid_proxies and self._proxies_file_path:
            self._persist_proxies()
        
        return valid

    def _persist_proxies(self) -> None:
        """Save current valid proxies to the proxies file."""
        if not self._proxies_file_path:
            return
        try:
            # Read original file to preserve comments/formatting if needed, or just write valid ones
            with self._proxies_file_path.open("w", encoding="utf-8") as fh:
                for proxy in self.proxies:
                    fh.write(proxy + "\n")
            logger.info("Persisted %d valid proxies to %s", len(self.proxies), self._proxies_file_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist proxies: %s", exc)

    def set_proxies_file_path(self, path: str | Path) -> None:
        """Set the path to the proxies file for persistence."""
        self._proxies_file_path = Path(path)

    def _get_next_proxy(self) -> dict[str, str] | None:
        """Get the next healthy proxy from the list. Returns None if no proxies available."""
        if not self.proxies:
            return None
        
        available = [p for p in self.proxies if p not in self._failed_proxies]
        if not available:
            # All proxies failed, reset and try all
            logger.warning("All proxies marked as failed; resetting failed proxy list")
            self._failed_proxies.clear()
            available = self.proxies
        
        proxy = available[self._proxy_index % len(available)]
        self._proxy_index += 1
        return {"http": proxy, "https": proxy}

    def _mark_proxy_failed(self, proxy_dict: dict[str, str] | None) -> None:
        """Mark a proxy as failed so it will be skipped."""
        if proxy_dict:
            proxy_url = proxy_dict.get("http")
            if proxy_url:
                self._failed_proxies.add(proxy_url)
                # Also remove from validated set
                self._validated_proxies.discard(proxy_url)
                logger.debug("Marked proxy as failed: %s", proxy_url)
                
                # Persist updated proxy list
                if self.persist_valid_proxies and self._proxies_file_path:
                    self._persist_proxies()

    def _apply_proxy(self, proxy_dict: dict[str, str] | None) -> None:
        """Apply or remove proxy from session."""
        if proxy_dict:
            self.session.proxies.update(proxy_dict)
            logger.debug("Using proxy: %s", proxy_dict.get("http"))
        else:
            self.session.proxies.clear()
            logger.debug("Cleared proxy (direct connection)")

    def _generate_fresh_user(self) -> TestUser:
        """Generate a new user to retry with after 'already registered' error."""
        return self._user_generator.generate(1)[0]

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
        current_user = user
        last_exc: Optional[Exception] = None
        current_proxy: Optional[dict[str, str]] = None
        
        for attempt in range(self.max_retries + 1):
            try:
                # Apply proxy rotation on retry attempts (after first attempt)
                if attempt > 0 and self.proxies:
                    # Mark previous proxy as failed if we had one
                    if current_proxy:
                        self._mark_proxy_failed(current_proxy)
                    
                    current_proxy = self._get_next_proxy()
                    self._apply_proxy(current_proxy)

                response = self.session.post(
                    self.api_url, 
                    json=payload, 
                    timeout=self.timeout,
                    verify=self.verify_ssl
                )
                status_code = response.status_code
                try:
                    response_json: Optional[dict[str, Any]] = response.json()
                except ValueError:
                    response_json = {"raw": response.text}

                if 200 <= status_code < 300:
                    logger.info("Signup succeeded for %s (status %s)", current_user.username, status_code)
                    try:
                        with SQLiteManager(self.db_path) as db:
                            db.delete_user(current_user.username)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed to delete submitted user %s from DB: %s", current_user.username, exc)
                    return SignupResult(
                        user=current_user, success=True, status_code=status_code, response=response_json
                    )

                # Handle 400 "already registered" - generate new user and retry
                if status_code == 400 and attempt < self.max_retries:
                    error_msg = str(response_json).lower()
                    if "already registered" in error_msg or "email or phone" in error_msg:
                        logger.warning("Email/phone already registered for %s; generating new user (attempt %d/%d)", current_user.username, attempt + 1, self.max_retries)
                        current_user = self._generate_fresh_user()
                        payload = {
                            "name": current_user.full_name,
                            "full_name": current_user.full_name,
                            "username": current_user.username,
                            "email": current_user.email,
                            "country": current_user.country,
                            "phone": current_user.phone,
                            "password": current_user.password,
                            "sponsor": self.sponsor,
                            "website": "",
                            "sourceUrl": self.source_url,
                        }
                        continue

                if status_code == 429 and attempt < self.max_retries:
                    logger.warning("Rate limited (429) for %s; rotating proxy (attempt %d/%d)", current_user.username, attempt + 1, self.max_retries)
                    continue
                if status_code in {429, 500, 502, 503, 504, 522, 524} and attempt < self.max_retries:
                    retry_after = response.headers.get("Retry-After")
                    wait = min(
                        float(retry_after) if retry_after and retry_after.isdigit() else (2.0 * (attempt + 1)),
                        20.0,
                    )
                    logger.warning(
                        "Signup retryable response for %s (status %s); waiting %.1fs",
                        current_user.username,
                        status_code,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                logger.warning(
                    "Signup rejected for %s (status %s): %s",
                    current_user.username,
                    status_code,
                    response_json,
                )
                return SignupResult(
                    user=current_user,
                    success=False,
                    status_code=status_code,
                    response=response_json,
                    error_message=str(response_json),
                )
            except requests.RequestException as exc:
                last_exc = exc
                # Mark proxy as failed on connection/SSL errors
                if current_proxy and self.proxies:
                    self._mark_proxy_failed(current_proxy)
                
                if attempt < self.max_retries and self.proxies:
                    logger.warning("Request error for %s: %s; rotating proxy (attempt %d/%d)", current_user.username, exc, attempt + 1, self.max_retries)
                    continue
                if attempt < self.max_retries:
                    wait = min(1.0 + (attempt * 1.5), 10.0)
                    logger.warning("Signup request error for %s: %s; retrying in %.1fs", current_user.username, exc, wait)
                    time.sleep(wait)
                    continue
                logger.error("Signup request failed for %s: %s", current_user.username, exc)
        return SignupResult(
            user=current_user,
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