"""Unified gateway — one base URL that proxies to the right studio.

    {HUB}/studio/{id}/{path}  ->  http://{studio.host}:{studio.port}/{path}

This is the single address clients like Story Studio KH converge on instead of
storing five IPs. Responses stream through untouched (SSE job/download streams
included). Works for local AND remote registry entries, so a federated setup
still presents one address.

Intended for API traffic. Studio web UIs use absolute asset paths, so browsing
a UI *through* the gateway may not render — use the dashboard's "Open UI"
links (direct host:port) for that.
"""

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .registry import base_url
from .peers import studio_request

router = APIRouter()

# Hop-by-hop headers that must not be forwarded either direction.
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# No read timeout: generation and download streams can be quiet for minutes.
TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)

_client = httpx.AsyncClient(timeout=TIMEOUT)


def _monitor():
    from .main import monitor  # late import — main wires everything together
    return monitor


@router.api_route(
    "/studio/{studio_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(studio_id: str, path: str, request: Request):
    studio = next(
        (s for s in _monitor().registry if s["id"] == studio_id), None
    )
    if studio is None:
        return Response(f"unknown studio: {studio_id}", status_code=404)

    upstream, upstream_auth = studio_request(studio, f"/{path}")
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS
    }
    # Replace client-facing Hub credentials with the Studio fleet credential.
    headers.pop("authorization", None)
    headers.pop("x-hub-token", None)
    headers.update(upstream_auth)
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]

    req = _client.build_request(
        request.method, upstream,
        headers=headers, params=params, content=request.stream(),
    )
    try:
        upstream_resp = await _client.send(req, stream=True)
    except httpx.HTTPError as e:
        return Response(
            f"studio '{studio_id}' unreachable at {base_url(studio)} ({e})",
            status_code=502,
        )

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in HOP_HEADERS
    }
    # CRITICAL: close the upstream streamed response when this response finishes
    # (or the client disconnects), or the httpx connection leaks — over a long-
    # running service that exhausts the pool and hangs the gateway.
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )
