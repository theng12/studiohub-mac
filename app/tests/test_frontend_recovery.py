"""Focused contract checks for the bounded recovery workspace."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app" / "frontend" / "index.html"


def _javascript(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


def test_recovery_panel_groups_rows_and_escapes_operator_visible_fields():
    source = FRONTEND.read_text(encoding="utf-8")
    helper = _javascript(source, "let recoveryData = null", "function renderBatches")
    program = f"""
const nodes = Object.fromEntries([
  ["#recovery-state", {{textContent: "", className: ""}}],
  ["#recovery-auto", {{textContent: "", innerHTML: ""}}],
  ["#recovery-body", {{innerHTML: ""}}],
].map(([id, node]) => [id, node]));
const $ = selector => nodes[selector];
const esc = value => String(value ?? "").replace(/[&<>\"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;", "'":"&#39;"}})[ch]);
const jsq = value => JSON.stringify(String(value ?? "")).slice(1, -1);
{helper}
renderRecovery({{
  auto: {{state: "watching", reason: "safe <detail>", jobs: [
    {{studio: "voice", job_id: "job-42", model: "org/voice", phase: "cancel_requested", reason: "Cancellation accepted for the exact job"}},
  ]}},
  models: [
    {{studio: "voice@mac-a", model: "org/<model>", revision: "sha256:abc", reason: "bad <worker>", error_code: "worker_failed", failures: 2, blocked: true, blocked_at: 1720000000}},
    {{studio: "voice@mac-a", model: "second", revision: "r2", reason: "retry", error_code: "timeout", failures: 1, blocked: false}},
  ]
}});
console.log(JSON.stringify({{state: nodes["#recovery-state"].textContent, auto: nodes["#recovery-auto"].innerHTML, body: nodes["#recovery-body"].innerHTML}}));
"""
    result = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)
    rendered = json.loads(result.stdout)
    assert rendered["state"] == "2 quarantined · 1 blocked"
    assert "safe &lt;detail&gt;" in rendered["auto"]
    assert "1 tracked job" in rendered["auto"]
    assert "voice@mac-a" in rendered["body"]
    assert "org/&lt;model&gt;" in rendered["body"]
    assert "bad &lt;worker&gt;" in rendered["body"]
    assert "Recheck readiness" in rendered["body"]
    assert "Repair the affected model, then recheck readiness." in rendered["body"]
    assert "Models" in rendered["body"] and "Downloads" in rendered["body"]
    assert "Automatic worker recovery" in rendered["body"]
    assert "phase cancel_requested" in rendered["body"]
    assert "Cancellation accepted for the exact job" in rendered["body"]
    assert "cooldown" not in rendered["body"].lower()
    assert "restart remaining" not in rendered["body"].lower()
    assert "<b>org/<model>" not in rendered["body"]


def test_remote_voice_recovery_only_offers_result_recheck():
    source = FRONTEND.read_text(encoding="utf-8")
    helper = _javascript(source, "function voiceRecoveryActions", "async function loadGenerationItems")
    program = f"""
const jsq = value => JSON.stringify(String(value ?? "")).slice(1, -1);
{helper}
const remote = voiceRecoveryActions("batch-1", {{index: 0, studio: "voice@worker-a", state: "uncertain"}}, "voice");
const local = voiceRecoveryActions("batch-1", {{index: 1, studio: "voice", state: "uncertain"}}, "voice");
console.log(JSON.stringify({{remote, local}}));
"""
    result = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)
    rendered = json.loads(result.stdout)
    assert "Recheck result" in rendered["remote"]
    assert "Restart Voice service" not in rendered["remote"]
    assert "Recheck result" in rendered["local"]
    assert "Restart Voice service" in rendered["local"]


def test_recovery_recheck_uses_the_new_endpoint_and_surfaces_409_detail():
    source = FRONTEND.read_text(encoding="utf-8")
    assert 'api("/api/hub/recovery")' in source
    assert 'api("/api/hub/recovery/models/recheck"' in source
    assert 'response?.status === 409' in source
    assert 'body: JSON.stringify({ studio, model })' in source
