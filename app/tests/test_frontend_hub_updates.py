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


def test_completed_hub_batch_keeps_machine_failure_visible_and_retryable():
    source = FRONTEND.read_text()
    render = _javascript(source, "function renderHubVersions", "async function rescanHubVersions")
    data = {
        "latest": "2.13.7",
        "machines": {
            "mac-a": {
                "version": "2.13.7", "host": "10.0.0.8", "reachable": True,
                "checked_at": 2000,
                "last_update": {
                    "status": "failed", "detail": "pterm restart timed out after 90 seconds",
                    "attempted_at": 1900, "from_version": "2.13.1", "to_version": None,
                },
            },
        },
    }
    payload = _run_node(
        "const elements = {\n"
        "  '#hubupd-body': {innerHTML: ''}, '#hubupd-status': {className: '', innerHTML: '', classList: {add() {}}},\n"
        "  '#hubupd-run': {disabled: false, textContent: '', removeAttribute() {}},\n"
        "  '#hubupd-rescan': {disabled: false}\n"
        "};\n"
        "function $(selector) { return elements[selector]; }\n"
        "function esc(value) { return String(value); }\n"
        "function jsq(value) { return String(value); }\n"
        "function mlabel(value) { return value; }\n"
        "function fmtAgo(value) { return String(value); }\n"
        "function _sortHubEntries(value) { return value; }\n"
        "function _renderHubSortControl() {}\n"
        "function _hubHardware() { return {chip: 'Apple M1', ram: 8}; }\n"
        "function _hubRamLabel(value) { return value + ' GB'; }\n"
        "function renderUpdateProgress() {}\n"
        "function verGte(a, b) { return a === b; }\n"
        "let hubUpdateBusy = false; let lastHubVersionData = null;\n"
        f"{render}\n"
        f"renderHubVersions({json.dumps(data)});\n"
        "console.log(JSON.stringify({html: elements['#hubupd-body'].innerHTML}));",
    )

    assert "up to date" in payload["html"]
    assert "Last update failed" in payload["html"]
    assert "pterm restart timed out after 90 seconds" in payload["html"]
    assert 'class="hub-update-failure"' in payload["html"]
    assert ">Retry<" in payload["html"]


def test_updates_page_shows_drain_progress_instead_of_a_permanent_deferral():
    """The manual buttons must read as draining/installing/rejoining, not "Deferred"."""
    source = FRONTEND.read_text()
    states = _javascript(source, "function autoState", "function updateHubAutoControlState")
    render = _javascript(source, "function renderHubAutoUpdate", "function renderFleetAutoProgress")

    assert 'draining: ["gen", "Draining site"]' in states
    # The fleet table shares autoState, so the three phase names are the Hub
    # card's own and must not rename every sibling Studio row.
    assert 'updating: ["gen", "Updating"]' in states
    assert 'draining: "Draining site", updating: "Installing", restarting: "Rejoining"' in render
    # "draining" must count as busy everywhere the other in-flight states do,
    # otherwise the page stops polling and the drain looks like a no-op again.
    assert source.count('["checking", "draining", "updating", "restarting"]') == 3
    assert '["checking", "updating", "restarting"]' not in source


def test_update_now_documents_that_it_does_not_wait_for_idle():
    source = FRONTEND.read_text()
    action = _javascript(source, "async function hubAutoAction", "let hubRestartPolling")

    assert "does not wait for this site to become idle" in action
    assert "Studio workers are separate processes and keep running" in action
    assert "Withdrawing this site from fleet routing" in action
    assert "the next idle window" not in action


def test_drain_wait_is_an_owner_editable_setting_that_is_saved():
    source = FRONTEND.read_text()

    assert 'id="hau-drain" type="number" min="1" step="1"' in source
    assert "settings.drain_timeout_minutes" in source
    assert 'drain_timeout_minutes: Number($("#hau-drain").value)' in source
