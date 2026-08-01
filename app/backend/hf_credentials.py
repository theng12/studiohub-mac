"""Durable Hugging Face credential metadata for Studio Hub.

The token itself lives in the macOS Keychain, never in the Hub state JSON.
The JSON file contains only non-secret delivery metadata so an interrupted
broadcast can be retried without asking the operator to visit every machine.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from typing import Any

from .registry import DATA_DIR


KEYCHAIN_SERVICE = "GenStudio KH Studio Hub Credential"
KEYCHAIN_ACCOUNT = "huggingface"
STATE_FILE = DATA_DIR / "hf_credentials.json"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _security_binary() -> str:
    if sys.platform != "darwin":
        raise RuntimeError("Secure Hugging Face storage requires macOS Keychain.")
    path = shutil.which("security") or "/usr/bin/security"
    if not Path(path).exists():
        raise RuntimeError("macOS security tool is unavailable on this Hub.")
    return path


def _keychain_read() -> str | None:
    result = subprocess.run(
        [_security_binary(), "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", KEYCHAIN_ACCOUNT, "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _keychain_write(token: str) -> None:
    result = subprocess.run(
        [_security_binary(), "add-generic-password", "-U", "-s",
         KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w", token],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or "Keychain rejected the credential."
        raise RuntimeError(detail)


def _keychain_delete() -> None:
    subprocess.run(
        [_security_binary(), "delete-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", KEYCHAIN_ACCOUNT],
        capture_output=True, text=True, check=False,
    )


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_state(value: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
        os.chmod(STATE_FILE, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _safe_detail(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    # Do not allow a future HTTP/client error to echo a credential.
    for marker in ("hf_", "token=", "Authorization:", "Bearer "):
        index = text.lower().find(marker.lower())
        if index >= 0:
            text = text[:index] + "[redacted]"
    return text[:500]


def _public_state(state: dict[str, Any], configured: bool | None = None) -> dict[str, Any]:
    deliveries = state.get("deliveries")
    if not isinstance(deliveries, dict):
        deliveries = {}
    return {
        "configured": bool(_keychain_read()) if configured is None else configured,
        "credential_id": state.get("credential_id"),
        "updated_at": state.get("updated_at"),
        "last_broadcast_at": state.get("last_broadcast_at"),
        "pending_count": sum(1 for row in deliveries.values()
                              if isinstance(row, dict)
                              and row.get("status") in {"pending", "retryable"}),
        "deliveries": deliveries,
    }


def status() -> dict[str, Any]:
    state = _read_state()
    try:
        configured = bool(_keychain_read())
    except RuntimeError:
        configured = False
    return _public_state(state, configured)


def get_token() -> str | None:
    return _keychain_read()


def save_token(token: str) -> dict[str, Any]:
    value = str(token or "").strip()
    if not value:
        raise ValueError("A Hugging Face token is required.")
    if len(value) > 512:
        raise ValueError("The Hugging Face token is too long.")
    if not value.startswith("hf_"):
        raise ValueError("Hugging Face tokens should start with hf_.")
    _keychain_write(value)
    state = _read_state()
    state.update({
        "schema_version": 1,
        "credential_id": state.get("credential_id") or f"hfcred_{secrets.token_hex(8)}",
        "updated_at": _now(),
    })
    _write_state(state)
    return _public_state(state, True)


def clear_token() -> dict[str, Any]:
    _keychain_delete()
    state = _read_state()
    state.update({"schema_version": 1, "credential_id": None,
                  "updated_at": _now(), "last_broadcast_at": None,
                  "deliveries": {}})
    _write_state(state)
    return _public_state(state, False)


def record_delivery(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    state = _read_state()
    deliveries = state.get("deliveries")
    if not isinstance(deliveries, dict):
        deliveries = {}
    attempted = _now()
    for studio_id, result in results.items():
        status_code = result.get("status")
        if result.get("ok"):
            state_name = "delivered"
        elif status_code in {404, 405}:
            state_name = "unsupported"
        elif status_code in {401, 403}:
            state_name = "failed"
        else:
            state_name = "retryable"
        previous = deliveries.get(studio_id, {})
        attempts = int(previous.get("attempts") or 0) + 1 if isinstance(previous, dict) else 1
        deliveries[studio_id] = {
            "status": state_name,
            "attempts": attempts,
            "last_attempt_at": attempted,
            "http_status": status_code,
            "detail": _safe_detail(result.get("detail") or result.get("error")),
        }
    state.update({"schema_version": 1, "deliveries": deliveries,
                  "last_broadcast_at": attempted})
    _write_state(state)
    return _public_state(state, True)
