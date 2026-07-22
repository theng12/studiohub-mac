from pathlib import Path

import pytest

from backend import model_baselines
from backend.main import monitor


def test_model_baseline_runtime_state_is_ignored_by_git() -> None:
    root = Path(__file__).parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "model_baselines.json" in ignored


def _voice(studio_id: str, machine: str = "local") -> dict:
    return {
        "id": studio_id,
        "modality": "voice",
        "machine": machine,
        "host": "127.0.0.1",
        "port": 47870,
    }


def test_model_baseline_endpoint_is_authenticated_and_scoped(authed, client):
    assert client.get("/api/hub/model-baselines").status_code == 401
    response = authed.get("/api/hub/model-baselines")
    assert response.status_code == 200
    payload = response.json()
    assert payload["repo"] == model_baselines.WHISPER_TINY_REPO
    assert payload["scope"] == "voice-studio transcription workers only"


@pytest.mark.asyncio
async def test_reconcile_skips_non_voice_and_accepts_missing_tiny(monkeypatch, authed):
    voice = _voice("voice")
    monitor.registry = [voice, {**voice, "id": "image", "modality": "image"}]
    monitor.status = {"voice": {"status": "up"}, "image": {"status": "up"}}

    async def transcription(studio, force=False):
        return {"models": [{"repo": model_baselines.WHISPER_TINY_REPO,
                             "cached": False}]}

    class Response:
        def raise_for_status(self): return None
        def json(self): return {"job": {"id": "download-1", "state": "queued"}}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs):
            assert kwargs["json"] == {"repo": model_baselines.WHISPER_TINY_REPO}
            return Response()

    monkeypatch.setattr(monitor, "get_transcription", transcription)
    monkeypatch.setattr(model_baselines.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(model_baselines.peers, "studio_request",
                        lambda studio, path: ("http://voice/api/downloads", {"X-Studio-Token": "x"}))

    response = authed.post("/api/hub/model-baselines/reconcile")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {"total": 1, "cached": 0, "pending": 1, "failed": 0}
    assert payload["targets"][0]["job_id"] == "download-1"


@pytest.mark.asyncio
async def test_reconcile_does_not_redownload_cached_tiny(monkeypatch, authed):
    monitor.registry = [_voice("voice")]
    monitor.status = {"voice": {"status": "up"}}

    async def transcription(studio, force=False):
        return {"models": [{"repo": model_baselines.WHISPER_TINY_REPO,
                             "cached": True}]}

    monkeypatch.setattr(monitor, "get_transcription", transcription)
    monkeypatch.setattr(model_baselines.httpx, "AsyncClient",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("download not expected")))

    response = authed.post("/api/hub/model-baselines/reconcile")
    assert response.status_code == 200
    assert response.json()["summary"]["cached"] == 1


def test_disabled_baseline_remains_visible(authed):
    response = authed.post("/api/hub/model-baselines", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
