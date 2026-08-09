from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app/frontend/index.html"


def _source() -> str:
    return FRONTEND.read_text()


def test_swarm_batch_has_operator_label_and_accessible_clone_picker():
    source = _source()

    assert 'id="j-label"' in source
    assert 'id="j-voice-field" hidden' in source
    assert 'id="j-voice" aria-label="Shared cloned voice"' in source
    assert 'id="j-status" role="status" aria-live="polite"' in source


def test_swarm_batch_sends_label_and_shared_voice_contract():
    source = _source()

    assert 'label: $("#j-label").value.trim() || "studiohub-ui"' in source
    assert "voice_library_id: selectedVoice" in source
    assert "ref_transcript: voice.transcript" in source
    assert 'modality === "voice" ? { text: value } : { prompt: value }' in source


def test_clone_picker_reports_sync_and_limits_supported_models():
    source = _source()

    assert "function jobModelSupportsCloning(repo)" in source
    assert "/api/hub/shared-voices" in source
    assert "Macs ready" in source
    assert "The broker sends this clone only to Macs where its reference is synchronized." in source
