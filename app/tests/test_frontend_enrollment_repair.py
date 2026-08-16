import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app" / "frontend" / "index.html"
FIXTURE = ROOT / "app" / "tests" / "browser_fixtures" / "enrollment_repair_remote_fixture.py"


def _frontend_source() -> str:
    return FRONTEND.read_text()


def test_repair_controls_are_controller_only_and_use_stable_machine_ids():
    source = _frontend_source()

    assert 'settings.role === "controller"' in source
    assert 'repairEligibility[e.m]' in source
    assert 'JSON.stringify({ machines: targets })' in source
    assert 'aria-label="Repair enrollment for ${esc(label)} (${esc(machine)})"' in source


def test_confirmation_copy_lists_preserved_state_and_token_not_changed():
    source = _frontend_source()

    expected = [
        "jobs", "models", "Studio registrations", "names", "settings",
        "owner sessions", "unique Hub token", "location identity",
        "saved parent Controller address", "fleet credential is verified but not changed",
    ]
    assert all(value in source for value in expected)


def test_batch_copy_says_uncertain_mac_is_parked_while_later_macs_continue():
    source = _frontend_source()

    assert "Repairs start one Mac at a time. If a Mac's result is uncertain, it is parked while later Macs continue; its background recovery status may update afterward." in source
    assert "eligible in stable order" in source
    assert "excluded" in source


def test_repair_dialog_has_focus_trap_return_focus_and_keyboard_close():
    source = _frontend_source()

    assert 'id="repair-confirmation"' in source
    assert 'role="dialog"' in source
    assert 'aria-modal="true"' in source
    assert "repairInvoker = document.activeElement" in source
    assert "event.key === \"Escape\"" in source
    assert "event.key === \"Tab\"" in source
    assert "repairInvoker?.focus()" in source


def test_confirm_focus_moves_to_stable_repair_status_and_polling_keeps_it():
    source = _frontend_source()

    confirm = source[
        source.index("async function confirmEnrollmentRepair()"):
        source.index('$("#repair-confirmation").addEventListener')
    ]
    assert "setRepairBatch(batch);" in confirm
    assert "focusRepairStatus(targets[0]);" in confirm
    assert confirm.index("setRepairBatch(batch);") < confirm.index("focusRepairStatus(targets[0]);")

    row = source[source.index("function renderRepairRow("):source.index("function syncRepairSummary()")]
    assert 'class="repair-state" tabindex="-1"' in row
    assert 'id="repair-batch-summary" class="repair-summary" tabindex="-1"' in source

    focus_helper = source[source.index("function focusRepairStatus("):source.index("function patchRepairRows()")]
    assert '[data-repair-machine="${CSS.escape(machine)}"] .repair-state' in focus_helper
    assert '|| $("#repair-batch-summary")' in focus_helper
    assert "focus({ preventScroll: true })" in focus_helper

    patch = source[source.index("function patchRepairRows()") : source.index("function renderRemoteSummary()")]
    assert "const active = document.activeElement" in patch
    assert "const focused = node.contains(active)" in patch
    assert ".focus(" not in patch

    apply_summary = source[source.index("function applySummary(sum)"):source.index("async function pollOnce()")]
    render_summary = source[source.index("function renderRemoteSummary()") : source.index("function repairBatchIsActive(")]
    assert "renderRemoteSummary()" in apply_summary
    assert "patchRepairRows()" in render_summary


def test_repair_status_has_polite_live_region_busy_state_and_persistent_result():
    source = _frontend_source()

    assert 'id="repair-live" role="status" aria-live="polite"' in source
    assert 'aria-busy="${active ? "true" : "false"}"' in source
    assert 'class="repair-result"' in source


def test_repair_rows_stack_on_narrow_screens_and_actions_are_44px():
    source = _frontend_source()

    assert "@media(max-width:600px){.repair-row" in source
    assert ".repair-row{grid-template-columns:1fr" in source
    assert ".repair-action{min-width:44px;min-height:44px" in source


def test_repair_polling_preserves_focus_expansion_and_scroll():
    source = _frontend_source()

    assert "function patchRepairRows()" in source
    assert "document.activeElement" in source
    assert "details.open" in source
    assert "window.scrollTo(scrollX, scrollY)" in source
    assert "machBody.innerHTML" not in source[source.index("function patchRepairRows()"):source.index("async function repairPollOnce()")]


def test_repair_request_state_table_makes_only_retryable_actionable_and_excludes_unresolved_batch_targets():
    source = _frontend_source()

    for state in (
        "queued", "checking", "ticket_issued", "dispatched", "redeemed",
        "applying", "verifying", "updating_hub", "confirmation_pending",
    ):
        assert f"{state}: {{ active: true, action: \"none\", unresolved: true" in source
    assert 'retryable: { active: false, action: "retry", unresolved: false' in source
    assert 'needs_review: { active: false, action: "none", unresolved: true' in source
    assert 'complete: { active: false, action: "none", unresolved: false' in source
    assert 'hub_update_required: { active: false, action: "none", unresolved: false' in source
    assert "!isUnresolvedRepairRequest(repairRequestFor(row.machine))" in source
    assert 'repair.action === "retry"' in source
    assert 'if (request && repair.state !== "hub_update_required") return ""' in source


def test_apply_summary_uses_keyed_repair_patch_instead_of_wholesale_machine_render_during_active_repair():
    source = _frontend_source()

    apply_summary = source[source.index("function applySummary(sum)"):source.index("async function pollOnce()")]
    assert "renderRemoteSummary()" in apply_summary
    assert "renderMachines()" not in apply_summary
    keyed_summary = source[source.index("function renderRemoteSummary()"):source.index("function repairBatchIsActive(")]
    assert "repairBatchIsActive" in keyed_summary
    assert "patchRepairRows()" in keyed_summary
    assert "renderMachines()" in keyed_summary
    patch = source[source.index("function patchRepairRows()"):source.index("function renderRemoteSummary()")]
    assert "document.activeElement" in patch
    assert "details.open" in patch
    assert "window.scrollTo(scrollX, scrollY)" in patch
    eligibility = source[source.index("async function loadRepairEligibility()"):source.index("function openRepairConfirmation(")]
    assert "renderRemoteSummary()" in eligibility
    assert "renderMachines()" not in eligibility


def test_retryable_aggregate_and_keyed_patch_use_the_same_retry_action_contract():
    source = _frontend_source()

    summary = source[source.index("function syncRepairSummary()"):source.index("function patchRepairRows()")]
    patch = source[source.index("function patchRepairRows()"):source.index("function renderRemoteSummary()")]
    assert 'states.filter(row => row.action === "retry").length' in summary
    assert 'const retry = repair.action === "retry"' in patch
    assert 'button.textContent = retry ? "Retry repair" : "Repair enrollment"' in patch
    assert 'button.setAttribute("aria-label", `${retry ? "Retry enrollment repair" : "Repair enrollment"} for ${mlabel(machine)} (${machine})`)' in patch
    assert "repair.retry" not in summary + patch


def test_hub_update_required_recovery_keeps_retry_exclusive_and_allows_a_fresh_ordinary_repair():
    source = _frontend_source()

    action = source[source.index("function renderRepairAction("):source.index("function renderRepairRow(")]
    targets = source[source.index("function repairEligibleMachineIds()"):source.index("function repairRequestFor(")]
    assert 'if (repair.action === "retry")' in action
    assert 'if (request && repair.state !== "hub_update_required") return ""' in action
    assert ">Retry repair</button>" in action
    assert ">Repair enrollment</button>" in action
    assert "!isUnresolvedRepairRequest(repairRequestFor(row.machine))" in targets


def test_feature_disable_hides_all_owner_repair_initiation_paths():
    source = _frontend_source()
    action = source[source.index("function renderRepairAction("):source.index("function renderRepairRow(")]
    summary = source[source.index("function syncRepairSummary()"):source.index("function focusRepairStatus(")]
    patch = source[source.index("function patchRepairRows()"):source.index("function renderRemoteSummary()")]
    confirm = source[source.index("async function confirmEnrollmentRepair()"):source.index('$("#repair-confirmation").addEventListener')]
    focus_gate = source[source.index("function isRepairController()"):source.index("function repairEligibleMachineIds()")]

    assert "repairEligibilityIssuance = data.issuance_enabled === true" in source
    assert "row.eligible && repairEligibilityIssuance" in source
    assert "eligibility?.eligible && repairEligibilityIssuance" in source
    assert "repairEligibility[machine]?.eligible && repairEligibilityIssuance" in source
    assert 'if (!repairEligibilityIssuance) return "";' in action
    assert 'action.classList.toggle("hide", !controller || !repairEligibilityIssuance)' in summary
    assert "if (!repairEligibilityIssuance) button?.remove()" in patch
    assert "function hideRepairInitiation()" in focus_gate
    assert "dialog.contains(document.activeElement)" in focus_gate
    assert 'document.querySelector(\'nav button[data-tab="remote"]\')' in focus_gate
    assert "focus({ preventScroll: true })" in focus_gate
    assert "hideRepairInitiation()" in summary
    assert "repairTargets = []" in focus_gate
    assert "if (!repairEligibilityIssuance || !targets.length)" in confirm
    assert 'class="repair-state-text"' in source
    assert 'class="repair-result"' in source


def test_failed_repair_creation_stays_visible_per_target_and_in_batch_summary():
    source = _frontend_source()
    row = source[source.index("function renderRepairRow("):source.index("function syncRepairSummary()")]
    summary = source[source.index("function syncRepairSummary()"):source.index("function focusRepairStatus(")]
    confirm = source[source.index("async function confirmEnrollmentRepair()"):source.index('$("#repair-confirmation").addEventListener')]

    assert "let repairStartFailures = new Map()" in source
    assert "repairStartFailures.get(machine)" in row
    assert "repairStartFailures.get(machine)" in source[source.index("function patchRepairRows()"):source.index("function renderRemoteSummary()")]
    assert "summary.textContent = repairStartError" in summary
    assert "repairStartFailures.set(machine, repairStartError)" in confirm
    assert "live.textContent = repairStartError" in confirm
    assert "focusRepairStatus(targets[0])" in confirm
    assert "Review the Controller connection and try again." in confirm


def test_browser_fixture_is_loopback_only_and_serves_the_shipped_frontend():
    assert FIXTURE.exists(), "offline repair fixture is required"
    sys.path.insert(0, str(FIXTURE.parent))
    try:
        from enrollment_repair_remote_fixture import create_fixture_server

        server = create_fixture_server(FRONTEND, port=0)
        try:
            host, port = server.server_address[:2]
            assert host == "127.0.0.1"
            url = f"http://{host}:{port}"
            import threading

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            for _ in range(40):
                try:
                    with urllib.request.urlopen(url + "/", timeout=0.2) as response:
                        assert response.read() == FRONTEND.read_bytes()
                    break
                except urllib.error.URLError:
                    time.sleep(0.02)
            else:
                raise AssertionError("fixture did not serve loopback frontend")
            with urllib.request.urlopen(url + "/api/hub/enrollment-repairs/eligibility", timeout=1) as response:
                eligibility = json.load(response)
            assert eligibility["issuance_enabled"] is True
            assert eligibility["machines"][0]["machine"] == "fixture-agent-a"
            request = urllib.request.Request(
                url + "/api/hub/enrollment-repairs",
                data=b'{"machines":["fixture-agent-a"]}',
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                batch = json.load(response)
            assert batch["requests"][0]["target_machine"] == "fixture-agent-a"
            for phase, expected_state, expected_error in (
                ("retryable", "retryable", "offline"),
                ("needs_review", "needs_review", "ambiguous_registry_host"),
            ):
                state_request = urllib.request.Request(
                    url + "/__fixture/state",
                    data=json.dumps({"phase": phase}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(state_request, timeout=1) as response:
                    assert json.load(response) == {"phase": phase, "offline": True}
                with urllib.request.urlopen(url + "/api/hub/enrollment-repairs/fixture-batch", timeout=1) as response:
                    rendered = json.load(response)["requests"][0]
                assert rendered["state"] == expected_state
                assert rendered["error_code"] == expected_error
                with urllib.request.urlopen(url + "/api/hub/enrollment-repairs/eligibility", timeout=1) as response:
                    terminal_eligibility = json.load(response)["machines"][0]
                assert terminal_eligibility["request_state"] == expected_state
                assert terminal_eligibility["code"] == expected_error
                if phase == "needs_review":
                    assert rendered["evidence"] == {"conflict": "sanitized fixture evidence"}
            for phase, expected_code in (
                ("start_failure", "repair_request_rejected"),
                ("issuance_disabled", "repair_issuance_disabled"),
            ):
                state_request = urllib.request.Request(
                    url + "/__fixture/state",
                    data=json.dumps({"phase": phase}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(state_request, timeout=1) as response:
                    assert json.load(response) == {"phase": phase, "offline": True}
                with urllib.request.urlopen(url + "/api/hub/enrollment-repairs/eligibility", timeout=1) as response:
                    gated = json.load(response)
                assert gated["issuance_enabled"] is (phase != "issuance_disabled")
                failed_request = urllib.request.Request(
                    url + "/api/hub/enrollment-repairs",
                    data=b'{"machines":["fixture-agent-a"]}',
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with __import__("pytest").raises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(failed_request, timeout=1)
                assert failure.value.code == 503
                assert json.load(failure.value)["detail"]["code"] == expected_code
            with __import__("pytest").raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + "/unexpected/outbound", timeout=1)
            assert error.value.code == 404
        finally:
            server.shutdown()
            server.server_close()
    finally:
        sys.path.remove(str(FIXTURE.parent))
