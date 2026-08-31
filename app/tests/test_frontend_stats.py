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
function jsq(value) {{ return JSON.stringify(String(value ?? "")).slice(1, -1); }}
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


def _details_javascript(source: str) -> str:
    return _javascript(source, "const FLEET_ORIGIN_LABELS =", "function fleetActivityModelName")


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
const dialog = {{ open: false }};
global.document = {{ activeElement: fleetSummary, querySelector: selector => selector === "#fleet-job-details" ? dialog : null }};
{helper}
const focused = fleetActivityHasFocus();
document.activeElement = {{ closest: () => null }};
const outside = fleetActivityHasFocus();
dialog.open = true;
const drawer = fleetActivityHasFocus();
console.log(JSON.stringify([focused, outside, drawer]));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [True, False, True]
    assert "vis(\"stats\") && !fleetActivityHasFocus()" in source


def test_background_activity_response_does_not_rebuild_when_focus_enters_while_fetch_is_pending():
    source = FRONTEND.read_text()
    helper = _javascript(source, "function fleetActivityHasFocus", "function fleetActivityCaptureView")
    program = f"""
let focused = false, renders = 0;
global.document = {{ get activeElement() {{ return {{ closest: () => focused ? {{}} : null }}; }} }};
function renderFleetActivity() {{ renders += 1; }}
{helper}
let resolve;
const pending = new Promise(done => resolve = done);
const request = (async () => renderFleetActivityIfSafe(await pending, {{ background: true, preserve: true }}))();
focused = true;
resolve({{ machines: [] }});
request.then(rendered => console.log(JSON.stringify([rendered, renders])));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [False, 0]
    assert "renderFleetActivityIfSafe(d.fleet_activity" in source


def test_fleet_activity_renders_stable_origins_and_allowlisted_detail_actions():
    rendered = _render({
        "schema": "studiohub.fleet_activity.v1",
        "window": {"since_s": 0, "now": 1000},
        "pulse": {"working": 4},
        "machines": [
            {"machine": "hub", "state": "working", "studio": "image@hub", "job_id": "hub-job",
             "origin": "hub", "origin_device": "Studio Hub KH · PPS", "model": "org/image",
             "timeline": [{"studio": "voice@hub", "job_id": "timeline-job", "state": "done",
                           "origin": "local_ui", "origin_device": "Worker Mac", "model": "org/voice"}]},
            {"machine": "local", "state": "working", "studio": "voice@local", "job_id": "local-job",
             "origin": "local_ui", "model": "org/voice"},
            {"machine": "api", "state": "working", "studio": "image@api", "job_id": "api-job",
             "origin": "api", "model": "org/image"},
            {"machine": "legacy", "state": "working", "studio": "voice@legacy", "job_id": "legacy-job",
             "origin": "unknown", "model": "org/voice"},
        ],
    })

    rows = rendered["rows"]
    for label in ("Studio Hub", "Local Studio UI", "API/automation", "Unknown/legacy"):
        assert label in rows
    assert rows.index("Studio Hub") < rows.index("Studio Hub KH · PPS")
    assert rows.index("Local Studio UI") < rows.index("Worker Mac")
    assert rows.count("View details") == 5
    assert "openFleetJobDetails({studio:" in rows
    for forbidden in ("prompt", "transcript", "handle", "parameters", "output_path"):
        assert forbidden not in rows


def test_stats_render_and_load_never_prefetch_job_details():
    source = FRONTEND.read_text()
    helper = _javascript(source, "const FLEET_ACTIVITY_STATES =", "async function loadStats")
    loader = _javascript(source, "async function loadStats", "// ── remote")
    program = f"""
const paths = [];
const nodes = new Proxy({{}}, {{ get(target, key) {{
  if (!target[key]) target[key] = {{ innerHTML: "", textContent: "", dataset: {{}}, style: {{}}, value: "", querySelectorAll() {{ return []; }}, addEventListener() {{}} }};
  return target[key];
}} }});
function $(id) {{ return nodes[id]; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function mlabel(value) {{ return String(value || "local"); }}
function fmtDur(value) {{ return String(value || 0) + "s"; }}
function fmtAgo() {{ return "now"; }}
function renderThroughput() {{}}
const MOD_EMOJI = {{}}, MOD_COLOR = {{ image: "#000" }}, WIN_LABEL = {{}};
let stWin = "0", stSrc = "all", stOp = "", stMach = "";
async function api(path) {{ paths.push(path); return {{ ok: true, json: async () => null }}; }}
{helper}
renderFleetActivity({{ schema: "studiohub.fleet_activity.v1", window: {{}}, pulse: {{}}, machines: [] }});
{loader}
loadStats().then(() => console.log(JSON.stringify(paths)));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["/api/hub/stats"]


def test_opening_details_uses_one_encoded_url_and_aborts_the_previous_open():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    program = f"""
const calls = [];
const status = {{ textContent: "", dataset: {{}} }};
const body = {{ textContent: "", querySelectorAll() {{ return []; }} }};
const dialog = {{ open: false, dataset: {{}}, showModal() {{ this.open = true; }}, close() {{ this.open = false; }} }};
function $(id) {{ return id === "#fleet-job-details" ? dialog : id === "#fleet-job-details-status" ? status : body; }}
async function api(path, options) {{ calls.push([path, options]); return new Promise(() => {{}}); }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value); }}
global.document = {{ activeElement: null }};
global.URL = {{ revokeObjectURL() {{}} }};
{helper}
openFleetJobDetails({{ studio: "image@Mac / A", jobId: "job / 1", machine: "Mac", origin: "hub", originDevice: "Hub" }});
const first = fleetJobDetailsState.controller;
openFleetJobDetails({{ studio: "voice@Mac / B", jobId: "job / 2", machine: "Mac", origin: "api", originDevice: "" }});
setImmediate(() => console.log(JSON.stringify({{ paths: calls.map(call => call[0]), firstAborted: first.signal.aborted }})));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "paths": [
            "/studio/image%40Mac%20%2F%20A/api/fleet/jobs/job%20%2F%201/details",
            "/studio/voice%40Mac%20%2F%20B/api/fleet/jobs/job%20%2F%202/details",
        ],
        "firstAborted": True,
    }


def test_closing_details_destroys_ephemeral_content_and_restores_focus():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    program = f"""
const events = [];
const audio = {{ pause() {{ events.push("pause"); }}, removeAttribute(name) {{ events.push("remove:" + name); }}, load() {{ events.push("load"); }} }};
const status = {{ textContent: "secret status" }};
const body = {{ textContent: "secret prompt", querySelectorAll(selector) {{ return selector === "audio" ? [audio] : []; }} }};
const dialog = {{ open: true, dataset: {{}}, close() {{ this.open = false; events.push("close"); }} }};
function $(id) {{ return id === "#fleet-job-details" ? dialog : id === "#fleet-job-details-status" ? status : body; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value); }}
global.document = {{ activeElement: null }};
global.URL = {{ revokeObjectURL(value) {{ events.push("revoke:" + value); }} }};
{helper}
fleetJobDetailsState.controller = {{ abort() {{ events.push("abort"); }} }};
fleetJobDetailsState.objectUrls = ["blob:one", "blob:two"];
fleetJobDetailsState.invoker = {{ focus() {{ events.push("focus"); }} }};
closeFleetJobDetails();
console.log(JSON.stringify({{ events, body: body.textContent, status: status.textContent, urls: fleetJobDetailsState.objectUrls }}));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    closed = json.loads(result.stdout)
    assert closed["events"] == ["abort", "pause", "remove:src", "load", "revoke:blob:one", "revoke:blob:two", "close", "focus"]
    assert closed["body"] == closed["status"] == ""
    assert closed["urls"] == []


def test_replacing_details_or_error_destroys_detached_ephemeral_media_first():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    program = f"""
const events = [];
const audio = {{ pause() {{ events.push("pause"); }}, removeAttribute(name) {{ events.push("remove:" + name); }}, load() {{ events.push("load"); }} }};
function element(tag) {{ return {{ tag, textContent: "", className: "", dataset: {{}}, children: [], append(...items) {{ this.children.push(...items); events.push("append:" + tag); }}, addEventListener() {{}} }}; }}
const dialog = {{ dataset: {{ machine: "Mac", origin: "api", originDevice: "" }} }};
const status = {{ textContent: "", dataset: {{}} }}, title = {{ textContent: "" }};
const body = {{
  value: "old content",
  querySelectorAll(selector) {{ return selector === "audio" ? [audio] : []; }},
  set textContent(value) {{ this.value = value; events.push("clear"); }},
  get textContent() {{ return this.value; }},
  append(...items) {{ events.push("append:body"); }},
}};
function $(id) {{ return id === "#fleet-job-details" ? dialog : id === "#fleet-job-details-body" ? body : id === "#fleet-job-details-status" ? status : title; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value); }}
global.document = {{ createElement: element }};
global.URL = {{ revokeObjectURL(value) {{ events.push("revoke:" + value); }} }};
{helper}
fleetJobDetailsState.objectUrls = ["blob:render"];
renderFleetJobDetails({{ schema: "kh-studio.job-details.v1", studio: "image", job: {{ id: "one" }}, inputs: {{}}, references: [], outputs: [] }});
const renderEvents = events.slice();
events.length = 0;
fleetJobDetailsState.objectUrls = ["blob:error"];
showFleetJobDetailsError("temporarily_unavailable");
console.log(JSON.stringify({{ renderEvents, errorEvents: events, urls: fleetJobDetailsState.objectUrls }}));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    replaced = json.loads(result.stdout)
    assert replaced["renderEvents"][:5] == ["pause", "remove:src", "load", "revoke:blob:render", "clear"]
    assert replaced["errorEvents"][:5] == ["pause", "remove:src", "load", "revoke:blob:error", "clear"]
    assert replaced["urls"] == []


def test_overlapping_detail_refreshes_ignore_an_older_response_that_finishes_last():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    program = f"""
const pending = [];
const rendered = [];
function $(id) {{ return {{ textContent: "", dataset: {{}} }}; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value); }}
async function api() {{ return new Promise(resolve => pending.push(resolve)); }}
global.document = {{ createElement() {{ return {{ addEventListener() {{}}, append() {{}} }}; }} }};
global.URL = {{ revokeObjectURL() {{}} }};
{helper}
fleetJobDetailsState.controller = new AbortController();
fleetJobDetailsState.studio = "image@mac";
fleetJobDetailsState.jobId = "job-1";
renderFleetJobDetails = details => rendered.push(details.job.id);
const older = refreshFleetJobDetails();
const newer = refreshFleetJobDetails();
pending[1]({{ ok: true, json: async () => ({{ schema: "kh-studio.job-details.v1", job: {{ id: "newer" }} }}) }});
setImmediate(() => {{
  pending[0]({{ ok: true, json: async () => ({{ schema: "kh-studio.job-details.v1", job: {{ id: "older" }} }}) }});
  Promise.all([older, newer]).then(results => console.log(JSON.stringify({{ rendered, results: results.map(item => item?.job?.id || null) }})));
}});
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"rendered": ["newer"], "results": [None, "newer"]}


def test_expired_open_and_download_replay_once_with_fresh_metadata():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    for mode in ("open", "download"):
        program = f"""
const mode = {json.dumps(mode)};
const paths = [], events = [];
const target = {{ children: [], textContent: "", replaceChildren(...items) {{ this.children = items; }}, append(...items) {{ this.children.push(...items); }} }};
function element(tag) {{ return {{ tag, textContent: "", className: "", hidden: false, children: [], addEventListener() {{}}, append(...items) {{ this.children.push(...items); }}, click() {{ events.push("download"); }}, remove() {{}} }}; }}
function $(id) {{ return {{ textContent: "", dataset: {{}} }}; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value); }}
global.document = {{ createElement: element, body: {{ append() {{}} }} }};
global.URL = {{ revokeObjectURL() {{}}, createObjectURL() {{ return "blob:fresh"; }} }};
const popup = {{ opener: {{}}, location: {{ href: "about:blank" }}, close() {{ events.push("popup-close"); }} }};
global.window = {{ open(url) {{ events.push("open:" + url); return popup; }} }};
let mediaCalls = 0;
async function api(path) {{
  paths.push(path);
  if (path.endsWith("/details")) return {{ ok: true, json: async () => ({{
    schema: "kh-studio.job-details.v1", job: {{ id: "job-1" }}, references: [],
    outputs: [{{ handle: "fresh", name: "result.png", media_type: "image/png" }}],
  }}) }};
  mediaCalls += 1;
  if (mediaCalls === 1) return {{ ok: false, status: 410, json: async () => ({{ detail: {{ code: "handle_expired" }} }}) }};
  return {{ ok: true, blob: async () => ({{ safe: true }}) }};
}}
{helper}
fleetJobDetailsState.controller = new AbortController();
fleetJobDetailsState.studio = "image@mac";
fleetJobDetailsState.jobId = "job-1";
renderFleetJobDetails = details => {{ Object.defineProperty(details.outputs[0], "_previewElement", {{ value: target }}); }};
const item = {{ handle: "expired", name: "result.png", media_type: "image/png" }};
Object.defineProperty(item, "_previewElement", {{ value: target }});
Object.defineProperty(item, "_mediaIdentity", {{ value: {{ collection: "outputs", index: 0 }} }});
loadFleetJobMedia(item, mode).then(() => console.log(JSON.stringify({{ paths, events, href: popup.location.href }})));
"""
        result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
        replayed = json.loads(result.stdout)
        assert replayed["paths"] == [
            f"/studio/image%40mac/api/fleet/jobs/job-1/media/expired{'?download=true' if mode == 'download' else ''}",
            "/studio/image%40mac/api/fleet/jobs/job-1/details",
            f"/studio/image%40mac/api/fleet/jobs/job-1/media/fresh{'?download=true' if mode == 'download' else ''}",
        ]
        if mode == "open":
            assert replayed["events"] == ["open:about:blank"]
            assert replayed["href"] == "blob:fresh"
        else:
            assert replayed["events"] == ["download"]


def test_open_reserves_a_safe_popup_before_fetch_and_closes_it_on_failure():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    program = f"""
const events = [];
let resolveFetch;
const target = {{ replaceChildren() {{}}, append() {{}}, textContent: "" }};
function element() {{ return {{ textContent: "", addEventListener() {{}}, append() {{}} }}; }}
function $(id) {{ return {{ textContent: "", dataset: {{}} }}; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value); }}
global.document = {{ createElement: element }};
global.URL = {{ revokeObjectURL() {{}}, createObjectURL() {{ return "blob:safe"; }} }};
const popups = [];
global.window = {{ open(url, targetName) {{
  events.push("open:" + url + ":" + targetName);
  const popup = {{ opener: "unsafe", location: {{ href: url }}, close() {{ events.push("close"); }} }};
  popups.push(popup);
  return popup;
}} }};
async function api() {{ events.push("api"); return new Promise(resolve => {{ resolveFetch = resolve; }}); }}
{helper}
fleetJobDetailsState.controller = new AbortController();
fleetJobDetailsState.studio = "image@mac";
fleetJobDetailsState.jobId = "job-1";
const item = {{ handle: "https://evil.invalid/", media_type: "image/png" }};
Object.defineProperty(item, "_previewElement", {{ value: target }});
const success = loadFleetJobMedia(item, "open");
const beforeFetch = events.slice();
resolveFetch({{ ok: true, blob: async () => ({{}}) }});
success.then(async () => {{
  const first = {{ opener: popups[0].opener, href: popups[0].location.href }};
  const failure = loadFleetJobMedia(item, "open");
  const beforeFailureFetch = events.slice(-2);
  resolveFetch({{ ok: false, status: 503, json: async () => ({{}}) }});
  await failure;
  console.log(JSON.stringify({{ beforeFetch, beforeFailureFetch, first, events }}));
}});
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    opened = json.loads(result.stdout)
    assert opened["beforeFetch"] == ["open:about:blank:_blank", "api"]
    assert opened["beforeFailureFetch"] == ["open:about:blank:_blank", "api"]
    assert opened["first"] == {"opener": None, "href": "blob:safe"}
    assert opened["events"][-1] == "close"


def test_expired_media_refreshes_once_then_exposes_retry_without_persistent_storage():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    assert all(token not in helper for token in (
        "localStorage", "sessionStorage", "indexedDB", "serviceWorker", "caches.", "history.pushState",
    ))
    assert ".innerHTML" not in helper
    program = f"""
let refreshes = 0;
let popupOpens = 0, popupCloses = 0;
const target = {{ children: [], textContent: "", replaceChildren(...items) {{ this.children = items; }}, append(...items) {{ this.children.push(...items); }} }};
function element(tag) {{ return {{ tag, textContent: "", className: "", disabled: false, addEventListener() {{}}, append(...items) {{ this.children = items; }} }}; }}
function $(id) {{ return {{ textContent: "", dataset: {{}} }}; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value); }}
global.document = {{ createElement: element }};
global.URL = {{ revokeObjectURL() {{}}, createObjectURL() {{ return "blob:new"; }} }};
global.window = {{ open() {{ popupOpens += 1; return {{ opener: {{}}, close() {{ popupCloses += 1; }} }}; }} }};
async function api() {{ return {{ ok: false, status: 410, json: async () => ({{ detail: {{ code: "handle_expired" }} }}) }}; }}
{helper}
fleetJobDetailsState.controller = new AbortController();
fleetJobDetailsState.studio = "image@mac";
fleetJobDetailsState.jobId = "job-1";
refreshFleetJobDetails = async () => {{
  refreshes += 1;
  const fresh = {{ handle: "fresh", media_type: "image/png" }};
  Object.defineProperty(fresh, "_previewElement", {{ value: target }});
  Object.defineProperty(fresh, "_mediaIdentity", {{ value: {{ collection: "outputs", index: 0 }} }});
  return {{ outputs: [fresh] }};
}};
const item = {{ handle: "opaque", media_type: "image/png" }};
Object.defineProperty(item, "_previewElement", {{ value: target }});
Object.defineProperty(item, "_mediaIdentity", {{ value: {{ collection: "outputs", index: 0 }} }});
(async () => {{
  await loadFleetJobMedia(item, "open");
  console.log(JSON.stringify({{ refreshes, popupOpens, popupCloses, labels: target.children.map(child => child.textContent) }}));
}})();
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"refreshes": 1, "popupOpens": 1, "popupCloses": 1, "labels": [
        "The secure media link expired. Retry to request a fresh one.", "Retry",
    ]}


def test_detail_heading_uses_broker_origin_only_when_the_activity_row_proves_hub_ownership():
    source = FRONTEND.read_text()
    helper = _details_javascript(source)
    program = f"""
function element(tag) {{ return {{ tag, textContent: "", className: "", children: [], append(...items) {{ this.children.push(...items); }}, addEventListener() {{}} }}; }}
const dialog = {{ dataset: {{ machine: "Mac", origin: "hub", originDevice: "Controller" }} }};
const body = element("div"), status = {{ textContent: "", dataset: {{}} }}, title = {{ textContent: "" }};
function $(id) {{ return id === "#fleet-job-details" ? dialog : id === "#fleet-job-details-body" ? body : id === "#fleet-job-details-status" ? status : title; }}
function esc(value) {{ return String(value ?? ""); }}
function jsq(value) {{ return String(value ?? ""); }}
function fmtDur(value) {{ return String(value) + "s"; }}
global.document = {{ createElement: element }};
{helper}
renderFleetJobDetails({{ schema: "kh-studio.job-details.v1", studio: "image", job: {{ id: "1", origin: "api" }}, inputs: {{}}, references: [], outputs: [] }});
const broker = title.textContent;
dialog.dataset.origin = "api";
renderFleetJobDetails({{ schema: "kh-studio.job-details.v1", studio: "voice", job: {{ id: "2", origin: "local_ui" }}, inputs: {{}}, references: [], outputs: [] }});
console.log(JSON.stringify([broker, title.textContent]));
"""
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [
        "Image Studio job details · Studio Hub",
        "Voice Studio job details · Local Studio UI",
    ]


def test_details_drawer_uses_native_accessible_responsive_markup():
    source = FRONTEND.read_text()

    assert '<dialog id="fleet-job-details" aria-labelledby="fleet-job-details-title">' in source
    assert 'id="fleet-job-details-status" role="status" aria-live="polite"' in source
    assert 'id="fleet-job-details-close" aria-label="Close job details"' in source
    assert ".fleet-job-details-shell" in source and "min(720px, 92vw)" in source
    assert "#fleet-job-details :focus-visible" in source
    assert "object-fit:contain" in source
    assert "@media(max-width:600px)" in source and "min-height:44px" in source
