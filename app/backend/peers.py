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
import time

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
        t = FLEET_TOKEN_FILE.read_text().strip()
        return t or None
    return None


def set_fleet_token(token: str):
    FLEET_TOKEN_FILE.write_text((token or "").strip() + "\n")


def _peer_url(studio: dict) -> str:
    return f"http://{studio['host']}:{studio.get('hub_port', DEFAULT_HUB_PORT)}"


def _peer_token(studio: dict) -> str | None:
    return studio.get("hub_token") or fleet_token()


def _remote_machines(registry: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for s in registry:
        if s.get("machine", "local") != "local":
            out.setdefault(s["machine"], []).append(s)
    return out


async def refresh(registry: list[dict], client: httpx.AsyncClient):
    """Refresh each remote machine's host+studio stats from its peer Hub.
    TTL-guarded so it only actually hits the network every PEER_TTL_S."""
    machines = _remote_machines(registry)
    now = time.time()
    stale = [m for m, studios in machines.items()
             if now - _cache.get(m, (0, None))[0] >= PEER_TTL_S]
    if not stale:
        return

    async def one(machine: str, studios: list[dict]):
        s0 = studios[0]
        url = _peer_url(s0)
        headers = {"X-Hub-Token": _peer_token(s0)} if _peer_token(s0) else {}
        try:
            r = await client.get(f"{url}/api/hub/resources?local_only=true",
                                  headers=headers, timeout=PEER_TIMEOUT_S)
            if r.status_code == 401:
                _cache[machine] = (now, {"host": None, "studios": {},
                                        "reachable": True, "auth": False})
                return
            data = r.json()
            _cache[machine] = (now, {
                "host": data.get("host"),
                "studios": data.get("studios", {}),
                "reachable": True, "auth": True,
            })
        except Exception:
            # Peer Hub not running / machine offline — reachable=False means the
            # studios may still answer health directly, we just have no Hub there.
            _cache[machine] = (now, {"host": None, "studios": {},
                                    "reachable": False, "auth": True})

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
