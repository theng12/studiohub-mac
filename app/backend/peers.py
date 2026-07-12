"""Peer Hubs — the fleet layer.

Each Mac runs its own Studio Hub. A Hub is the local authority for its machine:
it alone can read that machine's host RAM, per-studio process memory, and run
`pterm` to start/stop its studios. So to see a remote Mac's specs or control its
servers, the primary Hub asks THAT machine's Hub.

A "peer" is derived automatically from any remote studio entry: the peer Hub
lives at http://<studio.host>:<hub_port default 47873>. No separate peer list —
if you registered a machine's studios, its Hub is reachable at the same host.

Auth across Hubs uses a shared **fleet token**: set the same token on every
Mac's Hub (dashboard → Remote, or STUDIOHUB_FLEET_TOKEN env). Each Hub accepts
it; the primary presents it when calling peers. Recursion is prevented with
?local_only=true so a peer returns only its own machine and never fans back out.

Peer resource data is cached (short TTL) and refreshed from the monitor poll
loop, so the 5s dashboard poll never blocks on slow/offline peers.
"""

import asyncio
import os
import secrets
import time
from urllib.parse import urlsplit

import httpx

from .registry import DATA_DIR

FLEET_TOKEN_FILE = DATA_DIR / ".fleet_token"
DEFAULT_HUB_PORT = 47873
PEER_TTL_S = 12.0
PEER_TIMEOUT_S = 5.0

# machine -> (ts, {"host": {...}|None, "studios": {modality: stats}, "reachable": bool})
_cache: dict[str, tuple[float, dict]] = {}


def fleet_token() -> str | None:
    env = os.environ.get("STUDIOHUB_FLEET_TOKEN")
    if env and env.strip():
        return env.strip()
    if FLEET_TOKEN_FILE.exists():
        os.chmod(FLEET_TOKEN_FILE, 0o600)
        t = FLEET_TOKEN_FILE.read_text().strip()
        if t:
            return t
    token = secrets.token_urlsafe(24)
    set_fleet_token(token)
    return token


def set_fleet_token(token: str):
    value = (token or "").strip() or secrets.token_urlsafe(24)
    FLEET_TOKEN_FILE.write_text(value + "\n")
    os.chmod(FLEET_TOKEN_FILE, 0o600)


def studio_headers(studio: dict | None = None) -> dict[str, str]:
    token = (studio or {}).get("studio_token") or fleet_token()
    return {"X-Studio-Token": token} if token else {}


def _peer_url(studio: dict) -> str:
    return f"http://{studio['host']}:{studio.get('hub_port', DEFAULT_HUB_PORT)}"


def _peer_token(studio: dict) -> str | None:
    return studio.get("hub_token") or fleet_token()


def studio_request(studio: dict, path_or_url: str) -> tuple[str, dict[str, str]]:
    """Return the safest URL + credentials for a Studio API request.

    A connected peer Hub is the local authority for its machine. Routing remote
    Studio traffic through that Hub also survives a Studio process that still
    has an older fleet token in memory: the peer reaches its own Studio over
    loopback, while this Hub authenticates to the peer with the shared Hub
    token. If no connected peer is available, retain the direct Studio path.
    """
    parsed = urlsplit(path_or_url)
    path = parsed.path.lstrip("/")
    if parsed.query:
        path += "?" + parsed.query
    machine = studio.get("machine", "local")
    peer = cached(machine) if machine != "local" else None
    if peer and peer.get("status") == "connected":
        token = _peer_token(studio)
        headers = {"X-Hub-Token": token} if token else {}
        return f"{_peer_url(studio)}/studio/{studio['modality']}/{path}", headers
    direct = path_or_url if parsed.scheme in {"http", "https"} else (
        f"http://{studio['host']}:{studio['port']}/{path}")
    return direct, studio_headers(studio)


def _remote_machines(registry: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for s in registry:
        if s.get("machine", "local") != "local":
            out.setdefault(s["machine"], []).append(s)
    return out


# Guard so overlapping refreshes never pile up (the poll loop fires this every
# few seconds; a slow/offline fleet must not stack N in-flight refreshes).
_inflight = {"v": False}


async def refresh(registry: list[dict], client: httpx.AsyncClient):
    """Refresh each remote machine's host+studio stats from its peer Hub.
    TTL-guarded so it only actually hits the network every PEER_TTL_S. Safe to
    fire-and-forget: an in-flight guard prevents overlap, and per-peer errors
    are swallowed so one slow machine can't break the sweep."""
    if _inflight["v"]:
        return
    machines = _remote_machines(registry)
    now = time.time()
    stale = [m for m, studios in machines.items()
             if now - _cache.get(m, (0, None))[0] >= PEER_TTL_S]
    if not stale:
        return
    _inflight["v"] = True
    try:
        await _refresh_stale(machines, stale, client, now)
    finally:
        _inflight["v"] = False


async def _refresh_stale(machines, stale, client, now):

    async def one(machine: str, studios: list[dict]):
        s0 = studios[0]
        url = _peer_url(s0)
        token = _peer_token(s0)
        headers = {"X-Hub-Token": token} if token else {}
        try:
            r = await client.get(f"{url}/api/hub/resources?local_only=true",
                                  headers=headers, timeout=PEER_TIMEOUT_S)
            if r.status_code == 401:
                # Hub is reachable but rejected the token → clearest possible signal
                # that the fleet tokens don't match on that machine.
                _cache[machine] = (now, {"host": None, "studios": {},
                                        "reachable": True, "auth": False,
                                        "status": ("no_token" if not token
                                                   else "token_rejected")})
                return
            data = r.json()
            _cache[machine] = (now, {
                "host": data.get("host"),
                "studios": data.get("studios", {}),
                "reachable": True, "auth": True, "status": "connected",
            })
        except httpx.ConnectError:
            # TCP refused: nothing is listening on :47873 there — the Studio Hub
            # isn't actually running on that machine (even if its studios are).
            _cache[machine] = (now, {"host": None, "studios": {},
                                    "reachable": False, "auth": True,
                                    "status": "no_hub"})
        except (httpx.TimeoutException, httpx.ConnectTimeout):
            # Packets dropped: a firewall is blocking :47873, or the Mac is asleep/off.
            _cache[machine] = (now, {"host": None, "studios": {},
                                    "reachable": False, "auth": True,
                                    "status": "unreachable"})
        except Exception:
            _cache[machine] = (now, {"host": None, "studios": {},
                                    "reachable": False, "auth": True,
                                    "status": "unreachable"})

    await asyncio.gather(*(one(m, machines[m]) for m in stale))


def cached(machine: str) -> dict | None:
    entry = _cache.get(machine)
    return entry[1] if entry else None


async def control_remote(client: httpx.AsyncClient, studio: dict, action: str) -> dict:
    """Proxy a start/stop to the studio's own machine's Hub, which runs pterm
    locally there. The peer addresses the studio by its local id = modality."""
    url = _peer_url(studio)
    token = _peer_token(studio)
    if not token:
        return {"ok": False, "error": "no fleet token set — set one on this Hub "
                "and the same on the remote Hub to control remote studios"}
    headers = {"X-Hub-Token": token}
    local_id = studio.get("modality")
    try:
        r = await client.post(f"{url}/api/hub/studios/{local_id}/{action}",
                              headers=headers, timeout=10.0)
        if r.status_code == 401:
            return {"ok": False, "error": "remote Hub rejected the fleet token "
                    "(set the SAME fleet token on that machine's Hub)"}
        if r.status_code == 404:
            return {"ok": False, "error": f"remote Hub has no '{local_id}' studio "
                    "(is a Studio Hub running on that machine?)"}
        return {"ok": r.status_code < 400, "status": r.status_code,
                "remote": r.json() if r.headers.get("content-type", "").startswith("application/json") else None}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"can't reach the Hub on {studio['host']} "
                f"— run Studio Hub on that Mac ({e})"}
