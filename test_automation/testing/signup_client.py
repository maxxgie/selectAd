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
        
        # Progress tracking file - stores which batch index we've processed up to
        self._progress_file = Path(self.db_path).parent / ".proxy_progress.json"
        
        # Load original proxies and progress
        self._all_proxies = proxies or []
        self._original_proxies = list(self._all_proxies)  # Keep full list for reload
        
        # Load progress (which batches have been consumed)
        self._consumed_batches = self._load_progress()
        self._current_batch_index = self._consumed_batches
        
        # Batch configuration - split all proxies into batches of 10
        self._proxy_batch_size = 10
        self._all_batches: list[list[str]] = []
        self._batch_states: list[str] = []
        self._batch_proxy_index: dict[int, int] = {}
        self._batch_validated: dict[int, list[str]] = {}
        self._batch_failed_proxies: dict[int, set[str]] = {}
        self._batch_successful_proxies: dict[int, list[str]] = {}
        
        # Rest pool: SUCCESSFUL proxies from exhausted batches
        self._rest_pool: list[tuple[str, float]] = []
        self._rest_duration_seconds = 300
        self._main_batches_exhausted = False
        self._batch_429_count: dict[int, int] = {}
        
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

    def _ensure_backup_file(self) -> None:
        """Ensure backup file exists and is empty on startup."""
        if not self._proxies_file_path:
            return
        backup_path = self._proxies_file_path.with_suffix(".txt.backup")
        try:
            # Create empty backup file on startup
            with backup_path.open("w", encoding="utf-8") as fh:
                pass  # Truncate to empty
            logger.info("Initialized empty backup file: %s", backup_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to initialize backup file: %s", exc)

    def _load_progress(self) -> int:
        """Load consumed batch count from progress file."""
        if self._progress_file.exists():
            try:
                content = self._progress_file.read_text(encoding="utf-8-sig")  # Handle BOM
                data = json.loads(content)
                consumed = data.get("consumed_batches", 0)
                logger.info("Loaded progress: %d batches already consumed", consumed)
                return consumed
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load progress file: %s", exc)
        return 0

    def _save_progress(self) -> None:
        """Save consumed batch count to progress file."""
        try:
            data = {"consumed_batches": self._consumed_batches}
            self._progress_file.write_text(json.dumps(data, indent=2), encoding="utf-8-sig")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save progress file: %s", exc)

    def _update_proxies_file(self, batch_index: int) -> None:
        """Remove consumed batch proxies from main file. Write consumed proxies to backup file."""
        if not self._proxies_file_path or not self._proxies_file_path.exists():
            return
        
        try:
            # Read current main file
            with self._proxies_file_path.open("r", encoding="utf-8") as fh:
                lines = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
            
            # Get proxies to remove from the current batch
            if batch_index < len(self._all_batches):
                proxies_to_remove = list(self._all_batches[batch_index])
            else:
                proxies_to_remove = []
            
            # Write remaining proxies to main file
            remaining = [p for p in lines if p not in set(proxies_to_remove)]
            with self._proxies_file_path.open("w", encoding="utf-8") as fh:
                for proxy in remaining:
                    fh.write(proxy + "\n")
            
            logger.info("Updated proxies file: removed batch %d (%d proxies), %d remaining", 
                        batch_index + 1, len(proxies_to_remove), len(remaining))
            
            # Write consumed proxies to backup file
            self._append_to_backup(proxies_to_remove)
            
            # Save successful proxies to working file
            self._save_working_proxies(batch_index)
            
            # If main file is empty, reload from backup
            if not remaining:
                self._reload_main_from_backup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to update proxies file: %s", exc)

    def _append_to_backup(self, proxies: list[str]) -> None:
        """Append consumed proxies to the backup file (no duplicates)."""
        if not self._proxies_file_path or not proxies:
            return
        backup_path = self._proxies_file_path.with_suffix(".txt.backup")
        try:
            # Read existing backup to check for duplicates
            existing = []
            if backup_path.exists():
                with backup_path.open("r", encoding="utf-8") as fh:
                    existing = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
            
            existing_set = set(existing)
            new_proxies = [p for p in proxies if p not in existing_set]
            
            if new_proxies:
                with backup_path.open("a", encoding="utf-8") as fh:
                    for proxy in new_proxies:
                        fh.write(proxy + "\n")
                logger.debug("Appended %d new consumed proxies to backup file", len(new_proxies))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to append to backup file: %s", exc)

    def _save_working_proxies(self, batch_index: int) -> None:
        """Save successful proxies from a batch to the working proxies file."""
        if not self._proxies_file_path:
            return
        
        working_path = self._proxies_file_path.with_suffix(".working.txt")
        successful = self._batch_successful_proxies.get(batch_index, [])
        if not successful:
            return
        
        try:
            # Read existing working proxies
            existing = []
            if working_path.exists():
                with working_path.open("r", encoding="utf-8") as fh:
                    existing = [line.strip() for line in fh if line.strip()]
            
            # Add new successful proxies (avoid duplicates)
            existing_set = set(existing)
            for proxy in successful:
                if proxy not in existing_set:
                    existing.append(proxy)
                    existing_set.add(proxy)
            
            # Write working proxies
            with working_path.open("w", encoding="utf-8") as fh:
                for proxy in existing:
                    fh.write(proxy + "\n")
            
            logger.info("Saved %d working proxies to %s (%d total)", 
                       len(successful), working_path, len(existing))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save working proxies: %s", exc)

    def _reload_main_from_backup(self) -> None:
        """Reload main proxies file from backup when it's empty. Then clear backup."""
        if not self._proxies_file_path:
            return
        
        backup_path = self._proxies_file_path.with_suffix(".txt.backup")
        if backup_path and backup_path.exists():
            try:
                # Read backup, deduplicate
                with backup_path.open("r", encoding="utf-8") as fh:
                    backup_lines = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
                
                # Deduplicate while preserving order
                seen = set()
                unique_proxies = []
                for p in backup_lines:
                    if p not in seen:
                        seen.add(p)
                        unique_proxies.append(p)
                
                # Write deduplicated proxies to main file
                with self._proxies_file_path.open("w", encoding="utf-8") as fh:
                    for proxy in unique_proxies:
                        fh.write(proxy + "\n")
                
                # Clear backup file after successful reload
                with backup_path.open("w", encoding="utf-8") as fh:
                    pass  # Truncate to empty
                
                logger.info("Reloaded main proxies file from backup: %d unique proxies (was %d lines); backup cleared", 
                           len(unique_proxies), len(backup_lines))
                
                # Reload proxies from the restored file
                self._all_proxies = list(unique_proxies)
                self._original_proxies = list(self._all_proxies)
                
                # Reset progress and reorganize
                self._consumed_batches = 0
                self._current_batch_index = 0
                self._batch_429_count = {}
                self._save_progress()
                self._organize_all_batches()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to reload from backup: %s", exc)

    def _reload_all_proxies(self) -> None:
        """Reload all proxies from backup/original and reset progress. Restore main file from backup."""
        backup_path = self._proxies_file_path.with_suffix(".txt.backup") if self._proxies_file_path else None
        
        # Use the same deduplication logic as _reload_main_from_backup
        if backup_path and backup_path.exists():
            try:
                # Read backup, deduplicate
                with backup_path.open("r", encoding="utf-8") as fh:
                    backup_lines = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
                
                seen = set()
                unique_proxies = []
                for p in backup_lines:
                    if p not in seen:
                        seen.add(p)
                        unique_proxies.append(p)
                
                # Write deduplicated proxies to main file
                if self._proxies_file_path:
                    with self._proxies_file_path.open("w", encoding="utf-8") as fh:
                        for proxy in unique_proxies:
                            fh.write(proxy + "\n")
                
                # Clear backup file after successful reload
                with backup_path.open("w", encoding="utf-8") as fh:
                    pass  # Truncate to empty
                
                logger.info("Reloaded %d unique proxies from backup (was %d lines); backup cleared", 
                           len(unique_proxies), len(backup_lines))
                
                self._all_proxies = list(unique_proxies)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to reload from backup: %s", exc)
                self._all_proxies = list(self._original_proxies)
        elif self._proxies_file_path and self._proxies_file_path.exists():
            with self._proxies_file_path.open("r", encoding="utf-8") as fh:
                self._all_proxies = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
            logger.info("Reloaded %d proxies from main file", len(self._all_proxies))
        else:
            self._all_proxies = list(self._original_proxies)
            logger.info("Reloaded %d proxies from original list", len(self._all_proxies))
        
        # Update original_proxies to match reloaded list
        self._original_proxies = list(self._all_proxies)
        
        # Reset progress
        self._consumed_batches = 0
        self._current_batch_index = 0
        self._main_batches_exhausted = False
        self._rest_pool = []
        self._batch_429_count = {}
        self._save_progress()
        
        # Reorganize batches
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
        self._batch_successful_proxies = {i: [] for i in range(len(self._all_batches))}
        
        # Skip already consumed batches
        if self._consumed_batches > 0:
            # If consumed batches exceed available batches, reset and use current file
            if self._consumed_batches >= len(self._all_batches):
                logger.info("Consumed batches (%d) >= available batches (%d); resetting progress and starting fresh", 
                           self._consumed_batches, len(self._all_batches))
                self._consumed_batches = 0
                self._current_batch_index = 0
                self._rest_pool = []
                self._main_batches_exhausted = False
                self._save_progress()
            else:
                for i in range(self._consumed_batches):
                    self._batch_states[i] = "exhausted"
                self._current_batch_index = self._consumed_batches
                logger.info("Skipping first %d consumed batches; starting at batch %d", 
                           self._consumed_batches, self._current_batch_index + 1)
        else:
            self._current_batch_index = 0
        
        self._rest_pool = []
        self._main_batches_exhausted = False
        self._batch_429_count = {}
        logger.info("Organized %d proxies into %d batches of size %d (%d available)", 
                   len(self._all_proxies), len(self._all_batches), self._proxy_batch_size,
                   len(self._all_batches) - self._consumed_batches)

    def _get_current_batch(self) -> list[str] | None:
        """Get the current batch, skipping exhausted ones."""
        # Find next available batch from main list
        while self._current_batch_index < len(self._all_batches):
            state = self._batch_states[self._current_batch_index]
            if state == "available":
                return self._all_batches[self._current_batch_index]
            elif state == "in_use":
                return self._all_batches[self._current_batch_index]
            self._current_batch_index += 1
        
        # All main batches exhausted - mark flag and check rest pool
        if not self._main_batches_exhausted:
            self._main_batches_exhausted = True
            logger.info("All %d main batches exhausted; moving to rest pool", len(self._all_batches))
        
        # Check rest pool for ready proxies
        self._check_rest_pool()
        
        # If rest pool has ready proxies, create a virtual batch from them
        ready_proxies = [p for p, rest_until in self._rest_pool if rest_until <= time.time()]
        if ready_proxies:
            logger.info("Using %d proxies from rest pool", len(ready_proxies))
            # Remove used proxies from rest pool
            self._rest_pool = [(p, t) for p, t in self._rest_pool if t > time.time()]
            return ready_proxies
        
        # No ready proxies in rest pool - wait for next one
        if self._rest_pool:
            wait_time = min(t for _, t in self._rest_pool) - time.time()
            if wait_time > 0:
                logger.info("Waiting %.1fs for next rest pool proxy", wait_time)
                time.sleep(wait_time)
                self._check_rest_pool()
                return self._get_current_batch()
        
        # Rest pool also empty - FULLY EXHAUSTED: reload all proxies and start over
        logger.info("All batches and rest pool exhausted; reloading all proxies and starting fresh")
        self._reload_all_proxies()
        return self._get_current_batch()

    def _check_rest_pool(self) -> None:
        """No-op: rest pool is checked lazily in _get_current_batch"""
        pass

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
        """Get the next working proxy from the current batch or rest pool."""
        # Get current batch (main or rest pool)
        batch = self._get_current_batch()
        if not batch:
            return None
        
        # Check if we're using rest pool (main batches exhausted)
        using_rest_pool = self._main_batches_exhausted
        
        if using_rest_pool:
            # Rest pool: batch is a list of ready proxies - just use them
            for proxy in batch:
                if not hasattr(self, '_failed_proxies_global'):
                    self._failed_proxies_global = set()
                if proxy not in self._failed_proxies_global:
                    return {"http": proxy, "https": proxy}
            logger.warning("All rest pool proxies failed")
            return None
        
        # Main batch path
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
        logger.info("Batch %d exhausted (all %d proxies failed); marking exhausted", 
                   batch_idx + 1, len(validated))
        self._mark_batch_exhausted(batch_idx)
        return self._get_next_proxy()

    def _mark_batch_exhausted(self, batch_index: int) -> None:
        """Mark a batch as exhausted. Move ONLY successful proxies to rest pool. Update progress."""
        self._batch_states[batch_index] = "exhausted"
        
        # Only successful proxies (got 200) go to rest pool
        successful = self._batch_successful_proxies.get(batch_index, [])
        if successful:
            rest_until = time.time() + self._rest_duration_seconds
            for proxy in successful:
                self._rest_pool.append((proxy, rest_until))
            logger.info("Batch %d exhausted: %d successful proxies moved to rest pool (cooldown %.0fs)", 
                       batch_index + 1, len(successful), self._rest_duration_seconds)
        else:
            logger.info("Batch %d exhausted: no successful proxies to rest", batch_index + 1)
        
        # Discard failed proxies permanently
        failed_count = len(self._batch_failed_proxies.get(batch_index, set()))
        if failed_count:
            logger.debug("Batch %d: %d failed proxies discarded", batch_index + 1, failed_count)
        
        # Update progress: this batch is now consumed
        self._consumed_batches = max(self._consumed_batches, batch_index + 1)
        self._save_progress()
        
        # Remove consumed batch from proxies file
        self._update_proxies_file(batch_index)
        
        self._current_batch_index += 1

    def _mark_batch_rate_limited(self, batch_index: int) -> None:
        """Mark remaining proxies in batch as failed so they are skipped, but don't exhaust the batch yet.
        This allows the normal rotation to try other proxies in the batch before moving on."""
        # Mark all remaining validated proxies as failed (they'll be skipped by rotation)
        validated = self._batch_validated.get(batch_index, [])
        failed_set = self._batch_failed_proxies.get(batch_index, set())
        successful = self._batch_successful_proxies.get(batch_index, [])
        
        for proxy in validated:
            if proxy not in successful:
                failed_set.add(proxy)
        self._batch_failed_proxies[batch_index] = failed_set
        
        logger.debug("Batch %d: marked %d remaining proxies as failed (will be skipped)", 
                    batch_index + 1, len(failed_set))

    def _mark_proxy_successful(self, proxy_dict: dict[str, str] | None) -> None:
        """Mark a proxy as successful (got 200) in the current batch or rest pool."""
        if proxy_dict:
            proxy_url = proxy_dict.get("http")
            if proxy_url:
                if self._main_batches_exhausted:
                    # Using rest pool - track successes globally
                    if not hasattr(self, '_successful_proxies_global'):
                        self._successful_proxies_global = set()
                    self._successful_proxies_global.add(proxy_url)
                    logger.debug("Marked rest pool proxy as successful: %s", proxy_url)
                else:
                    # Using main batch
                    batch_idx = self._current_batch_index
                    if batch_idx < len(self._all_batches):
                        if proxy_url not in self._batch_successful_proxies[batch_idx]:
                            self._batch_successful_proxies[batch_idx].append(proxy_url)
                            logger.debug("Marked proxy as successful in batch %d: %s", batch_idx + 1, proxy_url)

    def _mark_proxy_failed(self, proxy_dict: dict[str, str] | None) -> None:
        """Mark a proxy as failed in the current batch or rest pool."""
        if proxy_dict:
            proxy_url = proxy_dict.get("http")
            if proxy_url:
                if self._main_batches_exhausted:
                    # Using rest pool - track failures globally
                    if not hasattr(self, '_failed_proxies_global'):
                        self._failed_proxies_global = set()
                    self._failed_proxies_global.add(proxy_url)
                    logger.debug("Marked rest pool proxy as failed: %s", proxy_url)
                else:
                    # Using main batch
                    batch_idx = self._current_batch_index
                    if batch_idx < len(self._all_batches):
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
        # Ensure backup file exists
        self._ensure_backup_file()

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
                # Apply proxy on first attempt and retries
                if self._all_batches:
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
                    # Mark proxy as successful for rest pool
                    self._mark_proxy_successful(current_proxy)
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

                # 429 = server rate limit - mark proxy as exhausted and rotate to next
                if status_code == 429 and attempt < self.max_retries:
                    logger.warning("Rate limited (429) for %s; marking proxy exhausted, rotating (attempt %d/%d)", 
                                  current_user.username, attempt + 1, self.max_retries)
                    if current_proxy and self._all_batches:
                        self._mark_proxy_failed(current_proxy)
                    
                    # Track 429s per batch - if multiple proxies in same batch get 429, skip the batch
                    batch_idx = self._current_batch_index
                    if batch_idx < len(self._all_batches):
                        if not hasattr(self, '_batch_429_count'):
                            self._batch_429_count = {}
                        self._batch_429_count[batch_idx] = self._batch_429_count.get(batch_idx, 0) + 1
                        
                        # If 2+ proxies in same batch get 429, skip the whole batch
                        if self._batch_429_count[batch_idx] >= 2:
                            logger.warning("Batch %d has %d rate-limited proxies; skipping batch", 
                                          batch_idx + 1, self._batch_429_count[batch_idx])
                            self._mark_batch_rate_limited(batch_idx)
                    continue
                
                # Other retryable server errors (5xx) - wait and retry
                if status_code in {500, 502, 503, 504, 522, 524} and attempt < self.max_retries:
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
                # Only mark proxy as failed on connection/SSL errors (proxy itself is broken)
                # Timeouts and 429s are NOT proxy failures - they're server/network conditions
                is_proxy_failure = isinstance(exc, (
                    requests.exceptions.ConnectTimeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.SSLError,
                ))
                if current_proxy and self._all_batches and is_proxy_failure:
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