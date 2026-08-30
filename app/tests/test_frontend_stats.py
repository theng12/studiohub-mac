"""Behaviour checks for the operator-first portion of the Stats page."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app" / "frontend" / "index.html"


def _javascript(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


def _render(activity: dict | None) -> dict:
    source = FRONTEND.read_text()
    helper = _javascript(source, "const FLEET_ACTIVITY_STATES =", "async function loadStats")
    program = f"""
const nodes = Object.fromEntries([
  "#st-fleet-pulse", "#st-fleet-note", "#st-fleet-body", "#st-fleet-meta",
].map(id => [id, {{ innerHTML: "", textContent: "", className: "", dataset: {{}}, setAttribute() {{}} }}]));
function $(id) {{ return nodes[id]; }}
function esc(value) {{ return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;"); }}
function mlabel(value) {{ return String(value || "local"); }}
function fmtDur(value) {{
  value = Math.round(Number(value || 0));
  if (value < 60) return value + "s";
  if (value < 3600) return Math.floor(value / 60) + "m";
  return Math.floor(value / 3600) + "h";
}}
function fmtAgo(value) {{ return value ? "1m ago" : "never"; }}
{helper}
renderFleetActivity({json.dumps(activity)});
console.log(JSON.stringify({{
  pulse: nodes["#st-fleet-pulse"].innerHTML,
  note: nodes["#st-fleet-note"].textContent,
  rows: nodes["#st-fleet-body"].innerHTML,
  meta: nodes["#st-fleet-meta"].textContent,
}}));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_fleet_activity_renders_all_operational_states_attention_first_with_safe_evidence():
    activity = {
        "window": {"since_s": 0, "now": 1000},
        "pulse": {"working": 1, "just_finished": 1, "ready": 1, "long_idle": 1, "offline": 1, "needs_attention": 1, "unknown": 1},
        "machines": [
            {"machine": "ready", "state": "ready", "state_duration_s": 90, "completed": 2, "failed": 0, "utilization": {"ratio": 0.4, "evidence": "complete"}},
            {"machine": "idle", "state": "long_idle", "state_duration_s": 7200, "completed": 0, "failed": 0, "utilization": {"ratio": None, "evidence": "partial"}, "limitation": "Direct activity partially unavailable"},
            {"machine": "offline", "state": "offline", "state_duration_s": 120, "completed": 0, "failed": 0, "utilization": {"ratio": None, "evidence": "partial"}},
            {"machine": "working", "state": "working", "state_duration_s": 70, "studio": "image@working", "model": "org/model", "progress": 0.42, "job_id": "job-42", "source": "direct", "completed": 4, "failed": 0, "utilization": {"ratio": 0.5, "evidence": "complete"}},
            {"machine": "finished", "state": "just_finished", "state_duration_s": 31, "studio": "voice@finished", "model": "org/voice", "latest": {"state": "done", "runtime_s": 13}, "completed": 3, "failed": 0, "utilization": {"ratio": 0.2, "evidence": "complete"}},
            {"machine": "attention", "state": "needs_attention", "state_duration_s": 61, "studio": "voice@attention", "model": "org/voice", "latest": {"state": "error", "error_code": "worker_failed"}, "completed": 0, "failed": 1, "utilization": {"ratio": None, "evidence": "partial"}},
            {"machine": "unknown", "state": "unknown", "state_duration_s": 0, "completed": 0, "failed": 0, "utilization": {"ratio": None, "evidence": "partial"}, "limitation": "Direct activity unavailable"},
        ],
    }

    rendered = _render(activity)

    for label in ("Needs attention", "Working", "Just finished", "Ready", "Long idle", "Offline", "Unknown"):
        assert label in rendered["pulse"]
        assert label in rendered["rows"]
    assert rendered["rows"].index("attention") < rendered["rows"].index("working") < rendered["rows"].index("finished")
    assert "42% complete" in rendered["rows"]
    assert "Unavailable / partial" in rendered["rows"]
    assert ">0% observed" not in rendered["rows"]
    assert "Direct activity unavailable" in rendered["note"]
    assert "<details" in rendered["rows"]
    assert 'aria-label="Show activity details for attention"' in rendered["rows"]
    assert "Last 16m" in rendered["meta"]


def test_fleet_activity_has_loading_empty_and_error_copy_without_hiding_historical_stats():
    source = FRONTEND.read_text()

    assert "function renderFleetActivityLoading()" in source
    assert "function renderFleetActivityError({ preserve = false } = {})" in source
    assert "Loading current fleet activity" in source
    assert "No Image or Voice machines are registered" in source
    assert "Current fleet activity could not be loaded" in source
    assert "Historical performance" in source


def test_fleet_activity_reports_health_attention_unknown_progress_job_identity_and_real_timeline_states():
    rendered = _render({
        "schema": "studiohub.fleet_activity.v1",
        "window": {"since_s": 0, "now": 1000},
        "pulse": {"needs_attention": 1, "working": 2},
        "machines": [
            {"machine": "health", "state": "needs_attention", "state_duration_s": 4,
             "latest": {"state": "done", "runtime_s": 8}, "job_id": "safe-job-42",
             "completed": 1, "failed": 0, "utilization": {"ratio": 0.2, "evidence": "complete"}},
            {"machine": "no-progress", "state": "working", "state_duration_s": 4,
             "progress": None, "completed": 0, "failed": 0,
             "utilization": {"ratio": 0.2, "evidence": "complete"}},
            {"machine": "zero-progress", "state": "working", "state_duration_s": 4,
             "progress": 0, "completed": 0, "failed": 0,
             "utilization": {"ratio": 0.2, "evidence": "complete"},
             "timeline": [
                 {"state": "queued", "observed_at": 10}, {"state": "running", "observed_at": 11},
                 {"state": "done", "finished_at": 12}, {"state": "error", "finished_at": 13},
                 {"state": "cancelled", "finished_at": 14},
             ]},
        ],
    })

    assert "Studio health needs attention" in rendered["rows"]
    assert "Completed in 8s" not in rendered["rows"].split("no-progress")[0]
    assert "Processing" in rendered["rows"]
    assert "0% complete" in rendered["rows"]
    assert "safe-job-42" in rendered["rows"]
    for label in ("Queued", "Running", "Completed", "Failed", "Cancelled"):
        assert label in rendered["rows"]


def test_fleet_activity_attention_disclosure_matches_the_failure_or_health_cause():
    rendered = _render({
        "schema": "studiohub.fleet_activity.v1",
        "window": {"since_s": 0, "now": 1000},
        "pulse": {"needs_attention": 2},
        "machines": [
            {"machine": "failed-job", "state": "needs_attention", "latest": {"state": "error", "error_code": "worker_failed"}},
            {"machine": "health-check", "state": "needs_attention", "latest": {"state": "done", "runtime_s": 9}},
        ],
    })

    failed = rendered["rows"].split("health-check")[0]
    health = rendered["rows"].split("health-check", 1)[1]
    assert "Failed · worker_failed" in failed
    assert "Latest job failed with worker_failed" in failed
    assert "Studio health needs attention. Check this Mac’s Studio health" not in failed
    assert "Studio health needs attention. Check this Mac’s Studio health" in health


def test_fleet_activity_refresh_and_contract_guards_preserve_the_existing_board():
    source = FRONTEND.read_text()
    loader = _javascript(source, "async function loadStats", "// Auto-clear")

    assert "loadStats({ background: true })" in source
    assert "!background && !fleetActivityHasRendered" in loader
    assert "response.ok" in loader
    assert "!d || typeof d !== \"object\" || Array.isArray(d)" in loader
    assert "isFleetActivitySnapshot(d.fleet_activity)" in loader
    assert "renderFleetActivityError({ preserve: fleetActivityHasRendered })" in loader
    assert "fleetActivityCaptureView" in source and "fleetActivityRestoreView" in source
    assert 'aria-live="off"' in source
    assert "repeat(7," in source
    assert "@media(max-width:1200px){.fleet-pulse{grid-template-columns:repeat(4," in source
    assert ".fleet-detail code{overflow-wrap:anywhere" in source
    assert "detail:" not in _javascript(source, "const FLEET_ACTIVITY_STATES =", "const FLEET_ACTIVITY_ORDER")


def test_fleet_activity_contract_requires_a_real_additive_snapshot_but_allows_an_empty_fleet():
    source = FRONTEND.read_text()
    helper = _javascript(source, "const FLEET_ACTIVITY_STATES =", "async function loadStats")
    program = f"""
function fmtDur(value) {{ return String(value); }}
{helper}
const valid = {{ schema: "studiohub.fleet_activity.v1", window: {{}}, pulse: {{}}, machines: [] }};
console.log(JSON.stringify([
  isFleetActivitySnapshot(valid), isFleetActivitySnapshot(null),
  isFleetActivitySnapshot([]), isFleetActivitySnapshot({{}}),
  isFleetActivitySnapshot({{ ...valid, machines: {{}} }}),
  isFleetActivitySnapshot({{ ...valid, window: [] }}),
  isFleetActivitySnapshot({{ ...valid, pulse: [] }}),
]));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [True, False, False, False, False, False, False]


def test_fleet_activity_focus_guard_suppresses_background_rebuilds_for_native_disclosures():
    source = FRONTEND.read_text()
    helper = _javascript(source, "function fleetActivityHasFocus", "function renderFleetActivityLoading")
    program = f"""
const fleetSummary = {{ closest: selector => selector === ".fleet-activity" ? {{}} : null }};
global.document = {{ activeElement: fleetSummary }};
{helper}
const focused = fleetActivityHasFocus();
document.activeElement = {{ closest: () => null }};
const outside = fleetActivityHasFocus();
console.log(JSON.stringify([focused, outside]));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [True, False]
    assert "vis(\"stats\") && !fleetActivityHasFocus()" in source
