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
    assert payload["scope"] == "all registered Voice Studio workers"
    assert [row["repo"] for row in payload["models"]] == [
        model_baselines.WHISPER_TINY_REPO,
        model_baselines.KOKORO_REPO,
        model_baselines.VIBEVOICE_REPO,
        model_baselines.FISH_AUDIO_REPO,
    ]


@pytest.mark.asyncio
async def test_reconcile_skips_non_voice_and_accepts_all_missing_models(monkeypatch, authed):
    voice = _voice("voice")
    monitor.registry = [voice, {**voice, "id": "image", "modality": "image"}]
    monitor.status = {"voice": {"status": "up"}, "image": {"status": "up"}}

    async def transcription(studio, force=False):
        return {"models": [{"repo": model_baselines.WHISPER_TINY_REPO,
                             "cached": False}]}

    async def catalog(studio, force=False):
        return {"models": [
            {"repo": model_baselines.KOKORO_REPO, "cache": {"state": "absent"}},
            {"repo": model_baselines.VIBEVOICE_REPO, "cache": {"state": "absent"}},
            {"repo": model_baselines.FISH_AUDIO_REPO, "cache": {"state": "absent"}},
        ]}

    class Response:
        def raise_for_status(self): return None
        def __init__(self, repo): self.repo = repo
        def json(self):
            return {"job": {"id": f"download-{self.repo.rsplit('/', 1)[-1]}",
                            "state": "queued"}}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs):
            assert kwargs["json"]["repo"] in {
                model_baselines.WHISPER_TINY_REPO,
                model_baselines.KOKORO_REPO,
                model_baselines.VIBEVOICE_REPO,
                model_baselines.FISH_AUDIO_REPO,
            }
            return Response(kwargs["json"]["repo"])

    monkeypatch.setattr(monitor, "get_transcription", transcription)
    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(model_baselines.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(model_baselines.peers, "studio_request",
                        lambda studio, path: ("http://voice/api/downloads", {"X-Studio-Token": "x"}))

    response = authed.post("/api/hub/model-baselines/reconcile")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {"total": 4, "cached": 0, "pending": 4, "failed": 0}
    assert {row["model_repo"] for row in payload["targets"]} == {
        model_baselines.WHISPER_TINY_REPO,
        model_baselines.KOKORO_REPO,
        model_baselines.VIBEVOICE_REPO,
        model_baselines.FISH_AUDIO_REPO,
    }
    assert all(row["job_id"].startswith("download-") for row in payload["targets"])


@pytest.mark.asyncio
async def test_reconcile_does_not_redownload_cached_models(monkeypatch, authed):
    monitor.registry = [_voice("voice")]
    monitor.status = {"voice": {"status": "up"}}

    async def transcription(studio, force=False):
        return {"models": [{"repo": model_baselines.WHISPER_TINY_REPO,
                             "cached": True}]}

    async def catalog(studio, force=False):
        return {"models": [
            {"repo": model_baselines.KOKORO_REPO, "cache": {"state": "cached"}},
            {"repo": model_baselines.VIBEVOICE_REPO, "cache": {"state": "cached"}},
            {"repo": model_baselines.FISH_AUDIO_REPO, "cache": {"state": "cached"}},
        ]}

    monkeypatch.setattr(monitor, "get_transcription", transcription)
    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(model_baselines.httpx, "AsyncClient",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("download not expected")))

    response = authed.post("/api/hub/model-baselines/reconcile")
    assert response.status_code == 200
    assert response.json()["summary"]["cached"] == 4


@pytest.mark.asyncio
async def test_offline_voice_retains_every_required_model_for_retry(authed):
    monitor.registry = [_voice("voice@offline", machine="offline")]
    monitor.status = {"voice@offline": {"status": "down"}}

    response = authed.post("/api/hub/model-baselines/reconcile")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {"total": 4, "cached": 0, "pending": 4, "failed": 0}
    assert {row["state"] for row in payload["targets"]} == {"offline"}
    assert all("retrying automatically" in row["detail"]
               for row in payload["targets"])


def test_disabled_baseline_remains_visible(authed):
    response = authed.post("/api/hub/model-baselines", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
