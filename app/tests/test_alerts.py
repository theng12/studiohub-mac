import pytest

from backend import alerts


def test_emit_records_to_ring(reset):
    alerts.emit("test", "hello", {"x": 1})
    r = alerts.recent()
    assert r[0]["kind"] == "test" and r[0]["message"] == "hello"
    assert r[0]["data"] == {"x": 1}


def test_config_roundtrip(reset):
    assert alerts.load_config() == {}
    alerts.set_config({"webhook": "http://x/y", "desktop": True})
    assert alerts.load_config()["webhook"] == "http://x/y"


def test_studio_down_and_recovered_transitions(monitor):
    img = next(s for s in monitor.registry if s["id"] == "image")
    # first observation (unknown -> down) must NOT alert
    monitor._note_transition(img, "unknown", "down")
    assert alerts.recent() == []
    # up -> down alerts
    monitor._note_transition(img, "up", "down")
    assert alerts.recent()[0]["kind"] == "studio_down"
    # down -> up alerts recovery
    monitor._note_transition(img, "down", "up")
    assert alerts.recent()[0]["kind"] == "studio_recovered"


def test_no_alert_when_status_unchanged(monitor):
    img = next(s for s in monitor.registry if s["id"] == "image")
    monitor._note_transition(img, "up", "up")
    assert alerts.recent() == []


def test_alerts_api(authed):
    assert authed.get("/api/hub/alerts").json()["config"] == {}
    authed.post("/api/hub/alerts", json={"webhook": "http://h/cb", "desktop": True})
    d = authed.get("/api/hub/alerts").json()
    assert d["config"]["webhook"] == "http://h/cb" and d["config"]["desktop"] is True


@pytest.mark.asyncio
async def test_batch_failure_emits_alert(reset):
    import httpx
    from backend import broker
    b = {"id": "bf", "modality": "image", "model": "a/b", "created_at": 1.0,
         "cancelled": False, "webhook": None,
         "items": [{"index": 0, "state": "error", "error": "boom",
                    "artifact_url": None, "artifact_path": None, "asset_id": None}]}
    async with httpx.AsyncClient() as c:
        await broker._maybe_finish(c, b)
    kinds = [e["kind"] for e in alerts.recent()]
    assert "batch_failed" in kinds
