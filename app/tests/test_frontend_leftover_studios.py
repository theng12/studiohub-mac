"""The dashboard control for removing a retired Studio that is still installed.

The owner must be able to click this, not curl it — and the page must stay
silent about it on a Mac that has nothing left over.
"""

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


def _render(data: dict) -> dict:
    render = _javascript(
        FRONTEND.read_text(),
        "let startupLeftoverRows = [];",
        "async function removeLeftoverStudio",
    )
    return _run_node(
        "const panel = {className: 'hide', innerHTML: ''};\n"
        "function $(selector) { return selector === '#startup-leftover' ? panel : null; }\n"
        "function esc(value) { return String(value); }\n"
        "function jsq(value) { return String(value); }\n"
        "function mlabel(value) { return value; }\n"
        f"{render}\n"
        f"renderStartupLeftovers({json.dumps(data)});\n"
        "console.log(JSON.stringify({"
        "className: panel.className, html: panel.innerHTML, rows: startupLeftoverRows}));",
    )


def test_leftover_section_lists_each_installed_retired_studio_with_a_remove_button():
    payload = _render({"machines": {
        "local": {"reachable": True, "supported": True, "services": [], "leftover_studios": [
            {"modality": "music", "title": "Music Studio KH",
             "folders": ["musicstudio-mac", "musicstudio-mac.git"]},
        ]},
        "mac-b": {"reachable": True, "supported": True, "services": [], "leftover_studios": [
            {"modality": "render", "title": "Render Studio KH",
             "folders": ["renderstudio-mac"]},
        ]},
    }})

    assert payload["className"] == ""
    assert "Leftover retired studios" in payload["html"]
    assert "Music Studio KH" in payload["html"] and "Render Studio KH" in payload["html"]
    assert "musicstudio-mac, musicstudio-mac.git" in payload["html"]
    assert payload["html"].count(">Remove<") == 2
    assert "removeLeftoverStudio('local','music')" in payload["html"]
    assert "removeLeftoverStudio('mac-b','render')" in payload["html"]
    # The section states the outcome plainly, before anyone clicks anything.
    assert "moves the folder to the Trash" in payload["html"]
    assert "Models still used by other Studios are kept" in payload["html"]
    assert "nothing is erased" in payload["html"]
    assert [row["machine"] for row in payload["rows"]] == ["local", "mac-b"]


def test_leftover_section_is_absent_when_every_mac_is_clean():
    payload = _render({"machines": {
        "local": {"reachable": True, "supported": True,
                  "services": [{"modality": "image"}], "leftover_studios": []},
    }})

    assert payload["className"] == "hide"
    assert payload["html"] == ""
    assert payload["rows"] == []


def test_leftover_section_tolerates_a_peer_hub_that_predates_the_field():
    """An older peer simply reports no leftovers rather than breaking the audit."""
    payload = _render({"machines": {
        "mac-old": {"reachable": True, "supported": True, "services": []},
        "mac-off": {"reachable": False, "supported": False},
    }})

    assert payload["className"] == "hide"
    assert payload["rows"] == []


def test_remove_confirmation_states_the_outcome_and_reports_refusals_verbatim():
    source = FRONTEND.read_text()
    action = _javascript(
        source, "async function removeLeftoverStudio", "async function loadStartupServices",
    )

    # An explicit confirm naming the machine, before any request is sent.
    assert "confirm(" in action
    assert "stops its services, removes it from startup, and moves the folder to the Trash" in action
    assert "Models still used by other Studios are kept" in action
    assert action.index("confirm(") < action.index("/remove")
    # A refusal must reach the owner in the Hub's own words, both shapes:
    # a plain local detail string and a remote `{code, message}` object.
    assert 'typeof data.detail === "string" ? data.detail : data.detail?.message' in action
    assert "was not removed" in action
    # The list is re-read from the Hub after every attempt, so a removed row
    # disappears instead of being struck off optimistically.
    assert "await loadStartupServices();" in action


def test_startup_audit_refreshes_the_leftover_list_it_renders():
    source = FRONTEND.read_text()
    loader = _javascript(
        source, "async function loadStartupServices", "async function runStartupInstall",
    )

    assert "renderStartupLeftovers(data);" in loader
    assert '<div id="startup-leftover" class="hide"></div>' in source
