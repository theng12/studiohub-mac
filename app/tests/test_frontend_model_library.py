import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app/frontend/index.html"


def _render_model_library(models: list[dict], modality: str = "") -> dict:
    source = FRONTEND.read_text(encoding="utf-8")
    start = source.find("function modelLibraryRows(models)")
    assert start >= 0, "Model Library needs a dedicated visible-row policy"
    end = source.find("let mTimer;", start)
    assert end > start
    renderer = source[start:end]
    script = (
        "const elements = {\n"
        "  'm-body': {innerHTML: ''},\n"
        "  'm-machine': {dataset: {}, innerHTML: '', value: ''},\n"
        "  'm-count': {textContent: ''},\n"
        f"  'm-modality': {{value: {json.dumps(modality)}}},\n"
        "};\n"
        "const $ = selector => elements[selector.slice(1)];\n"
        "const esc = value => String(value ?? '');\n"
        "const ramSourceLabel = () => '';\n"
        f"let modelsData = {json.dumps(models)};\n"
        "let mMachine = '';\n"
        "let mSort = 'modality';\n"
        f"{renderer}\n"
        "renderModels();\n"
        "process.stdout.write(JSON.stringify({\n"
        "  body: elements['m-body'].innerHTML,\n"
        "  count: elements['m-count'].textContent,\n"
        "  machines: elements['m-machine'].innerHTML,\n"
        "}));\n"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_model_library_hides_only_undownloaded_transcription_models():
    rendered = _render_model_library([
        {
            "repo": "mlx/whisper-ready",
            "label": "Whisper Ready",
            "modality": "transcription",
            "downloaded": True,
            "cached_on": ["mac-a"],
        },
        {
            "repo": "mlx/whisper-catalog-only",
            "label": "Whisper Catalog Only",
            "modality": "transcription",
            "downloaded": False,
            "cached_on": [],
        },
        {
            "repo": "org/image-catalog-only",
            "label": "Image Catalog Only",
            "modality": "image",
            "downloaded": False,
            "cached_on": [],
        },
    ])

    assert "mlx/whisper-ready" in rendered["body"]
    assert "mlx/whisper-catalog-only" not in rendered["body"]
    assert "org/image-catalog-only" in rendered["body"]
    assert rendered["count"] == "2 models · 1 downloaded"
    assert "mac-a" in rendered["machines"]


def test_transcription_library_has_a_specific_empty_state():
    transcription = _render_model_library([
        {
            "repo": "mlx/whisper-catalog-only",
            "label": "Whisper Catalog Only",
            "modality": "transcription",
            "downloaded": False,
            "cached_on": [],
        },
    ], modality="transcription")
    image = _render_model_library([], modality="image")

    assert "No downloaded transcription models." in transcription["body"]
    assert "No models match" in image["body"]
    assert "No downloaded transcription models." not in image["body"]
