"""Config / model broadcaster — push one thing to many studios at once.

- Model downloads: fan out to each studio's own `POST /api/downloads`
  (identical schema across the family: {repo, token?}). Works for local AND
  remote registry entries since it goes over HTTP.
- Environment variables: rewrite the KEY=value line in each local studio's
  ENVIRONMENT file (append if missing). File-level by necessity — that's where
  Pinokio reads env from. Local studios only, and the studio must be
  restarted for the change to take effect (we report that back).
"""

import re

import httpx

from .registry import base_url
from .peers import studio_headers
from .control import PINOKIO_HOME

# Guard the env broadcaster against writing outside a studio's own folder.
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


async def broadcast_download(
    client: httpx.AsyncClient, studios: list[dict], repo: str,
    token: str | None = None,
) -> dict:
    results = {}
    for s in studios:
        try:
            body = {"repo": repo}
            if token:
                body["token"] = token
            r = await client.post(
                f"{base_url(s)}/api/downloads", json=body,
                headers=studio_headers(s), timeout=15.0)
            payload = r.json() if r.status_code < 500 else {}
            results[s["id"]] = {
                "ok": r.status_code < 400,
                "status": r.status_code,
                "detail": payload.get("detail"),
                "job": (payload.get("job") or {}).get("id"),
            }
        except httpx.HTTPError as e:
            results[s["id"]] = {"ok": False, "error": str(e)}
    return results


def broadcast_env(studios: list[dict], key: str, value: str) -> dict:
    if not _KEY_RE.match(key):
        return {"error": f"invalid env key: {key!r} (UPPER_SNAKE_CASE only)"}
    results = {}
    for s in studios:
        if s.get("machine", "local") != "local" or not s.get("app"):
            results[s["id"]] = {"ok": False, "error": "local studios only"}
            continue
        env_file = PINOKIO_HOME / "api" / s["app"] / "ENVIRONMENT"
        try:
            lines = env_file.read_text().splitlines() if env_file.exists() else []
            pattern = re.compile(rf"^{re.escape(key)}=")
            replaced = False
            for i, line in enumerate(lines):
                if pattern.match(line):
                    lines[i] = f"{key}={value}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"{key}={value}")
            env_file.write_text("\n".join(lines) + "\n")
            results[s["id"]] = {"ok": True, "action": "replaced" if replaced else "appended"}
        except OSError as e:
            results[s["id"]] = {"ok": False, "error": str(e)}
    return {
        "results": results,
        "note": "restart each studio for the change to take effect",
    }
