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
from collections import deque

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
        
        # Store ALL original proxies (1126) - don't filter yet
        self._all_proxies = proxies or []
        
        # Batch configuration - split all proxies into batches of 10
        self._proxy_batch_size = 10
        self._all_batches: list[list[str]] = []
        self._current_batch_index = 0
        
        # Track batch states: 'available', 'in_use', 'resting', 'exhausted'
        self._batch_states: list[str] = []
        self._batch_proxy_index: dict[int, int] = {}  # batch_index -> proxy_index
        self._batch_validated: dict[int, list[str]] = {}  # batch_index -> validated proxies
        self._batch_failed_proxies: dict[int, set[str]] = {}  # batch_index -> failed proxies
        
        # Rest pool: batches that have been used and are cooling down
        self._rest_pool: deque[tuple[int, float]] = deque()  # (batch_index, rest_until_timestamp)
        self._rest_duration_seconds = 300  # 5 minutes cooldown
        
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

        # Organize all proxies into batches
        self._organize_all_batches()

    def _organize_all_batches(self) -> None:
        """Split ALL proxies into batches of _proxy_batch_size (default 10)."""
        if not self._all_proxies:
            self._all_batches = []
            self._batch_states = []
            return
        
        self._all_batches = [
            self._all_proxies[i:i + self._proxy_batch_size]
            for i in range(0, len(self._all_proxies), self._proxy_batch_size)
        ]
        self._batch_states = ["available"] * len(self._all_batches)
        self._batch_proxy_index = {i: 0 for i in range(len(self._all_batches))}
        self._batch_validated = {i: [] for i in range(len(self._all_batches))}
        self._batch_failed_proxies = {i: set() for i in range(len(self._all_batches))}
        self._current_batch_index = 0
        logger.info("Organized %d proxies into %d batches of size %d", 
                   len(self._all_proxies), len(self._all_batches), self._proxy_batch_size)

    def _get_current_batch(self) -> list[str] | None:
        """Get the current batch, skipping exhausted/resting ones."""
        # First, check if any resting batches are ready to be reused
        self._check_rest_pool()
        
        # Find next available batch
        while self._current_batch_index < len(self._all_batches):
            state = self._batch_states[self._current_batch_index]
            if state == "available":
                return self._all_batches[self._current_batch_index]
            elif state == "in_use":
                return self._all_batches[self._current_batch_index]
            self._current_batch_index += 1
        
        # No available batches - check if any are resting
        if self._rest_pool:
            wait_time = self._rest_pool[0][1] - time.time()
            if wait_time > 0:
                logger.info("All batches in use/resting; waiting %.1fs for next batch to be ready", wait_time)
                time.sleep(wait_time)
                self._check_rest_pool()
                return self._get_current_batch()
        
        logger.warning("No batches available")
        return None

    def _check_rest_pool(self) -> None:
        """Move batches from rest pool back to available if cooldown complete."""
        now = time.time()
        while self._rest_pool and self._rest_pool[0][1] <= now:
            batch_idx, _ = self._rest_pool.popleft()
            self._batch_states[batch_idx] = "available"
            self._batch_failed_proxies[batch_idx].clear()
            logger.info("Batch %d cooldown complete; moved back to available", batch_idx + 1)

    def _validate_batch(self, batch_index: int) -> list[str]:
        """Validate proxies in a batch. Returns list of working proxies."""
        batch = self._all_batches[batch_index]
        if not batch:
            return []
        
        # Check if already validated
        if self._batch_validated[batch_index]:
            return self._batch_validated[batch_index]
        
        logger.info("Validating batch %d (%d proxies)...", batch_index + 1, len(batch))
        
        valid = []
        with ThreadPoolExecutor(max_workers=self.max_workers_proxy_check) as executor:
            future_to_proxy = {executor.submit(self._check_proxy, p): p for p in batch}
            for future in as_completed(future_to_proxy):
                proxy, is_valid = future.result()
                if is_valid:
                    valid.append(proxy)
        
        self._batch_validated[batch_index] = valid
        logger.info("Batch %d validation complete: %d valid, %d invalid", 
                   batch_index + 1, len(valid), len(batch) - len(valid))
        return valid

    def _get_next_proxy(self) -> dict[str, str] | None:
        """Get the next working proxy from the current batch."""
        # Get current batch
        batch = self._get_current_batch()
        if not batch:
            return None
        
        batch_idx = self._current_batch_index
        self._batch_states[batch_idx] = "in_use"
        
        # Validate batch if not done
        validated = self._validate_batch(batch_idx)
        if not validated:
            logger.warning("Batch %d has no valid proxies; marking exhausted", batch_idx + 1)
            self._mark_batch_exhausted(batch_idx)
            return self._get_next_proxy()
        
        # Get next non-failed proxy from validated list
        proxy_idx = self._batch_proxy_index[batch_idx]
        failed_in_batch = self._batch_failed_proxies[batch_idx]
        
        attempts = 0
        while attempts < len(validated):
            proxy = validated[proxy_idx % len(validated)]
            self._batch_proxy_index[batch_idx] += 1
            proxy_idx = self._batch_proxy_index[batch_idx]
            
            if proxy not in failed_in_batch:
                return {"http": proxy, "https": proxy}
            attempts += 1
        
        # All proxies in this batch failed - mark batch exhausted
        logger.info("Batch %d exhausted (all %d proxies failed); moving to rest pool", 
                   batch_idx + 1, len(validated))
        self._mark_batch_exhausted(batch_idx)
        return self._get_next_proxy()

    def _mark_batch_exhausted(self, batch_index: int) -> None:
        """Mark a batch as exhausted and move to rest pool."""
        self._batch_states[batch_index] = "exhausted"
        rest_until = time.time() + self._rest_duration_seconds
        self._rest_pool.append((batch_index, rest_until))
        self._current_batch_index += 1
        logger.info("Batch %d moved to rest pool for %.0fs", batch_index + 1, self._rest_duration_seconds)

    def _mark_proxy_failed(self, proxy_dict: dict[str, str] | None) -> None:
        """Mark a proxy as failed in the current batch."""
        if proxy_dict:
            proxy_url = proxy_dict.get("http")
            if proxy_url:
                batch_idx = self._current_batch_index
                self._batch_failed_proxies[batch_idx].add(proxy_url)
                logger.debug("Marked proxy as failed in batch %d: %s", batch_idx + 1, proxy_url)

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

        # Validate proxies on first preflight if enabled - just validate first batch
        if self.auto_validate_proxies and not self._proxy_validation_done and self._all_batches:
            logger.info("Running initial proxy validation on first batch...")
            self._validate_batch(0)
            self._proxy_validation_done = True
            validated_count = len(self._batch_validated.get(0, []))
            info["proxies_validated"] = validated_count
            info["total_batches"] = len(self._all_batches)
            info["proxies_in_first_batch"] = len(self._all_batches[0]) if self._all_batches else 0

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
        """Validate all proxy batches. Returns list of all working proxies across all batches."""
        if not self._all_batches:
            logger.warning("No proxy batches to validate")
            return []
        
        all_valid = []
        for i in range(len(self._all_batches)):
            valid = self._validate_batch(i)
            all_valid.extend(valid)
        
        logger.info("All batches validation complete: %d total valid proxies", len(all_valid))
        
        if persist and self.persist_valid_proxies and self._proxies_file_path:
            self._persist_proxies()
        
        return all_valid

    def _persist_proxies(self) -> None:
        """Save current valid proxies from all batches to the proxies file."""
        if not self._proxies_file_path:
            return
        try:
            all_valid = []
            for batch_valid in self._batch_validated.values():
                all_valid.extend(batch_valid)
            with self._proxies_file_path.open("w", encoding="utf-8") as fh:
                for proxy in all_valid:
                    fh.write(proxy + "\n")
            logger.info("Persisted %d valid proxies to %s", len(all_valid), self._proxies_file_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist proxies: %s", exc)

    def set_proxies_file_path(self, path: str | Path) -> None:
        """Set the path to the proxies file for persistence."""
        self._proxies_file_path = Path(path)

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
                if attempt > 0 and self._all_batches:
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
                if current_proxy and self._all_batches:
                    self._mark_proxy_failed(current_proxy)
                
                if attempt < self.max_retries and self._all_batches:
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