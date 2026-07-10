"""Token auth — local stays frictionless, remote requires the Hub token.

Trust model (SPEC §7 exposure decision):
- Requests from loopback (this Mac) need no token: the Pinokio webview, local
  scripts and the local dashboard keep working untouched.
- Requests from anywhere else (LAN / Tailscale) must present the token via
  `Authorization: Bearer <token>`, `X-Hub-Token: <token>`, or `?token=`.
- The static dashboard page itself is served without a token; its API calls
  are what get checked (the page prompts for the token on first 401).

The token is generated once and persisted to `.hub_token` at the launcher root
(gitignored — machine state). Rotate by deleting the file and restarting.
"""

import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse

from .registry import LAUNCHER_ROOT

from .registry import DATA_DIR
TOKEN_FILE = DATA_DIR / ".hub_token"

# Paths any client may hit without a token.
PUBLIC_PATHS = {"/", "/api/health", "/api/version"}


def load_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token + "\n")
    return token


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def presented_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get("x-hub-token")
    if header:
        return header.strip()
    return request.query_params.get("token")


def make_middleware(token: str):
    from . import peers

    async def middleware(request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or is_loopback(request):
            return await call_next(request)
        offered = presented_token(request)
        if offered is not None:
            if secrets.compare_digest(offered, token):
                return await call_next(request)
            # Fleet token: lets peer Hubs on the tailnet authenticate as a fleet.
            fleet = peers.fleet_token()
            if fleet and secrets.compare_digest(offered, fleet):
                return await call_next(request)
        return JSONResponse(
            {"detail": "Hub token required for remote access. "
                       "Open the dashboard on the Hub machine to see the token."},
            status_code=401,
        )
    return middleware
