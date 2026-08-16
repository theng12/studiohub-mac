"""Target-side validation and durable pre-apply enrollment repair journal."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import threading
import time
import weakref
from contextlib import asynccontextmanager, contextmanager
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import control_plane, peers
from .controller_settings_lock import SettingsWriterBusy, settings_writer_lock
from .enrollment_repair_transport import (
    PinnedTransportError,
    ResolvedOrigin,
    canonical_private_address,
    open_pinned_json,
    resolve_private_origin,
)
from .registry import DATA_DIR


JOURNAL_FILE = DATA_DIR / ".enrollment_repair_journal.json"
CALLBACK_TIMEOUT_SECONDS = 5.0
AGENT_STATES = (
    "accepted", "redemption_attempted", "applying",
    "complete", "never_applied", "needs_review",
)
_UNRESOLVED_STATES = {"accepted", "redemption_attempted", "applying"}
_IDENTITY_ENV = (
    "STUDIOHUB_ROLE", "STUDIOHUB_SITE_ID", "STUDIOHUB_SITE_NAME",
    "STUDIOHUB_CONTROLLER_ID",
)
_DISPATCH_KEYS = {
    "schema", "schema_version", "request_id", "target_machine_id", "ticket",
    "redemption_expires_at", "controller_url", "controller",
}
_CONTROLLER_KEYS = {"site_id", "site_name", "controller_id"}
_CLAIM_KEYS = {
    "schema", "schema_version", "request_id", "target_machine_id", "role",
    "site_id", "site_name", "controller_id",
}
_URLSAFE_TICKET = re.compile(r"^[A-Za-z0-9_-]{43,256}$")
_journal_process_locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_journal_process_locks_guard = threading.Lock()


def _journal_process_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _journal_process_locks_guard:
        return _journal_process_locks.setdefault(loop, asyncio.Lock())


class RepairExecutorError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class JournalLockBusy(RuntimeError):
    pass


def _bounded(value: Any, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not minimum <= len(value) <= maximum:
        raise RepairExecutorError("dispatch_invalid")
    return value


def _ticket(value: Any) -> str:
    if not isinstance(value, str) or not _URLSAFE_TICKET.fullmatch(value):
        raise RepairExecutorError("dispatch_invalid")
    return value


def _source_address(value: str) -> str:
    try:
        return canonical_private_address(value)
    except ValueError as exc:
        raise RepairExecutorError("callback_source_mismatch") from exc


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _dispatch_digest(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RepairExecutorError("dispatch_invalid") from exc
    return _hash(encoded)


def _saved_parent(value: Any, dispatch_origin: str) -> tuple[dict[str, Any], str | None]:
    if value is None or value == "":
        return {
            "present": False,
            "status": "missing",
            "matches_dispatch": False,
        }, None
    if not isinstance(value, str) or value != value.strip() or len(value) > 500:
        return {"present": True, "status": "invalid", "matches_dispatch": False}, None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return {"present": True, "status": "invalid", "matches_dispatch": False}, None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        return {"present": True, "status": "invalid", "matches_dispatch": False}, None
    host = parsed.hostname.lower()
    display = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = "" if port in {None, default_port} else f":{port}"
    origin = f"{parsed.scheme}://{display}{suffix}"
    return {
        "present": True,
        "status": "valid",
        "origin": origin,
        "matches_dispatch": hmac.compare_digest(origin, dispatch_origin),
    }, origin


@asynccontextmanager
async def _journal_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                await asyncio.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def _journal_file_lock_sync(path: Path, *, blocking: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, flags)
        except OSError as exc:
            if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise JournalLockBusy from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class RepairExecutor:
    def __init__(
        self,
        *,
        journal_path: Path = JOURNAL_FILE,
        settings_path: Path = control_plane.SETTINGS_FILE,
        clock: Callable[[], float] = time.time,
        origin_resolver: Callable[[str], ResolvedOrigin] = resolve_private_origin,
        connection_factory: Callable[[ResolvedOrigin], Any] = open_pinned_json,
    ) -> None:
        self.journal_path = Path(journal_path)
        self.settings_path = Path(settings_path)
        self.clock = clock
        self.origin_resolver = origin_resolver
        self.connection_factory = connection_factory
        self.journal_lock_path = self.journal_path.with_name(self.journal_path.name + ".lock")

    def _load_journal(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RepairExecutorError("journal_invalid") from exc
        if not isinstance(value, dict) or value.get("state") not in AGENT_STATES:
            raise RepairExecutorError("journal_invalid")
        state = value["state"]
        has_ticket = "ticket" in value
        has_claim = isinstance(value.get("claim"), dict)
        if state == "redemption_attempted":
            valid = (
                value.get("outcome") == "unknown"
                and isinstance(value.get("ticket"), str)
                and _URLSAFE_TICKET.fullmatch(value["ticket"]) is not None
                and not has_claim
            )
        elif state in {"accepted", "applying"}:
            valid = value.get("outcome") == "accepted" and has_claim and not has_ticket
        else:
            valid = not has_ticket and not has_claim
        if not valid:
            raise RepairExecutorError("journal_invalid")
        return value

    def _save_journal(self, value: Mapping[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.journal_path.name}.", dir=self.journal_path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.journal_path)
            os.chmod(self.journal_path, 0o600)
            directory = os.open(self.journal_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _expire(self, journal: dict[str, Any]) -> dict[str, Any]:
        expires_at = journal.get("redemption_expires_at")
        if (journal.get("state") in {"accepted", "redemption_attempted"}
                and journal.get("outcome") != "accepted"
                and isinstance(expires_at, (int, float))
                and float(self.clock()) >= float(expires_at)):
            journal = dict(journal)
            journal["state"] = "never_applied"
            journal.pop("outcome", None)
            journal.pop("error_code", None)
            journal.pop("ticket", None)
            journal.pop("claim", None)
            journal["updated_at"] = float(self.clock())
            self._save_journal(journal)
        return journal

    @staticmethod
    def _result(journal: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "request_id": str(journal.get("request_id", "")),
            "state": str(journal.get("state", "needs_review")),
        }
        if journal.get("outcome") == "unknown":
            result["outcome"] = "unknown"
        if journal.get("error_code"):
            result["error_code"] = str(journal["error_code"])
        if journal.get("state") == "complete" and isinstance(journal.get("identity"), Mapping):
            result["identity"] = dict(journal["identity"])
            if isinstance(journal.get("applied_at"), (int, float)):
                result["applied_at"] = float(journal["applied_at"])
        return result

    def _review(self, journal: Mapping[str, Any], code: str) -> dict[str, Any]:
        return self._terminal(journal, "needs_review", error_code=code)

    @staticmethod
    def _error(request_id: str, code: str, *, state: str = "needs_review") -> dict[str, Any]:
        return {"request_id": request_id, "state": state, "error_code": code}

    def _parse_dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != _DISPATCH_KEYS:
            raise RepairExecutorError("dispatch_invalid")
        if (payload.get("schema") != "studiohub.enrollment-repair-dispatch"
                or type(payload.get("schema_version")) is not int
                or payload.get("schema_version") != 1):
            raise RepairExecutorError("dispatch_invalid")
        controller = payload.get("controller")
        if not isinstance(controller, Mapping) or set(controller) != _CONTROLLER_KEYS:
            raise RepairExecutorError("dispatch_invalid")
        expires = payload.get("redemption_expires_at")
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(float(expires))):
            raise RepairExecutorError("dispatch_invalid")
        return {
            "request_id": _bounded(payload.get("request_id"), 16, 128),
            "target_machine_id": _bounded(payload.get("target_machine_id"), 1, 100),
            "ticket": _ticket(payload.get("ticket")),
            "redemption_expires_at": float(expires),
            "controller_url": _bounded(payload.get("controller_url"), 1, 500),
            "controller": {
                "site_id": _bounded(controller.get("site_id"), 1, 100),
                "site_name": _bounded(controller.get("site_name"), 1, 120),
                "controller_id": _bounded(controller.get("controller_id"), 1, 100),
            },
        }

    def _read_settings(self) -> tuple[bytes, dict[str, Any]]:
        try:
            raw = self.settings_path.read_bytes()
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RepairExecutorError("settings_invalid") from exc
        if not isinstance(parsed, dict):
            raise RepairExecutorError("settings_invalid")
        return raw, parsed

    def _checkpoint(self, _site: str) -> None:
        """Test-only failure/pause seam around the single settings replacement."""

    def _stage_settings_file(self, content: bytes) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.settings_path.name}.", dir=self.settings_path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                self._checkpoint("stage_write")
                stream.write(content)
                stream.flush()
                self._checkpoint("file_fsync")
                os.fsync(stream.fileno())
            return temporary
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    def _replace_settings_file(self, staged: Path) -> None:
        self._checkpoint("replace")
        os.replace(staged, self.settings_path)

    def _fsync_settings_directory(self) -> None:
        self._checkpoint("directory_fsync")
        descriptor = os.open(self.settings_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _identity_from_claim(claim: Mapping[str, Any]) -> dict[str, str]:
        return {
            "role": str(claim.get("role", "")),
            "site_id": str(claim.get("site_id", "")),
            "site_name": str(claim.get("site_name", "")),
            "controller_id": str(claim.get("controller_id", "")),
        }

    def _terminal(
        self,
        journal: Mapping[str, Any],
        state: str,
        *,
        error_code: str | None = None,
        identity: Mapping[str, Any] | None = None,
        applied_at: float | None = None,
    ) -> dict[str, Any]:
        updated = dict(journal)
        updated["state"] = state
        updated.pop("outcome", None)
        updated.pop("ticket", None)
        updated.pop("claim", None)
        if error_code is None:
            updated.pop("error_code", None)
        else:
            updated["error_code"] = error_code
        if identity is not None:
            updated["identity"] = dict(identity)
        if applied_at is not None:
            updated["applied_at"] = float(applied_at)
        updated["updated_at"] = float(self.clock())
        self._save_journal(updated)
        return self._result(updated)

    def _classify_current(
        self,
        journal: Mapping[str, Any],
        *,
        reload_cache: bool = False,
    ) -> dict[str, Any]:
        try:
            current = self.settings_path.read_bytes()
        except OSError:
            return self._terminal(
                journal, "needs_review", error_code="settings_state_ambiguous"
            )
        current_hash = _hash(current)
        staged_hash = journal.get("settings_staged_sha256")
        preimage_hash = journal.get("settings_preimage_sha256")
        if isinstance(staged_hash, str) and hmac.compare_digest(current_hash, staged_hash):
            claim = journal.get("claim")
            identity = (
                self._identity_from_claim(claim)
                if isinstance(claim, Mapping)
                else journal.get("identity")
            )
            if not isinstance(identity, Mapping):
                return self._terminal(
                    journal, "needs_review", error_code="settings_state_ambiguous"
                )
            if reload_cache:
                control_plane.reload_settings_cache()
            return self._terminal(
                journal,
                "complete",
                identity=identity,
                applied_at=float(journal.get("apply_started_at", self.clock())),
            )
        if isinstance(preimage_hash, str) and hmac.compare_digest(current_hash, preimage_hash):
            return self._terminal(journal, "never_applied")
        return self._terminal(
            journal, "needs_review", error_code="settings_state_ambiguous"
        )

    async def apply(
        self,
        payload: Mapping[str, Any],
        *,
        direct_source: str,
    ) -> dict[str, Any]:
        async with _journal_process_lock():
            async with _journal_file_lock(self.journal_lock_path):
                return await self._apply(payload, direct_source=direct_source)

    async def _apply(
        self,
        payload: Mapping[str, Any],
        *,
        direct_source: str,
    ) -> dict[str, Any]:
        request_id = payload.get("request_id", "") if isinstance(payload, Mapping) else ""
        request_id = request_id if isinstance(request_id, str) and len(request_id) <= 128 else ""
        try:
            existing = self._load_journal()
            if existing is not None:
                existing = self._expire(existing)
                if existing.get("state") in _UNRESOLVED_STATES:
                    if not hmac.compare_digest(str(existing.get("request_id", "")), request_id):
                        return self._error(request_id, "request_conflict")
                    if not hmac.compare_digest(
                        str(existing.get("dispatch_digest", "")), _dispatch_digest(payload)
                    ):
                        return self._error(request_id, "request_conflict")
                    if _source_address(direct_source) != existing.get("controller_address"):
                        return self._error(request_id, "callback_source_mismatch")
                    if existing.get("state") != "redemption_attempted":
                        return self._result(existing)
                elif hmac.compare_digest(str(existing.get("request_id", "")), request_id):
                    return self._result(existing)
                elif existing.get("state") in {"complete", "needs_review"}:
                    return self._error(request_id, "request_conflict")

            parsed = self._parse_dispatch(payload)
            if parsed["redemption_expires_at"] <= float(self.clock()):
                return self._error(request_id, "ticket_expired", state="never_applied")
            callback_deadline = time.monotonic() + CALLBACK_TIMEOUT_SECONDS
            try:
                origin = await asyncio.wait_for(
                    asyncio.to_thread(self.origin_resolver, parsed["controller_url"]),
                    timeout=max(0.0, callback_deadline - time.monotonic()),
                )
            except TimeoutError:
                return self._error(request_id, "transport_unavailable")
            except Exception:
                return self._error(request_id, "callback_url_invalid")
            source = _source_address(direct_source)
            if source != origin.address:
                return self._error(request_id, "callback_source_mismatch")
            if any(os.environ.get(name) for name in _IDENTITY_ENV):
                return self._error(request_id, "environment_locked")
            settings_bytes, saved_settings = self._read_settings()
            settings = {**control_plane.defaults(), **saved_settings}
            database_mode = os.environ.get(
                "STUDIOHUB_DATABASE_MODE", str(settings.get("database_mode", "off"))
            ).strip().lower()
            if database_mode != "off":
                return self._error(request_id, "database_mode_unsafe")
            saved_parent, observed_parent = _saved_parent(
                settings.get("parent_controller_url"), origin.origin
            )
            observed = {
                "role": str(settings.get("role", ""))[:20],
                "site_id": str(settings.get("site_id", ""))[:100],
                "site_name": str(settings.get("site_name", ""))[:120],
                "controller_id": str(settings.get("controller_id", ""))[:100],
                "parent_controller_url": observed_parent,
            }
            now = float(self.clock())
            if existing is None or existing.get("state") != "redemption_attempted":
                journal = {
                    "schema": "studiohub.enrollment-repair-journal",
                    "schema_version": 1,
                    "request_id": parsed["request_id"],
                    "target_machine_id": parsed["target_machine_id"],
                    "controller": parsed["controller"],
                    "controller_origin": origin.origin,
                    "controller_address": origin.address,
                    "redemption_expires_at": parsed["redemption_expires_at"],
                    "dispatch_digest": _dispatch_digest(payload),
                    "settings_preimage_sha256": _hash(settings_bytes),
                    "observed_identity": observed,
                    "audit": {"saved_parent": saved_parent},
                    "state": "redemption_attempted",
                    "outcome": "unknown",
                    "ticket": parsed["ticket"],
                    "created_at": now,
                    "updated_at": now,
                }
            else:
                journal = dict(existing)
            journal["state"] = "redemption_attempted"
            journal["outcome"] = "unknown"
            journal.pop("error_code", None)
            journal["updated_at"] = float(self.clock())
            self._save_journal(journal)

            token = peers.current_fleet_token()
            if token is None:
                journal["error_code"] = "fleet_token_unavailable"
                self._save_journal(journal)
                return self._result(journal)
            redemption = {
                "schema": "studiohub.enrollment-repair-redemption",
                "schema_version": 1,
                "request_id": parsed["request_id"],
                "target_machine_id": parsed["target_machine_id"],
                "ticket": journal["ticket"],
                "redemption_expires_at": parsed["redemption_expires_at"],
                "observed_identity": observed,
            }
            try:
                remaining = callback_deadline - time.monotonic()
                if remaining <= 0:
                    raise PinnedTransportError("transport_timeout")

                async def redeem():
                    async with self.connection_factory(origin) as connection:
                        status, response = await connection.request_json(
                            "POST",
                            "/api/hub/enrollment-repair-tickets/redeem",
                            headers={"X-Hub-Token": token},
                            body=redemption,
                            timeout=remaining,
                        )
                        return status, response, _source_address(connection.direct_peer)

                status, response, peer = await asyncio.wait_for(redeem(), timeout=remaining)
            except TimeoutError:
                journal["error_code"] = "transport_unavailable"
                journal["updated_at"] = float(self.clock())
                self._save_journal(journal)
                return self._result(journal)
            except PinnedTransportError as exc:
                if str(exc) == "callback_source_mismatch":
                    return self._review(journal, "callback_source_mismatch")
                journal["error_code"] = "transport_unavailable"
                journal["updated_at"] = float(self.clock())
                self._save_journal(journal)
                return self._result(journal)
            except Exception:
                journal["error_code"] = "transport_unavailable"
                journal["updated_at"] = float(self.clock())
                self._save_journal(journal)
                return self._result(journal)
            if peer != origin.address:
                return self._review(journal, "callback_source_mismatch")
            if 300 <= status < 400:
                return self._review(journal, "callback_url_invalid")
            if status != 200:
                journal["error_code"] = "transport_unavailable"
                self._save_journal(journal)
                return self._result(journal)
            expected_claim = {
                "schema": "studiohub.enrollment-repair-claim",
                "schema_version": 1,
                "request_id": parsed["request_id"],
                "target_machine_id": parsed["target_machine_id"],
                "role": "agent",
                "site_id": parsed["controller"]["site_id"],
                "site_name": parsed["controller"]["site_name"],
                "controller_id": parsed["target_machine_id"],
            }
            if (not isinstance(response, Mapping)
                    or set(response) != _CLAIM_KEYS
                    or type(response.get("schema_version")) is not int
                    or response != expected_claim):
                return self._review(journal, "claim_invalid")
            try:
                with settings_writer_lock():
                    current, current_settings = self._read_settings()
                    if not hmac.compare_digest(_hash(current), journal["settings_preimage_sha256"]):
                        return self._review(journal, "settings_preimage_changed")
                    journal["state"] = "applying"
                    journal["outcome"] = "accepted"
                    journal["claim"] = expected_claim
                    journal["apply_started_at"] = float(self.clock())
                    journal.pop("ticket", None)
                    journal.pop("error_code", None)
                    journal["updated_at"] = float(self.clock())
                    self._save_journal(journal)

                    staged_settings = dict(current_settings)
                    staged_settings.update({
                        "role": "agent",
                        "site_id": expected_claim["site_id"],
                        "site_name": expected_claim["site_name"],
                        "controller_id": expected_claim["controller_id"],
                        "parent_controller_url": origin.origin,
                    })
                    staged_bytes = (
                        json.dumps(staged_settings, indent=2) + "\n"
                    ).encode("utf-8")
                    staged_path: Path | None = None
                    try:
                        staged_path = self._stage_settings_file(staged_bytes)
                        journal["settings_staged_sha256"] = _hash(staged_bytes)
                        journal["updated_at"] = float(self.clock())
                        self._save_journal(journal)

                        self._checkpoint("pre_replace_recheck")
                        final_preimage, _ = self._read_settings()
                        if not hmac.compare_digest(
                            _hash(final_preimage), journal["settings_preimage_sha256"]
                        ):
                            return self._review(journal, "settings_preimage_changed")
                        self._replace_settings_file(staged_path)
                        staged_path = None
                        self._fsync_settings_directory()
                        self._checkpoint("read_back")
                        read_back = self.settings_path.read_bytes()
                        if (read_back != staged_bytes
                                or not hmac.compare_digest(
                                    _hash(read_back), journal["settings_staged_sha256"]
                                )):
                            return self._review(journal, "settings_state_ambiguous")
                        self._checkpoint("after_read_back")
                        control_plane.reload_settings_cache()
                        return self._terminal(
                            journal,
                            "complete",
                            identity=self._identity_from_claim(expected_claim),
                            applied_at=journal["apply_started_at"],
                        )
                    except Exception:
                        return self._classify_current(journal, reload_cache=True)
                    finally:
                        if staged_path is not None:
                            staged_path.unlink(missing_ok=True)
            except SettingsWriterBusy:
                journal["state"] = "never_applied"
                journal.pop("outcome", None)
                journal.pop("ticket", None)
                journal["error_code"] = "settings_writer_busy"
                journal["updated_at"] = float(self.clock())
                self._save_journal(journal)
            return self._result(journal)
        except RepairExecutorError as exc:
            return self._error(request_id, exc.code)

    def status(self, request_id: str, *, direct_source: str) -> dict[str, Any]:
        try:
            with _journal_file_lock_sync(self.journal_lock_path, blocking=False):
                return self._status(request_id, direct_source=direct_source, expire=True)
        except JournalLockBusy:
            return self._status(request_id, direct_source=direct_source, expire=False)

    def _status(
        self, request_id: str, *, direct_source: str, expire: bool,
    ) -> dict[str, Any]:
        journal = self._load_journal()
        if journal is None or not hmac.compare_digest(str(journal.get("request_id", "")), request_id):
            return self._error(request_id, "request_not_found")
        try:
            if _source_address(direct_source) != journal.get("controller_address"):
                return self._error(request_id, "callback_source_mismatch")
        except RepairExecutorError as exc:
            return self._error(request_id, exc.code)
        return self._result(self._expire(journal) if expire else journal)

    def recover(self) -> dict[str, Any] | None:
        with _journal_file_lock_sync(self.journal_lock_path, blocking=True):
            with settings_writer_lock(blocking=True):
                journal = self._load_journal()
                if journal is None:
                    return None
                if journal.get("state") in {"accepted", "applying"}:
                    return self._classify_current(journal)
                return self._result(self._expire(journal))
