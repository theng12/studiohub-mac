"""Remote authentication for Studio Hub KH.

Trust model (SPEC §7 exposure decision):
- Requests from loopback (this Mac) need no token: the Pinokio webview, local
  scripts and the local dashboard keep working untouched.
- Requests from anywhere else (LAN / Tailscale) must present either a valid
  remembered-browser session or the Hub/fleet token.  Tokens remain necessary
  for peer Hubs, scripts, and recovery; people normally sign in with the owner
  password instead.
- The static dashboard page itself is served without a token; its API calls
  are what get checked (the page shows the sign-in screen on first 401).

The owner password is salted/scrypt-hashed. Browser sessions are random opaque
values whose hashes are stored locally, so neither password nor session can be
recovered from the Hub's state files.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from .registry import DATA_DIR
TOKEN_FILE = DATA_DIR / ".hub_token"
PASSWORD_FILE = DATA_DIR / ".hub_password.json"
SESSIONS_FILE = DATA_DIR / ".hub_sessions.json"

# Paths any client may hit without a token.
PUBLIC_PATHS = {"/", "/api/health", "/api/version", "/health/live",
                "/health/ready", "/health/capacity", "/api/auth/status",
                "/api/auth/login", "/api/auth/logout"}
COOKIE_NAME = "kh_hub_token"
SESSION_COOKIE_NAME = "kh_hub_session"
SESSION_TTL_DAYS = 90
SESSION_TTL_S = SESSION_TTL_DAYS * 24 * 60 * 60
_LOGIN_WINDOW_S = 15 * 60
_MAX_LOGIN_FAILURES = 5
_login_failures: dict[str, list[float]] = {}


def _write_private(path, value: dict) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    os.chmod(path, 0o600)


def _read_private(path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def load_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            os.chmod(TOKEN_FILE, 0o600)
            return token
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return token


def password_configured() -> bool:
    record = _read_private(PASSWORD_FILE, {})
    return all(isinstance(record.get(key), str) and record[key]
               for key in ("salt", "digest"))


def _password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=2**14, r=8, p=1, maxmem=64 * 1024 * 1024)


def clear_browser_sessions() -> None:
    _write_private(SESSIONS_FILE, {"sessions": []})


def set_owner_password(password: str) -> None:
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise ValueError("Enter a password.")
    salt = secrets.token_bytes(16)
    digest = _password_digest(password, salt)
    _write_private(PASSWORD_FILE, {
        "version": 1,
        "salt": salt.hex(),
        "digest": digest.hex(),
    })
    # Changing the password immediately removes remembered devices.
    clear_browser_sessions()


def verify_owner_password(password: str) -> bool:
    record = _read_private(PASSWORD_FILE, {})
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["digest"])
    except (KeyError, TypeError, ValueError):
        return False
    try:
        actual = _password_digest(password, salt)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _session_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _live_sessions() -> list[dict]:
    now = time.time()
    rows = _read_private(SESSIONS_FILE, {}).get("sessions", [])
    live = [row for row in rows if isinstance(row, dict)
            and isinstance(row.get("hash"), str)
            and isinstance(row.get("expires_at"), (int, float))
            and row["expires_at"] > now]
    if len(live) != len(rows):
        _write_private(SESSIONS_FILE, {"sessions": live})
    return live


def create_browser_session() -> str:
    value = secrets.token_urlsafe(32)
    sessions = _live_sessions()
    sessions.append({"hash": _session_hash(value),
                     "expires_at": int(time.time() + SESSION_TTL_S)})
    # Keep a sensible finite list if the owner signs in from many browsers.
    sessions = sessions[-20:]
    _write_private(SESSIONS_FILE, {"sessions": sessions})
    return value


def valid_browser_session(value: str | None) -> bool:
    if not value:
        return False
    candidate = _session_hash(value)
    return any(hmac.compare_digest(candidate, row["hash"])
               for row in _live_sessions())


def forget_browser_session(value: str | None) -> None:
    if not value:
        return
    candidate = _session_hash(value)
    sessions = [row for row in _live_sessions()
                if not hmac.compare_digest(candidate, row["hash"])]
    _write_private(SESSIONS_FILE, {"sessions": sessions})


def set_browser_session_cookie(response, value: str) -> None:
    response.set_cookie(SESSION_COOKIE_NAME, value, max_age=SESSION_TTL_S,
                        httponly=True, samesite="strict", path="/")


def clear_browser_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def login_allowed(request: Request) -> bool:
    key = request.client.host if request.client else "unknown"
    now = time.time()
    failures = [stamp for stamp in _login_failures.get(key, [])
                if stamp > now - _LOGIN_WINDOW_S]
    _login_failures[key] = failures
    return len(failures) < _MAX_LOGIN_FAILURES


def record_login_failure(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.time()
    _login_failures[key] = [stamp for stamp in _login_failures.get(key, [])
                            if stamp > now - _LOGIN_WINDOW_S] + [now]


def clear_login_failures(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    _login_failures.pop(key, None)


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def is_tailscale(request: Request) -> bool:
    """Whether a request arrived through the IPv4 Tailnet address space."""
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def presented_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get("x-hub-token")
    if header:
        return header.strip()
    cookie = request.cookies.get(COOKIE_NAME)
    return cookie.strip() if cookie else None


def presented_machine_token(request: Request) -> str | None:
    """Header-only credential for private service-to-service contracts."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header = request.headers.get("x-hub-token")
    return header.strip() if header else None


def valid_machine_token(request: Request, hub_token: str) -> bool:
    """Accept the Hub or fleet token; never a browser cookie or URL value."""
    offered = presented_machine_token(request)
    if not offered:
        return False
    if secrets.compare_digest(offered, hub_token):
        return True
    from . import peers
    fleet = peers.fleet_token()
    return bool(fleet and secrets.compare_digest(offered, fleet))


def make_middleware(token: str):
    from . import peers

    async def middleware(request: Request, call_next):
        # Local access stays passwordless, but an unrelated website opened in
        # the user's browser must not be able to mutate a loopback Hub. Native
        # clients do not send Origin; the Hub dashboard sends its own Host.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            origin_host = urlsplit(origin).netloc.lower()
            request_host = request.headers.get("host", "").lower()
            if not origin_host or origin_host != request_host:
                return JSONResponse(
                    {"detail": "Cross-origin browser writes are not allowed."},
                    status_code=403,
                )
        if request.url.path in PUBLIC_PATHS or is_loopback(request):
            return await call_next(request)
        if valid_browser_session(request.cookies.get(SESSION_COOKIE_NAME)):
            return await call_next(request)
        offered = presented_token(request)
        if offered is not None:
            if secrets.compare_digest(offered, token):
                response = await call_next(request)
                response.set_cookie(COOKIE_NAME, offered, httponly=True,
                                    samesite="strict")
                fleet = peers.fleet_token()
                if fleet:
                    response.set_cookie("kh_studio_token", fleet, httponly=True,
                                        samesite="strict")
                return response
            # Fleet token: lets peer Hubs on the tailnet authenticate as a fleet.
            fleet = peers.fleet_token()
            if fleet and secrets.compare_digest(offered, fleet):
                response = await call_next(request)
                response.set_cookie(COOKIE_NAME, offered, httponly=True,
                                    samesite="strict")
                response.set_cookie("kh_studio_token", fleet, httponly=True,
                                    samesite="strict")
                return response
        return JSONResponse(
            {"detail": "Hub token required for remote access. "
                       "Open the dashboard on the Hub machine to see the token."},
            status_code=401,
        )
    return middleware
