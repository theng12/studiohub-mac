"""Simple, private enrollment for Studio Hub controllers and agents.

Enrollment changes only site-local identity, the shared site fleet credential,
and the local hardware-profile assignment.  It never creates PostgreSQL
credentials or any global job, lease, billing, retry, or fencing authority.
"""

from __future__ import annotations

import hashlib
import ipaddress
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
CODE_TTL_SECONDS = 10 * 60
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
             controller_id TEXT NOT NULL
           )"""
    )
    return connection


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_enrollment_code(*, now: float | None = None) -> dict:
    """Return one high-entropy code while persisting only its digest."""
    settings = control_plane.load_settings()
    if settings["role"] != "controller":
        raise ValueError("Enrollment codes can be created only by a location controller.")
    issued_at = float(time.time() if now is None else now)
    expires_at = issued_at + CODE_TTL_SECONDS
    code = secrets.token_urlsafe(32)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM enrollment_codes WHERE expires_at < ? AND used_at IS NOT NULL",
            (issued_at - 24 * 60 * 60,),
        )
        connection.execute(
            """INSERT INTO enrollment_codes
                 (id, code_hash, created_at, expires_at, site_id, controller_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (secrets.token_hex(12), _code_hash(code), issued_at, expires_at,
             settings["site_id"], settings["controller_id"]),
        )
        connection.commit()
    return {
        "code": code,
        "expires_at": expires_at,
        "expires_in_seconds": CODE_TTL_SECONDS,
        "site": {
            "id": settings["site_id"],
            "name": settings["site_name"],
            "controller_id": settings["controller_id"],
        },
    }


def claim_enrollment_code(code: str, *, now: float | None = None) -> dict:
    """Atomically consume a code and return the minimum private site config."""
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
            "Site fleet credential is ready for one-time agent enrollment",
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
    for suffix in ("", "-wal", "-shm"):
        Path(f"{DB_FILE}{suffix}").unlink(missing_ok=True)
