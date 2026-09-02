"""The dashboard page is ~540 KB and no-store, so it is re-sent in full on
every load. It must be gzipped for clients that accept it — sites on slow
uplinks were paying the whole size each time."""
import gzip


def test_index_is_gzipped_when_accepted(client):
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["vary"].lower() == "accept-encoding"
    # no-store must survive: Pinokio's webview serves stale builds without it
    assert "no-store" in r.headers["cache-control"]
    assert b"<html" in r.content.lower()


def test_index_plain_when_gzip_not_accepted(client):
    r = client.get("/", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert "no-store" in r.headers["cache-control"]
    assert b"<html" in r.content.lower()


def test_gzip_body_matches_plain_body(client):
    plain = client.get("/", headers={"Accept-Encoding": "identity"}).content
    r = client.build_request("GET", "/", headers={"Accept-Encoding": "gzip"})
    raw = client.send(r)
    # httpx decodes transparently; compare against an explicit recompression
    assert raw.content == plain
    assert len(gzip.compress(plain, 6)) < len(plain) / 2, "should compress >2x"


def test_streaming_routes_are_not_compressed(client):
    """Guard the reason this is a route fix and not GZipMiddleware: blanket
    compression buffers text/event-stream and stalls the live summary."""
    r = client.get("/api/health", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") != "gzip"
