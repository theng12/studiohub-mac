import json
import subprocess
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[2] / "app" / "frontend" / "index.html"


def _remove_machine_function() -> str:
    source = FRONTEND.read_text()
    start = source.index("async function removeMachine")
    end = source.index("// Prune ONE studio", start)
    return source[start:end]


def _run_remove(response: str) -> list[str]:
    program = f"""
const notices = [];
function confirm() {{ return true; }}
function alert(value) {{ notices.push(String(value)); }}
async function api() {{ return {response}; }}
async function refresh() {{ notices.push('refreshed'); }}
function renderMachines() {{ notices.push('rendered'); }}
{_remove_machine_function()}
removeMachine('mac-a').then(() => console.log(JSON.stringify(notices)));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_remove_machine_displays_http_failure_without_refreshing():
    notices = _run_remove("{ok: false, status: 404, json: async () => ({detail: 'not registered here'})}")

    assert notices == ["Could not remove mac-a: not registered here"]


def test_remove_machine_displays_verified_controller_receipt_before_refreshing():
    notices = _run_remove("{ok: true, status: 200, json: async () => ({machine: 'mac-a', controller_id: 'controller-0300', site_id: 'site-0300', epoch_closed_at: 12, registry_absent: true})}")

    assert notices == [
        "Removed mac-a from controller controller-0300 at site site-0300 (epoch closed 12; no active registry row).",
        "refreshed", "rendered",
    ]
