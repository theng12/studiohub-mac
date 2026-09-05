import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app/frontend/index.html"


def _source() -> str:
    return FRONTEND.read_text()


def _run_generation_helpers(expression: str):
    source = _source()
    marker = "function filterGenerationBatches("
    assert marker in source
    start = source.index(marker)
    end = source.index("function renderBatches(", start)
    script = source[start:end] + f"\nconsole.log(JSON.stringify({expression}));"
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def _run_resource_helper(expression: str):
    source = _source()
    start = source.index("function resourceUsageHTML(")
    end = source.index("// Render generation history", start)
    script = "const esc = value => String(value);\n" + source[start:end]
    script += f"\nconsole.log(JSON.stringify({expression}));"
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


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


def test_swarm_batch_prefers_the_production_omnivoice_cloner():
    source = _source()

    load_models = source[source.index("async function loadJobModels()"):
                         source.index('$("#j-modality").addEventListener', source.index("async function loadJobModels()"))]
    assert 'b.repo === "mlx-community/OmniVoice-bfloat16"' in load_models
    assert 'a.repo === "mlx-community/OmniVoice-bfloat16"' in load_models


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


def test_generation_batch_lookup_is_case_insensitive_and_modality_scoped():
    result = _run_generation_helpers("""
      filterGenerationBatches([
        {id: '3765742cde', modality: 'voice', model: 'OmniVoice', label: '0100'},
        {id: '26c99af3d6', modality: 'image', model: 'FLUX2 Klein', label: 'Tsh'},
        {id: 'other', modality: 'image', model: 'FLUX2 Klein', label: 'Pps'}
      ], 'image', '26C99')
    """)

    assert result == [
        {"id": "26c99af3d6", "modality": "image",
         "model": "FLUX2 Klein", "label": "Tsh"},
    ]
    source = _source()
    assert 'id="generation-batch-search"' in source
    assert 'aria-label="Find generation batch by ID, label, or model"' in source


def test_image_resource_evidence_is_visible_in_generation_details():
    rendered = _run_resource_helper("""
      resourceUsageHTML({
        schema: 'imagestudio.resource-telemetry',
        worker: {peak_rss_gb: 5.25},
        host: {minimum_available_gb: 1.5, peak_pressure_level: 'warn'},
        mlx: {reported_peak_gb: 4.75}
      })
    """)

    assert "worker peak 5.25 GB" in rendered
    assert "lowest free 1.50 GB" in rendered
    assert "pressure warn" in rendered
    assert "MLX peak 4.75 GB" in rendered


def test_voice_uncertainty_stays_visible_with_explicit_recovery_actions():
    source = _source()

    assert 'function recoverVoiceItem(batchId, itemIndex, force)' in source
    assert '/voice-recovery' in source
    assert 'i.state === "uncertain"' in source
    assert 'Restart Voice service' in source
    assert 'Stop and reconcile' in source
    assert '(b.cancel_requested || 0)' in source
    assert 'b.uncertain' in source
    assert 'const status = $("#voice-job-action-status");' in source
