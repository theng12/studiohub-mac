"""Simple, private enrollment for Studio Hub controllers and agents.

Enrollment changes only site-local identity, the shared site fleet credential,
and the local hardware-profile assignment.  It never creates PostgreSQL
credentials or any global job, lease, billing, retry, or fencing authority.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import control_plane, hardware_profiles, peers
from .registry import DATA_DIR

DB_FILE = DATA_DIR / "setup_enrollment.db"
ENROLLMENT_CODE_FILE = DATA_DIR / ".enrollment_code"
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


def _connect() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    DB_FILE.touch(mode=0o600, exist_ok=True)
    os.chmod(DB_FILE, 0o600)
    connection = sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS enrollment_codes (
             id TEXT PRIMARY KEY,
             code_hash TEXT NOT NULL UNIQUE,
             created_at REAL NOT NULL,
             expires_at REAL NOT NULL,
             used_at REAL,
             site_id TEXT NOT NULL,
             controller_id TEXT NOT NULL,
             kind TEXT NOT NULL DEFAULT 'legacy_one_time',
             revoked_at REAL,
             last_used_at REAL,
             use_count INTEGER NOT NULL DEFAULT 0
           )"""
    )
    columns = {
        row["name"] for row in connection.execute(
            "PRAGMA table_info(enrollment_codes)"
        ).fetchall()
    }
    migrations = {
        "kind": "TEXT NOT NULL DEFAULT 'legacy_one_time'",
        "revoked_at": "REAL",
        "last_used_at": "REAL",
        "use_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in migrations.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE enrollment_codes ADD COLUMN {name} {definition}"
            )
    return connection


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_enrollment_code(*, now: float | None = None) -> dict:
    """Create or rotate the permanent site enrollment credential.

    The database remains the claim authority and stores only a digest.  A
    private mode-0600 file keeps the controller-owned value revealable after a
    dashboard reload, matching the existing Hub and fleet credential model.
    """
    settings = control_plane.load_settings()
    if settings["role"] != "controller":
        raise ValueError("Enrollment codes can be created only by a location controller.")
    issued_at = float(time.time() if now is None else now)
    code = secrets.token_urlsafe(32)
    credential_id = secrets.token_hex(12)
    saved_file = _snapshot((ENROLLMENT_CODE_FILE,))
    try:
        _replace_private(
            ENROLLMENT_CODE_FILE,
            (json.dumps({"id": credential_id, "code": code}, separators=(",", ":"))
             + "\n").encode("utf-8"),
        )
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE enrollment_codes SET revoked_at = ?
                   WHERE kind = 'permanent' AND revoked_at IS NULL""",
                (issued_at,),
            )
            connection.execute(
                """INSERT INTO enrollment_codes
                     (id, code_hash, created_at, expires_at, site_id, controller_id,
                      kind, revoked_at, last_used_at, use_count)
                   VALUES (?, ?, ?, 0, ?, ?, 'permanent', NULL, NULL, 0)""",
                (credential_id, _code_hash(code), issued_at,
                 settings["site_id"], settings["controller_id"]),
            )
            connection.commit()
    except Exception:
        _restore(saved_file)
        raise
    return {
        "code": code,
        "permanent": True,
        "expires_at": None,
        "created_at": issued_at,
        "site": {
            "id": settings["site_id"],
            "name": settings["site_name"],
            "controller_id": settings["controller_id"],
        },
    }


def claim_enrollment_code(code: str, *, now: float | None = None) -> dict:
    """Validate a permanent or legacy code and return private site config."""
    settings = control_plane.load_settings()
    if settings["role"] != "controller":
        raise ValueError("This Hub is not a location controller.")
    candidate = str(code or "").strip()
    if not _CODE_PATTERN.fullmatch(candidate):
        raise ValueError("Enrollment code is invalid.")
    claimed_at = float(time.time() if now is None else now)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM enrollment_codes WHERE code_hash = ?",
            (_code_hash(candidate),),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise ValueError("Enrollment code is invalid.")
        permanent = row["kind"] == "permanent"
        if permanent:
            if row["revoked_at"] is not None:
                connection.rollback()
                raise ValueError("Enrollment code has been revoked.")
            if (row["site_id"] != settings["site_id"]
                    or row["controller_id"] != settings["controller_id"]):
                connection.rollback()
                raise ValueError("Enrollment code belongs to an older site identity; rotate it.")
        else:
            if row["used_at"] is not None:
                connection.rollback()
                raise ValueError("Enrollment code has already been used.")
            if float(row["expires_at"]) <= claimed_at:
                connection.rollback()
                raise ValueError("Enrollment code has expired.")
        token = peers.fleet_token()
        if not token:
            connection.rollback()
            raise ValueError("The controller has no site fleet credential.")
        if permanent:
            changed = connection.execute(
                """UPDATE enrollment_codes
                   SET last_used_at = ?, use_count = use_count + 1
                   WHERE id = ? AND revoked_at IS NULL""",
                (claimed_at, row["id"]),
            ).rowcount
        else:
            changed = connection.execute(
                """UPDATE enrollment_codes SET used_at = ?
                   WHERE id = ? AND used_at IS NULL AND expires_at > ?""",
                (claimed_at, row["id"], claimed_at),
            ).rowcount
        if changed != 1:
            connection.rollback()
            raise ValueError("Enrollment code could not be claimed.")
        connection.commit()
    return {
        "schema_version": 1,
        "site_id": settings["site_id"],
        "site_name": settings["site_name"],
        "controller_id": settings["controller_id"],
        "fleet_token": token,
    }


def enrollment_credential_status(*, include_code: bool = False) -> dict:
    """Return the current permanent credential without exposing it by default."""
    settings = control_plane.load_settings()
    if settings["role"] != "controller":
        return {"active": False, "permanent": True, "code": None}
    with _connect() as connection:
        row = connection.execute(
            """SELECT * FROM enrollment_codes
               WHERE kind = 'permanent' AND revoked_at IS NULL
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        return {"active": False, "permanent": True, "code": None}
    code = None
    if include_code:
        try:
            saved = json.loads(ENROLLMENT_CODE_FILE.read_text(encoding="utf-8"))
            candidate = str(saved.get("code") or "")
            if (saved.get("id") == row["id"]
                    and _CODE_PATTERN.fullmatch(candidate)
                    and secrets.compare_digest(_code_hash(candidate), row["code_hash"])):
                os.chmod(ENROLLMENT_CODE_FILE, 0o600)
                code = candidate
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return {
        "active": True,
        "permanent": True,
        "code": code,
        "revealable": code is not None,
        "created_at": float(row["created_at"]),
        "last_used_at": (float(row["last_used_at"])
                         if row["last_used_at"] is not None else None),
        "use_count": int(row["use_count"] or 0),
    }


def revoke_enrollment_credential(*, now: float | None = None) -> dict:
    """Revoke the permanent enrollment credential immediately."""
    settings = control_plane.load_settings()
    if settings["role"] != "controller":
        raise ValueError("Enrollment codes can be revoked only by a location controller.")
    revoked_at = float(time.time() if now is None else now)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """UPDATE enrollment_codes SET revoked_at = ?
               WHERE kind = 'permanent' AND revoked_at IS NULL""",
            (revoked_at,),
        ).rowcount
        connection.commit()
    ENROLLMENT_CODE_FILE.unlink(missing_ok=True)
    return {"ok": True, "revoked": bool(changed), "active": False,
            "permanent": True, "code": None}


def _allowed_private_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or (address.version == 4 and address in TAILSCALE_NETWORK)
    ) and not (address.is_multicast or address.is_unspecified)


def private_request_host(host: str | None) -> bool:
    return _allowed_private_ip(str(host or ""))


def validate_private_controller_url(value: str) -> str:
    """Validate a credential-free private HTTP controller base URL."""
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid controller URL.") from exc
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Controller URL must use private HTTP.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Controller URL must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Controller URL must be the Hub base address without a path.")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = sorted({
                result[4][0]
                for result in socket.getaddrinfo(hostname, port or 80, type=socket.SOCK_STREAM)
            })
        except OSError as exc:
            raise ValueError("Controller hostname could not be resolved on this private network.") from exc
    if not addresses or not all(_allowed_private_ip(address) for address in addresses):
        raise ValueError("Controller URL must resolve only to loopback, LAN, or Tailscale addresses.")
    host_display = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{host_display}{f':{port}' if port else ''}"


def _safe_fragment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned[:56] or fallback


def suggested_local_hub_id(profile_id: str) -> str:
    profile = hardware_profiles.hardware_profile(profile_id)
    if profile is None:
        raise ValueError(f"unknown hardware profile {profile_id!r}")
    hostname = _safe_fragment(socket.gethostname().split(".", 1)[0], "mac")
    prefix = profile["machine_prefix"]
    fingerprint = hashlib.sha256(
        f"{hostname}:{uuid.getnode()}".encode("utf-8")
    ).hexdigest()[:8]
    stem = hostname if hostname.startswith(prefix) else f"{prefix}-{hostname}"
    stem = f"{stem[:87]}-{fingerprint}"
    return f"{stem[:96]}-hub"[:100]


def suggested_site_id(location_name: str) -> str:
    return _safe_fragment(location_name, "new-location")


def _snapshot(paths: tuple[Path, ...]) -> dict[Path, bytes | None]:
    values: dict[Path, bytes | None] = {}
    for path in paths:
        try:
            values[path] = path.read_bytes()
        except FileNotFoundError:
            values[path] = None
    return values


def _replace_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            _replace_private(path, content)
    control_plane.reload_settings_from_disk()
    hardware_profiles._assignment_cache = None


def _configuration_paths() -> tuple[Path, ...]:
    return (
        control_plane.SETTINGS_FILE,
        control_plane.DATABASE_URL_FILE,
        peers.FLEET_TOKEN_FILE,
        peers.SHARED_STUDIO_TOKEN_FILE,
        hardware_profiles.MACHINE_PROFILES_FILE,
    )


def _ensure_setup_is_not_environment_locked() -> None:
    locked = [name for name in (
        "STUDIOHUB_ROLE", "STUDIOHUB_SITE_ID", "STUDIOHUB_SITE_NAME",
        "STUDIOHUB_CONTROLLER_ID", "STUDIOHUB_DATABASE_MODE",
        "STUDIOHUB_DATABASE_URL", "STUDIOHUB_FLEET_TOKEN",
    ) if os.environ.get(name)]
    if locked:
        raise ValueError(
            "Simple setup is unavailable while Studio Hub identity or credentials "
            "are fixed by environment variables. Remove the advanced environment "
            "configuration first."
        )


def configure_new_controller(location_name: str, site_id: str,
                             hardware_profile_id: str) -> dict:
    _ensure_setup_is_not_environment_locked()
    profile = hardware_profiles.hardware_profile(hardware_profile_id)
    if profile is None:
        raise ValueError(f"unknown hardware profile {hardware_profile_id!r}")
    current = control_plane.load_settings()
    controller_id = (
        current["controller_id"]
        if current["role"] == "controller" and current["site_id"] == site_id
        else suggested_local_hub_id(hardware_profile_id)
    )
    snapshot = _snapshot(_configuration_paths())
    try:
        saved = control_plane.save_settings({
            "role": "controller",
            "site_id": site_id,
            "site_name": location_name,
            "controller_id": controller_id,
            "database_mode": "off",
        }, clear_database_url=True)
        profile = hardware_profiles.set_machine_hardware_profile(
            "local", hardware_profile_id)
        peers.fleet_token()
    except Exception:
        _restore(snapshot)
        raise
    return {
        "ok": True,
        "mode": "controller",
        "settings": saved,
        "hardware_profile": profile,
        "checklist": [
            "Location controller role saved",
            "Local SQLite scheduler remains authoritative",
            "PostgreSQL and global claiming remain off",
            "Local hardware profile assigned",
            "Site fleet credential is ready for permanent-code agent enrollment",
        ],
    }


def _validated_claim(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Controller returned an unsupported enrollment response.")
    site_id = str(value.get("site_id") or "").strip().lower()
    site_name = str(value.get("site_name") or "").strip()
    controller_id = str(value.get("controller_id") or "").strip().lower()
    fleet_token = str(value.get("fleet_token") or "").strip()
    if not control_plane.ID_PATTERN.fullmatch(site_id):
        raise ValueError("Controller returned an invalid site ID.")
    if not 1 <= len(site_name) <= 120:
        raise ValueError("Controller returned an invalid site name.")
    if not control_plane.ID_PATTERN.fullmatch(controller_id):
        raise ValueError("Controller returned an invalid controller ID.")
    if not 12 <= len(fleet_token) <= 512:
        raise ValueError("Controller returned an invalid site fleet credential.")
    return {
        "site_id": site_id,
        "site_name": site_name,
        "controller_id": controller_id,
        "fleet_token": fleet_token,
    }


async def claim_remote(controller_url: str, code: str) -> dict:
    base_url = validate_private_controller_url(controller_url)
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        try:
            response = await client.post(
                f"{base_url}/api/hub/enrollment/claim",
                json={"code": str(code or "").strip()},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise ValueError("The location controller could not be reached over the private link.") from exc
    try:
        payload = response.json() if response.headers.get("content-type", "").startswith(
            "application/json") else {}
    except ValueError as exc:
        raise ValueError("Controller returned an invalid enrollment response.") from exc
    if response.status_code >= 400:
        raise ValueError(str(payload.get("detail") or "Enrollment code was rejected."))
    return _validated_claim(payload)


def configure_joined_agent(controller_url: str, hardware_profile_id: str,
                           claim: dict) -> dict:
    _ensure_setup_is_not_environment_locked()
    profile = hardware_profiles.hardware_profile(hardware_profile_id)
    if profile is None:
        raise ValueError(f"unknown hardware profile {hardware_profile_id!r}")
    base_url = validate_private_controller_url(controller_url)
    values = _validated_claim({"schema_version": 1, **claim})
    current = control_plane.load_settings()
    agent_id = (
        current["controller_id"]
        if current["role"] == "agent" and current["site_id"] == values["site_id"]
        else suggested_local_hub_id(hardware_profile_id)
    )
    snapshot = _snapshot(_configuration_paths())
    try:
        saved = control_plane.save_settings({
            "role": "agent",
            "site_id": values["site_id"],
            "site_name": values["site_name"],
            "controller_id": agent_id,
            "database_mode": "off",
            "parent_controller_url": base_url,
        }, clear_database_url=True)
        peers.set_fleet_token(values["fleet_token"])
        profile = hardware_profiles.set_machine_hardware_profile(
            "local", hardware_profile_id)
    except Exception:
        _restore(snapshot)
        raise
    return {
        "ok": True,
        "mode": "agent",
        "settings": saved,
        "hardware_profile": profile,
        "checklist": [
            "Joined the controller over a private link",
            "Agent role and location identity saved",
            "Site fleet credential stored in owner-only files",
            "PostgreSQL and customer submission remain disabled",
            "Local hardware profile assigned",
        ],
    }


def reset_for_tests() -> None:
    ENROLLMENT_CODE_FILE.unlink(missing_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{DB_FILE}{suffix}").unlink(missing_ok=True)
