import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app/frontend/index.html"


def _source() -> str:
    return FRONTEND.read_text()


def test_swarm_batch_has_operator_label_and_accessible_clone_picker():
    source = _source()

    assert 'id="j-label"' in source
    assert 'id="j-voice-field" hidden' in source
    assert 'id="j-clone-voice" aria-label="Shared cloned voice"' in source
    assert 'id="j-status" role="status" aria-live="polite"' in source


def test_swarm_batch_sends_label_and_shared_voice_contract():
    source = _source()

    assert 'label: $("#j-label").value.trim() || "studiohub-ui"' in source
    assert "voice_library_id: selectedVoice" in source
    assert 'language: voice?.language || "en"' in source
    assert "ref_transcript: voice.transcript" in source
    assert 'modality === "voice" ? { text: value } : { prompt: value }' in source


def test_clone_picker_reports_sync_and_limits_supported_models():
    source = _source()

    assert "function jobModelSupportsCloning(repo)" in source
    assert "/api/hub/shared-voices" in source
    assert "Macs ready" in source
    assert "The broker sends this clone only to Macs where its reference is synchronized." in source


def test_agent_hub_update_button_does_not_add_a_second_browser_confirmation():
    source = _source()
    start = source.index("async function startHubUpdate(machines = null)")
    end = source.index("function updateReadyHubs()", start)

    assert "confirm(" not in source[start:end]
    assert 'hubUpdateBusy = true' in source[start:end]
    assert 'renderHubUpdate(job)' in source[start:end]


def test_dashboard_ids_are_unique_so_live_refreshes_do_not_replace_controls():
    ids = re.findall(r'\bid="([^"]+)"', _source())

    assert len(ids) == len(set(ids))
