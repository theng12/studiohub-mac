from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).parents[2]
FRONTEND = ROOT / "app" / "frontend" / "index.html"


def _javascript(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


def _run_node(program: str) -> dict:
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, check=True, text=True,
    )
    return json.loads(result.stdout)


def test_completed_update_immediately_overlays_its_verified_version():
    source = FRONTEND.read_text()
    helper = _javascript(
        source, "function effectiveStudioUpdateRow", "function renderStudioUpdates",
    )
    row = {
        "id": "voice@mac-b", "version": "2.4.4", "latest_version": "2.4.5",
        "version_status": "update_available", "update_available": True,
        "reachable": True,
    }
    complete = {
        "studio": "voice@mac-b", "status": "complete", "to_version": "2.4.5",
    }
    failed = {
        "studio": "voice@mac-b", "status": "failed", "to_version": "2.4.5",
    }

    payload = _run_node(
        f"{helper}\n"
        f"const row = {json.dumps(row)};\n"
        f"const complete = effectiveStudioUpdateRow(row, {json.dumps(complete)});\n"
        f"const failed = effectiveStudioUpdateRow(row, {json.dumps(failed)});\n"
        "console.log(JSON.stringify({complete, failed}));",
    )

    assert payload["complete"] == {
        **row,
        "version": "2.4.5",
        "latest_version": "2.4.5",
        "version_status": "current",
        "update_available": False,
    }
    assert payload["failed"] == row

