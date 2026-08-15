from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).parents[2]
FRONTEND = ROOT / "app" / "frontend" / "index.html"


def _managed_release_card(source: str) -> str:
    start = source.index('id="managed-release-card"')
    end = source.index('</div>\n\n    <div class="card', start)
    return source[start:end]


def _javascript(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


def _run_node(program: str) -> dict:
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, check=True, text=True,
    )
    return json.loads(result.stdout)


def test_updates_has_distinct_status_only_managed_release_card():
    source = FRONTEND.read_text()
    card = _managed_release_card(source)

    assert 'id="managed-release-card"' in source
    assert 'aria-labelledby="managed-release-title"' in card
    assert 'role="status" aria-live="polite"' in card
    assert 'api("/api/hub/maintenance/release-intent")' in source
    assert "function renderManagedRelease" in source
    assert "function loadManagedRelease" in source
    assert "No managed release has been assigned" in source
    assert "Managed release status is temporarily unavailable" in source
    assert "Pending machines do not block healthy machines" in source
    assert all(token not in card for token in ("<button", "<input", "<select", "onclick="))


def test_managed_release_card_has_bounded_responsive_content():
    source = FRONTEND.read_text()

    assert 'id="managed-release-machines" class="managed-release-machines"' in source
    assert 'class="managed-release-target"' in source
    assert '.managed-release-machines{overflow-x:auto}' in source
    assert '@media(max-width:600px)' in source
    assert '.managed-release-target{grid-template-columns:1fr}' in source


def test_unactivated_intent_never_reuses_a_historical_release_job():
    source = FRONTEND.read_text()
    function = _javascript(
        source, "function managedReleaseJob", "function managedReleaseCanary",
    )
    fixture = {
        "desired": {"manifest": {"release_id": "sha256:new", "sequence": 2}},
        "activation": None,
        "jobs": [{
            "id": "historical-job", "release_id": "sha256:old",
            "state": "complete", "machines": {"local": {}},
        }],
    }

    payload = _run_node(
        f"{function}\n"
        f"const fixture = {json.dumps(fixture)};\n"
        "console.log(JSON.stringify({job: managedReleaseJob(fixture)}));",
    )

    assert payload == {"job": None}


def test_managed_release_polling_is_bounded_and_replaces_existing_timer():
    source = FRONTEND.read_text()
    scheduler = _javascript(
        source, "let managedReleasePollTimer", "function managedReleaseState",
    )
    payload = _run_node(
        "let cleared = []; let scheduled = []; let sequence = 0;\n"
        "function clearTimeout(value) { cleared.push(value); }\n"
        "function setTimeout(callback, delay) { scheduled.push(delay); return ++sequence; }\n"
        "function vis(tab) { return tab === 'updates'; }\n"
        "function loadManagedRelease() {}\n"
        f"{scheduler}\n"
        "scheduleManagedReleasePoll(MANAGED_RELEASE_PASSIVE_POLL_MS);\n"
        "scheduleManagedReleasePoll(MANAGED_RELEASE_ACTIVE_POLL_MS);\n"
        "console.log(JSON.stringify({cleared, scheduled, timer: managedReleasePollTimer, "
        "active: MANAGED_RELEASE_ACTIVE_POLL_MS, passive: MANAGED_RELEASE_PASSIVE_POLL_MS, "
        "error: MANAGED_RELEASE_ERROR_POLL_MS}));",
    )

    assert payload == {
        "cleared": [None, 1],
        "scheduled": [30_000, 5_000],
        "timer": 2,
        "active": 5_000,
        "passive": 30_000,
        "error": 10_000,
    }
    assert "scheduleManagedReleasePoll(active" in source
    assert "scheduleManagedReleasePoll(MANAGED_RELEASE_ERROR_POLL_MS)" in source
