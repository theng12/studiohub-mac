import json

import pytest

from backend import main


class FakeReq:
    def __init__(self, disconnected=False):
        self._d = disconnected

    async def is_disconnected(self):
        return self._d


def test_summary_has_alerts_active(authed):
    d = authed.get("/api/hub/summary").json()
    assert "alerts_active" in d and isinstance(d["alerts_active"], int)


@pytest.mark.asyncio
async def test_sse_generator_emits_summary(reset):
    gen = main._sse_summary(FakeReq(), interval=0.01)
    try:
        chunk = await gen.__anext__()
    finally:
        await gen.aclose()
    assert chunk.startswith("data: ")
    payload = json.loads(chunk[6:])
    assert "studios" in payload and "alerts_active" in payload


@pytest.mark.asyncio
async def test_sse_generator_stops_on_disconnect(reset):
    gen = main._sse_summary(FakeReq(disconnected=True))
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()  # disconnected → yields nothing


def test_sse_requires_auth(client):
    # non-loopback without token → 401 BEFORE the stream starts (no hang).
    assert client.get("/api/hub/stream").status_code == 401
