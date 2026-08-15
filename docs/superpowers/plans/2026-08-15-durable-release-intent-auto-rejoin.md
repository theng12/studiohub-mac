# Durable Release Intent and Automatic Rejoin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Give every Studio Hub controller a durable, exact-commit site release job that automatically resumes returning machines without changing ordinary automatic-update behaviour.

**Architecture:** Reuse each app's \`AutoUpdater\` for exact-component safety: it already owns locks, clean checkout, dependencies, restart, and rollback. Add one controller \`ReleaseReconciler\` that persists immutable GenStudio intent separately from terminal \`fleet_ops\` history, executes serial agent bundles, adopts child jobs after restart, triggers catalog reconciliation, and exposes cache-only quarantine evidence.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, asyncio, httpx, pytest, existing AutoUpdater and atomic JSON. No new dependency, launcher, cloud API, or worker workload.

**Implementation status (2026-08-15):** Tasks 1–7 are implemented and focused
verified in the shared release worktree. Task 8 release metadata and the full
812-test/syntax/dependency verification matrix are complete; final review,
commit, push, controlled canary, and GenStudio activation remain deliberately
pending.

## Global Constraints

- Release \`2.8.0\` with matching \`VERSION\`, dated \`CHANGELOG.md\`, and frontend \`RELEASE_NOTES\` on \`2026-08-15\`.
- Manage only Hub plus installed Image/Voice; never install or remove legacy siblings.
- GenStudio owns immutable target/global site ordering; Hub owns only local execution and credentials.
- Managed work always has exact \`target_commit\`, \`target_version\`, and \`operation_id\`; no managed code invokes moving-main \`update.js\`.
- Require canonical origin, clean main checkout, requested-SHA ancestry, target-tree version, exact loaded version, and exact loaded SHA.
- First reachable remote stable machine is canary; remote Hub/Image/Voice serially; remaining agents stable-ID serially; controller Image/Voice then Hub last.
- Offline/busy/disk/auth/local failures are durable pending/nonblocking. Manifest identity/SHA mismatch or the same clean-checkout health failure on two machines blocks only the frozen release.
- Keep normal \`Off\`/\`Notify\`/\`Auto\`, manual endpoints, existing \`fleet_ops\` terminal history, and two-good-probe health behaviour unchanged.
- Persist or publish no token, command, path, customer data, or prompt.
- Tasks 1--7 are uncommitted checkpoints. Make exactly one reviewed, versioned
  `2.8.0` release commit in Task 8; do not use unversioned checkpoint commits.
- Commit/push/canary only after full review and verification.

## File structure

| File | Responsibility |
| --- | --- |
| \`app/backend/release_reconciliation.py\` | canonical manifest, atomic desired/job state, due retries, serial controller orchestration |
| \`app/backend/auto_update.py\` | exact-target tuple, target preflight/merge/attestation, idempotent busy defer |
| \`app/backend/auto_update_config.py\` | constructor metadata only if AutoUpdater needs the safe app-commit reader |
| \`app/backend/main.py\` | Pydantic bodies, authenticated routes, lifespan, app commit, catalog request |
| \`app/backend/fleet_ops.py\` | immutable Hub target propagation; retain legacy history semantics |
| \`app/backend/fleet_auto_updates.py\` | managed tuple dispatch/attestation without changing ordinary inventory |
| \`app/backend/peers.py\` | recovery signal and authenticated child-job delivery/adoption |
| \`app/backend/capabilities.py\` | schema-v3 additive release evidence/quarantine |
| \`app/frontend/index.html\` | separate status-only Managed release panel |
| \`app/tests/test_release_reconciliation.py\` | intent/state/order/retry/adoption/redaction |
| \`app/tests/test_auto_update.py\` | tuple/preflight/rollback/idempotency |
| \`app/tests/test_fleet_ops.py\`, \`test_fleet_auto_updates.py\`, \`test_peers.py\` | propagation and existing-flow regression |
| \`app/tests/test_api.py\`, \`test_capabilities.py\` | authenticated routes and schema-v3 gate |

## Shared interfaces

\`\`\`python
# app/backend/auto_update.py
def trigger_update(
    self, *, after_current: bool = False,
    target_commit: str | None = None,
    target_version: str | None = None,
    operation_id: str | None = None,
) -> dict: ...

# all managed fields are all-or-none; status stores requested/started/
# completed/rollback commits, target version, and operation ID.

# app/backend/release_reconciliation.py
class ReleaseReconciler:
    def replace_intent(self, manifest: dict) -> tuple[bool, dict]: ...
    def activate(self, release_id: str, *, genstudio_run_reference: str | None) -> dict: ...
    def intent_snapshot(self) -> dict: ...
    def job_snapshot(self, job_id: str) -> dict | None: ...
    def note_peer_recovered(self, machine: str) -> None: ...
    def start(self) -> None: ...
    async def stop(self) -> None: ...
\`\`\`

A managed component result is:

\`\`\`json
{"component":"voice","state":"current","observed_version":"<component-release-semver>",
 "observed_commit":"<40-hex>","failure_class":null,"retryable":false}
\`\`\`

Agent \`POST /api/hub/maintenance/managed-update\` receives a stored \`release_id\` plus component targets and returns \`202 {site_id, controller_id, job_id, adopted}\`. Image/Voice must advertise \`managed_exact_commit: true\` before they can be marked current.

---

### Task 1: Characterize existing moving-main and terminal-history behaviour

**Files:**
- Modify: \`app/tests/test_auto_update.py\`
- Modify: \`app/tests/test_fleet_ops.py\`
- Modify: \`app/tests/test_fleet_auto_updates.py\`

**Interfaces:**
- Consumes: existing \`AutoUpdater\`, \`fleet_ops._load_state()\`, and FleetAutoUpdates resume.
- Produces: regression coverage proving the new desired state cannot reuse terminal fleet history.

- [ ] **Step 1: Write characterization tests**

\`\`\`python
def test_legacy_fleet_ops_restart_history_is_not_managed_desired_state(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_ops, "_STATE_FILE", tmp_path / "fleet_versions.json")
    fleet_ops._hub_updates["legacy"] = {"id": "legacy", "status": "running", "items": []}
    fleet_ops._save_state(); fleet_ops._hub_updates.clear(); fleet_ops._load_state()
    assert fleet_ops._hub_updates["legacy"]["restart_interrupted"] is True
    assert fleet_ops._hub_updates["legacy"]["status"] == "failed"

def test_current_auto_updater_has_no_managed_tuple():
    assert "target_commit" not in inspect.signature(AutoUpdater.trigger_update).parameters
\`\`\`

- [ ] **Step 2: Run characterization tests**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_fleet_ops.py app/tests/test_auto_update.py -k 'managed_desired_state or managed_tuple'\`

Expected: PASS before implementation. This locks the trace finding: fleet_ops terminalizes interrupted work and AutoUpdater currently follows main.

- [ ] **Step 3: Preserve current legacy behaviour**

Keep the existing interrupted-history assertion. Do not change \`fleet_ops._load_state()\`; the new reconciler owns its separate state.

- [ ] **Step 4: Run rolling regression**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_fleet_ops.py app/tests/test_fleet_auto_updates.py\`

Expected: PASS.

- [ ] **Step 5: Checkpoint without committing**

Keep these tests as uncommitted reviewed work. They are staged only with the
single versioned release in Task 8.

### Task 2: Extend AutoUpdater with a durable exact-target operation

**Files:**
- Modify: \`app/backend/auto_update.py\`
- Modify: \`app/backend/main.py\`
- Modify: \`app/tests/test_auto_update.py\`
- Modify: \`app/tests/test_api.py\`

**Interfaces:**
- Consumes: current \`_git_preflight\`, flock/status persistence, restart, rollback.
- Produces: managed \`trigger_update\` tuple and \`app_commit\` attestation.

- [ ] **Step 1: Write failing exact-target tests**

\`\`\`python
def test_managed_target_requires_all_three_fields(updater):
    with pytest.raises(UpdateError, match="all be provided"):
        updater.trigger_update(target_commit="a" * 40)

def test_managed_target_merges_requested_sha_not_origin_main(monkeypatch, updater):
    calls = fake_clean_git(monkeypatch, target="a" * 40, version="2.8.0")
    updater.trigger_update(target_commit="a" * 40, target_version="2.8.0", operation_id="op-1")
    assert ("merge", "--ff-only", "a" * 40) in calls
    assert ("merge", "--ff-only", "origin/main") not in calls

def test_same_operation_adopts_different_active_target_conflicts(updater):
    first = updater.trigger_update(target_commit="a" * 40, target_version="2.8.0", operation_id="op-1")
    assert updater.trigger_update(target_commit="a" * 40, target_version="2.8.0", operation_id="op-1")["operation_id"] == first["operation_id"]
    with pytest.raises(UpdateError, match="active managed"):
        updater.trigger_update(target_commit="b" * 40, target_version="2.8.1", operation_id="op-2")
\`\`\`

- [ ] **Step 2: Verify the tests fail**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_auto_update.py -k 'managed_target or same_operation'\`

Expected: FAIL because no managed tuple exists.

- [ ] **Step 3: Implement all-or-none tuple and exact preflight**

Validate lower-case 40-hex, SemVer, and bounded opaque operation ID. Atomically persist it before spawn; re-read it under existing flock. Retain existing origin/main, clean, rewrite, disk, dependency/import, service restart, and rollback protections. Before mutation require:

\`\`\`python
git("fetch", "origin", "main")
git("rev-parse", f"{target_commit}^{{commit}}")
git("merge-base", "--is-ancestor", target_commit, "origin/main")
git("merge-base", "--is-ancestor", current_head, target_commit)
git("show", f"{target_commit}:VERSION")
git("merge", "--ff-only", target_commit)
\`\`\`

Require target-tree version equality. Persist requested/started/completed/rollback commit fields. A busy retry retains the same tuple and operation; ordinary requests retain existing behavior.

- [ ] **Step 4: Add exact loaded-process attestation**

Capture safe local \`APP_COMMIT\` at process start and include it in \`/api/health\` and \`/api/version\`. Managed success requires health OK plus exact target version and commit; version-only ordinary success remains unchanged.

- [ ] **Step 5: Run focused updater/API tests**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_auto_update.py app/tests/test_api.py app/tests/test_auth.py\`

Expected: PASS, including dirty/wrong-origin/detached/unknown-SHA/not-on-main/local-not-ancestor/version-mismatch/rewrite refusal, rollback, busy persistence, and unchanged normal settings.

- [ ] **Step 6: Checkpoint without committing**

Keep the updater/API work for the single reviewed `2.8.0` release commit in
Task 8; do not create an unversioned intermediate commit.

### Task 3: Propagate frozen targets through Hub/Image/Voice maintenance

**Files:**
- Modify: \`app/backend/fleet_ops.py\`
- Modify: \`app/backend/fleet_auto_updates.py\`
- Modify: \`app/backend/main.py\`
- Modify: \`app/tests/test_fleet_ops.py\`
- Modify: \`app/tests/test_fleet_auto_updates.py\`

**Interfaces:**
- Consumes: Task 2 managed tuple and existing serial/reconnect code.
- Produces: immutable target fields in jobs and exact commit completion; normal Updates inventory stays as-is.

- [ ] **Step 1: Write failing target propagation tests**

\`\`\`python
@pytest.mark.asyncio
async def test_agent_hub_posts_frozen_target_and_rejects_wrong_commit(monkeypatch):
    item = {"machine": "mac-a", "host": "10.0.0.8", "target_commit": "a" * 40, "target_version": "2.8.0", "operation_id": "op-1"}
    posted = await fake_managed_hub(monkeypatch, item, loaded_commit="b" * 40)
    await fleet_ops._update_hub_one(item, "2.8.0")
    assert posted["target_commit"] == "a" * 40
    assert item["status"] == "failed"
    assert "commit" in item["detail"]

def test_managed_selector_keeps_every_installed_image_and_voice(monitor):
    targets = fleet_auto_updates.managed_targets(monitor, manifest())
    assert {row["id"] for row in targets} == {"image@a", "voice@a", "image@b", "voice@b"}
\`\`\`

- [ ] **Step 2: Verify failure**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_fleet_ops.py app/tests/test_fleet_auto_updates.py -k 'frozen_target or wrong_commit or managed_selector'\`

Expected: FAIL because current jobs retain versions only and normal inventory dedupes per repository.

- [ ] **Step 3: Implement a managed-only dispatch path**

Freeze \`repository/version/commit/operation_id\` before the first request. Hub self-update calls \`auto_updater.trigger_update(...)\`, never \`run_hub_script(\"update.js\")\`. Image/Voice receive the tuple through their authenticated auto-update API and must advertise \`managed_exact_commit\`; absence is durable \`retryable_failure\`. Preserve \`start_hub_updates\`, ordinary \`FleetAutoUpdates.targets()\`, and all normal auto-update UI operations unchanged.

- [ ] **Step 4: Run serial/reconnect regressions**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_fleet_ops.py app/tests/test_fleet_auto_updates.py app/tests/test_control.py\`

Expected: PASS, including current canary/failure continuation and reconnect to same remote job.

- [ ] **Step 5: Checkpoint without committing**

Keep target-propagation work with the final reviewed release; no intermediate
commit is allowed.

### Task 4: Implement atomic intent state and restart adoption

**Files:**
- Create: \`app/backend/release_reconciliation.py\`
- Create: \`app/tests/test_release_reconciliation.py\`
- Modify: \`app/tests/conftest.py\`
- Modify: \`.gitignore\`

**Interfaces:**
- Consumes: Tasks 2–3, \`monitor.registry\`, \`peers.cached\`.
- Produces: atomic \`release_reconciliation.json\`, validated intent, adopted job, due retry state.

- [ ] **Step 1: Write failing intent/adoption tests**

\`\`\`python
def test_intent_is_canonical_atomic_and_idempotent(tmp_path, monitor):
    service = ReleaseReconciler(monitor, state_path=tmp_path / "release.json")
    manifest = release_manifest(sequence=12)
    assert service.replace_intent(manifest)[0] is True
    assert service.replace_intent(manifest)[0] is False
    with pytest.raises(ValueError, match="release_id"):
        service.replace_intent(tamper_manifest(manifest))

def test_restart_adopts_nonterminal_job_not_fleet_ops_history(tmp_path, monitor):
    first = configured_reconciler(tmp_path, monitor)
    job = first.activate(RELEASE_ID, genstudio_run_reference=None)
    first.persist_remote_job(job["id"], "mac-a", "agent-job")
    second = configured_reconciler(tmp_path, monitor)
    assert second.resume_pending() == 1
    assert second.job_snapshot(job["id"])["machines"]["mac-a"]["agent_job_id"] == "agent-job"

def test_degraded_pending_work_survives_restart_and_due_scan(tmp_path, monitor, clock):
    first = configured_reconciler(tmp_path, monitor, clock=clock)
    job = first.activate(RELEASE_ID, genstudio_run_reference=None)
    first.record_component(job["id"], "mac-a", "voice", state="pending_offline", next_retry=clock.now())
    first.persist_job(job["id"], state="degraded")
    second = configured_reconciler(tmp_path, monitor, clock=clock)
    assert second.job_snapshot(job["id"])["state"] == "degraded"
    assert second.job_snapshot(job["id"])["finished_at"] is None
    assert second.resume_due() == 1

@pytest.mark.asyncio
async def test_restart_matrix_adopts_one_release_job_at_every_boundary(tmp_path, monitor):
    # Parameterize after intent receipt, after activation, between components,
    # during remote-Hub restart, after lost POST, and during poll transport loss.
    for boundary in RESTART_BOUNDARIES:
        service, fault = configured_reconciler_with_restart_fault(tmp_path, monitor, boundary)
        job_id = await service.activate_and_run(RELEASE_ID, fault=fault)
        resumed = configured_reconciler(tmp_path, monitor)
        await resumed.resume_pending()
        assert resumed.job_snapshot(job_id)["id"] == job_id
        assert child_execution_count(resumed, job_id) == 1
\`\`\`

- [ ] **Step 2: Verify failure**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_release_reconciliation.py -k 'intent or restart'\`

Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement canonical durable record**

Hash sorted-key/no-whitespace JSON with \`release_id\` omitted. Reject lower sequence, unknown component/repository, malformed timestamp/version/SHA, changed same-ID replay before mutation. Atomically replace a sibling temporary file containing desired manifest, activation, jobs, per-stable-machine host evidence/child ID/operation ID, expected/observed version/SHA, state, attempt, sanitized error, and retry time. Add state to \`.gitignore\`. Only \`complete\` and \`blocked_release\` are terminal: \`degraded\` is explicitly nonterminal while any row is pending/retryable/auth-blocked, has no \`finished_at\`, and retains a persisted \`next_retry\`. Use retry delays 60, 300, 900, 3600, 14400, 86400 seconds and a 15-minute due scan.

- [ ] **Step 4: Run focused state tests**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_release_reconciliation.py -k 'intent or restart or redaction or retry'\`

Expected: PASS; no token/path/command is serialized. The restart matrix covers after intent receipt, after activation, between components, remote-Hub restart, lost POST/poll response, and controller-Hub-last restart; each resumes one persisted job without duplicate execution.

- [ ] **Step 5: Checkpoint without committing**

Keep durable-state work with the final reviewed release; no intermediate
commit is allowed.

### Task 5: Execute serial bundles and automatic recovery

**Files:**
- Modify: \`app/backend/release_reconciliation.py\`
- Modify: \`app/backend/peers.py\`
- Modify: \`app/tests/test_release_reconciliation.py\`
- Modify: \`app/tests/test_peers.py\`

**Interfaces:**
- Consumes: Task 4 durable state and Task 3 exact component jobs.
- Produces: serial canary/controller-last sequence, target-local pending recovery, release-wide block, duplicate-safe child job adoption.

- [ ] **Step 1: Write failing ordering/failure tests**

\`\`\`python
@pytest.mark.asyncio
async def test_canary_agents_are_serial_then_controller_is_last(monkeypatch, reconciler):
    events = await record_release_events(monkeypatch, reconciler)
    await reconciler.run(RELEASE_ID)
    assert events == ["mac-a:hub", "mac-a:image", "mac-a:voice", "mac-b:hub", "mac-b:image", "mac-b:voice", "local:image", "local:voice", "local:hub"]

@pytest.mark.asyncio
async def test_offline_busy_auth_are_pending_but_later_machine_runs(monkeypatch, reconciler):
    job = await run_states(reconciler, {"mac-a": "pending_offline", "mac-b": "current"})
    assert job["state"] == "degraded"
    assert job["finished_at"] is None
    assert job["machines"]["mac-a"]["components"]["hub"]["next_retry"] is not None
    assert job["machines"]["mac-b"]["components"]["hub"]["state"] == "current"

@pytest.mark.asyncio
async def test_lost_managed_update_post_response_replays_one_operation(monkeypatch, reconciler):
    # The agent commits the child job, then the first HTTP response is lost.
    post = fake_agent_post_losing_first_response(monkeypatch, reconciler)
    await reconciler.run(RELEASE_ID)
    assert post.calls == 2
    assert post.bodies[0] == post.bodies[1]
    assert post.bodies[0]["operation_id"] == operation_id(RELEASE_ID, "mac-a", "managed-update")
    assert post.executions == 1
    assert reconciler.job_snapshot_for_machine(RELEASE_ID, "mac-a")["agent_job_id"] == "agent-job-1"

@pytest.mark.asyncio
async def test_second_clean_health_failure_blocks_remaining_fanout(monkeypatch, reconciler):
    job = await run_cross_machine_clean_failures(reconciler)
    assert job["state"] == "blocked_release"
    assert all(row["state"] == "release_blocked" for row in remaining_components(job))

@pytest.mark.asyncio
async def test_controller_hub_last_restart_attests_and_finalizes_same_job(monkeypatch, tmp_path, monitor):
    first = configured_reconciler(tmp_path, monitor)
    job = await first.run_until_controller_hub_restart(RELEASE_ID)
    # Persisted expected local tuple is all a newly started controller can trust.
    assert job["machines"]["local"]["components"]["hub"]["expected_commit"] == HUB_SHA
    restarted = configured_reconciler(tmp_path, monitor, loaded_version="2.8.0", loaded_commit=HUB_SHA)
    await restarted.resume_pending()
    current = restarted.job_snapshot(job["id"])
    assert current["machines"]["local"]["components"]["hub"]["state"] == "current"
    assert controller_hub_update_calls(restarted) == 0
    assert current["catalog"]["requested_at"] is not None
\`\`\`

- [ ] **Step 2: Verify failure**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_release_reconciliation.py -k 'canary or pending or blocks'\`

Expected: FAIL because site jobs do not yet execute.

- [ ] **Step 3: Implement ordered bundles**

Group registry entries by stable \`machine\`; choose first reachable remote canary, sort remaining remote IDs, append local. Derive \`operation_id\` deterministically from canonical release ID + stable machine ID + \`managed-update\`; POST the bundle with it. The agent persists the operation and child job before responding. If the first response is lost, repeat the identical POST until it returns that existing \`job_id\` with \`adopted: true\`, then persist the ID before polling. Repeating the HTTP request is permitted; starting a second child execution is not. Local runs Image then Voice then Hub. Missing app is \`not_installed\`; offline/busy/disk/auth/local rows retry while later machines continue. Manifest identity/SHA mismatch or a second distinct \`clean_checkout_health_failure\` marks remaining rows \`release_blocked\` and stops fanout.

- [ ] **Step 4: Schedule only recovered target**

At the existing peer down-to-healthy transition, lazily call \`release_reconciler.note_peer_recovered(machine)\`. It marks that known target due now; it never creates an intent or global authority.

- [ ] **Step 5: Run scheduler/peer and controller-restart regressions**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_release_reconciliation.py app/tests/test_peers.py app/tests/test_fleet_ops.py\`

Expected: PASS, including controller-Hub-last startup attestation: persisted expected version/SHA plus newly loaded app version/SHA marks the same local component current, requests catalog reconciliation, and never replays its self-update.

- [ ] **Step 6: Checkpoint without committing**

Keep serial-recovery work with the final reviewed release; no intermediate
commit is allowed.

### Task 6: Wire authenticated APIs, lifespan, and catalog request

**Files:**
- Modify: \`app/backend/main.py\`
- Modify: \`app/backend/release_reconciliation.py\`
- Modify: \`app/tests/test_api.py\`
- Modify: \`app/tests/test_model_baselines.py\`

**Interfaces:**
- Consumes: Tasks 4–5 and \`FleetModelBaselines.reconcile()\`.
- Produces: documented intent/activation/job routes, startup adoption, catalog-request evidence.

- [ ] **Step 1: Write failing API/lifecycle tests**

\`\`\`python
def test_release_intent_requires_machine_auth_and_controller_role(client, authed):
    assert client.put("/api/hub/maintenance/release-intent", json=release_manifest()).status_code == 401
    assert authed.put("/api/hub/maintenance/release-intent", json=release_manifest()).status_code == 409
    configure_controller()
    assert authed.put("/api/hub/maintenance/release-intent", json=release_manifest()).status_code == 200

def test_activation_replay_adopts_one_job(authed):
    configure_controller_and_intent(authed)
    a = authed.post(f"/api/hub/maintenance/release-intent/{RELEASE_ID}/activate")
    b = authed.post(f"/api/hub/maintenance/release-intent/{RELEASE_ID}/activate")
    assert a.status_code == b.status_code == 202
    assert a.json()["job_id"] == b.json()["job_id"]

def test_release_writes_require_machine_token_not_owner_cookie(owner_browser, controller_token, agent_token):
    owner_browser.cookies.set("studiohub_session", "owner-session")
    assert owner_browser.put("/api/hub/maintenance/release-intent", json=release_manifest()).status_code in {401, 403}
    assert controller_token.put("/api/hub/maintenance/release-intent", json=release_manifest()).status_code == 200
    assert controller_token.post(f"/api/hub/maintenance/release-intent/{RELEASE_ID}/activate").status_code == 202
    assert owner_browser.post("/api/hub/maintenance/managed-update", json=managed_bundle()).status_code in {401, 403}
    assert agent_token.post("/api/hub/maintenance/managed-update", json=managed_bundle()).status_code == 202
    assert agent_token.put("/api/hub/maintenance/release-intent", json=release_manifest()).status_code == 403
    assert controller_token.post("/api/hub/maintenance/managed-update", json=managed_bundle()).status_code == 403

def test_release_write_rejects_missing_invalid_and_cross_origin_machine_credentials(client, controller_token):
    assert client.put("/api/hub/maintenance/release-intent", json=release_manifest()).status_code == 401
    assert client.put("/api/hub/maintenance/release-intent", headers={"X-Hub-Token": "bad"}, json=release_manifest()).status_code == 401
    assert controller_token.put("/api/hub/maintenance/release-intent", headers={"Origin": "https://attacker.invalid"}, json=release_manifest()).status_code == 403
\`\`\`

- [ ] **Step 2: Verify failure**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_api.py -k 'release_intent or managed_update'\`

Expected: FAIL with 404.

- [ ] **Step 3: Add narrow routes and lifecycle**

Require \`X-Hub-Token\` validated as a machine credential plus controller role for intent/activation; agent role for managed-update. These writes must not accept an owner browser session/cookie, and CORS/origin policy must still reject cross-origin browser writes. Return configured site/controller identity. In lifespan construct the reconciler at \`DATA_DIR / \"release_reconciliation.json\"\`, start after monitor setup, and stop before client shutdown. On software convergence call \`await model_baselines.reconcile()\` once and record requested revision/count/timestamps only; never claim downloading models complete.

- [ ] **Step 4: Run focused regressions**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_api.py app/tests/test_auth.py app/tests/test_control_plane.py app/tests/test_model_baselines.py app/tests/test_release_reconciliation.py\`

Expected: PASS.

- [ ] **Step 5: Checkpoint without committing**

Keep route/lifecycle work with the final reviewed release; no intermediate
commit is allowed.

### Task 7: Publish schema-v3 evidence and status-only UI

**Files:**
- Modify: \`app/backend/capabilities.py\`
- Modify: \`app/backend/main.py\`
- Modify: \`app/frontend/index.html\`
- Modify: \`app/tests/test_capabilities.py\`
- Create: \`app/tests/test_frontend_release_reconciliation.py\`

**Interfaces:**
- Consumes: \`ReleaseReconciler.capability_evidence()\`.
- Produces: additive schema-v3 controller/machine/worker state and separate Managed release card.

- [ ] **Step 1: Write failing evidence/UI tests**

\`\`\`python
def test_pending_managed_release_quarantines_worker(authed, monitor):
    _seed_capability_site(monitor); seed_release(image="pending_offline", voice="current")
    payload = authed.get("/api/hub/capabilities").json()
    assert payload["schema_version"] == 3
    assert _model(_worker(payload, "image"), "image.text_to_image")["availability"]["reason"] == "managed_release_pending"

def test_updates_has_distinct_managed_release_card():
    source = (ROOT / "app/frontend/index.html").read_text()
    assert 'id="managed-release-card"' in source
    assert 'api("/api/hub/maintenance/release-intent")' in source
\`\`\`

- [ ] **Step 2: Verify failure**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_capabilities.py -k managed_release\`

Run: \`conda_env/bin/python -m pytest -q app/tests/test_frontend_release_reconciliation.py\`

Expected: FAIL because current schema is v2 and card is absent.

- [ ] **Step 3: Add additive evidence/gate**

Set schema v3. Publish sanitized desired/expected/observed/state/retry/catalog values at controller/machine/worker levels. With active desired intent, require current Hub and worker component before model \`available_now\`; use exactly \`managed_release_pending\`, \`managed_release_blocked\`, or \`managed_release_mismatch\`. Preserve old reason precedence when intent is absent/current. Render only status in the new card; it may not activate/retry/reconfigure ordinary updater controls.

- [ ] **Step 4: Run evidence/UI regressions**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_capabilities.py app/tests/test_broker.py app/tests/test_frontend_typography.py app/tests/test_frontend_release_reconciliation.py\`

Expected: PASS.

- [ ] **Step 5: Checkpoint without committing**

Keep schema/UI work with the final reviewed release; no intermediate commit is
allowed.

### Task 8: Release, verify, canary, and hand off

**Files:**
- Modify: \`VERSION\`
- Modify: \`CHANGELOG.md\`
- Modify: \`README.md\`
- Modify: \`CAPABILITY_CONTRACT.md\`
- Modify: \`studiohub_genstudio_integration.md\`
- Modify: \`app/frontend/index.html\`
- Modify: \`docs/superpowers/specs/2026-08-15-durable-release-intent-auto-rejoin-design.md\`
- Modify: \`docs/superpowers/plans/2026-08-15-durable-release-intent-auto-rejoin.md\`

**Interfaces:**
- Consumes: Tasks 1–7 and accepted Image/Voice exact-update capability releases.
- Produces: Studio Hub 2.8.0, documented cross-repo contract, controlled canary, return handoff.

- [x] **Step 1: Write failing release-doc test**

\`\`\`python
def test_managed_release_contract_is_documented():
    assert "/api/hub/maintenance/release-intent" in (ROOT / "README.md").read_text()
    assert "schema version 3" in (ROOT / "CAPABILITY_CONTRACT.md").read_text().lower()
\`\`\`

- [x] **Step 2: Verify failure**

Run: \`conda_env/bin/python -m pytest -q app/tests/test_release_metadata.py -k managed_release_contract\`

Expected: FAIL before documentation update.

- [x] **Step 3: Release metadata/docs**

Set \`VERSION=2.8.0\`. Add dated changelog and RELEASE_NOTES entries for immutable intent, serial canary/controller-last, nonblocking retry, exact SHA attestation, schema-v3 quarantine, unchanged ordinary updater controls, and PPS bootstrap boundary. Document API/auth/state/reasons in README, CAPABILITY_CONTRACT, and integration doc.

- [x] **Step 4: Full verification**

Run: \`conda_env/bin/python -m pytest -q\`

Run: \`conda_env/bin/python -m compileall -q app\`

Run: \`for file in *.js; do node --check \"$file\"; done\`

Run: \`for file in *.sh; do bash -n \"$file\"; done\`

Run: \`conda_env/bin/python -m pip check\`

Run: \`git diff --check\`

Expected: PASS; version/date agree in VERSION, CHANGELOG, RELEASE_NOTES.

- [ ] **Step 5: Review then commit and push**

\`\`\`bash
git add \\
  .gitignore \\
  VERSION CHANGELOG.md README.md CAPABILITY_CONTRACT.md studiohub_genstudio_integration.md \\
  app/backend/auto_update.py \\
  app/backend/auto_update_config.py \\
  app/backend/capabilities.py \\
  app/backend/fleet_auto_updates.py \\
  app/backend/fleet_ops.py \\
  app/backend/main.py \\
  app/backend/peers.py \\
  app/backend/release_reconciliation.py \\
  app/frontend/index.html \\
  app/tests/conftest.py \\
  app/tests/test_api.py \\
  app/tests/test_auto_update.py \\
  app/tests/test_capabilities.py \\
  app/tests/test_fleet_auto_updates.py \\
  app/tests/test_fleet_ops.py \\
  app/tests/test_frontend_release_reconciliation.py \\
  app/tests/test_model_baselines.py \\
  app/tests/test_peers.py \\
  app/tests/test_release_metadata.py \\
  app/tests/test_release_reconciliation.py \\
  docs/superpowers/specs/2026-08-15-durable-release-intent-auto-rejoin-design.md \\
  docs/superpowers/plans/2026-08-15-durable-release-intent-auto-rejoin.md \\
  docs/superpowers/plans/2026-08-15-image-voice-exact-commit-updater-contract.md \\
  docs/superpowers/plans/2026-08-15-durable-release-integration-rollout.md
git status --short
git diff --cached --name-status
git diff --cached --check
git commit -m "release: reconcile durable fleet releases"
git push origin main
\`\`\`

Do this only after review and Image/Voice handoffs prove their exact-update capability. The staged list is the release boundary: if a listed file was not changed, omit it from the command; if any unlisted path appears in \`git diff --cached --name-status\`, unstage it and resolve the discrepancy before committing. Do not roll the fleet in this step.

- [ ] **Step 6: Controlled canary and GenStudio handoff**

Use GenStudio to activate one reachable controlled site. Verify requested SHA/version survives Hub restart, child jobs adopt, schema-v3 leaves only exact/current workers routable, normal updater settings stay unchanged, and catalog evidence is requested. Write \`/Users/thengmacmini/Developer/_handoffs/2026-08-15_to-claude-genstudio_from-gpt-studiofleet_studiohub-2.8.0-release-intent-ready.md\` with version/commit, endpoints/fields, evidence/reasons, sibling capability versions, verification/canary result, and PPS boundary; include no credential/customer data.

## Plan self-review

- Existing moving-main and terminalized FleetOps jobs are characterized and explicitly avoided: Task 1.
- Exact SHA/version/origin/health/deferred-idempotent updater: Task 2.
- Immutable target propagation and rejection of same-version/wrong-SHA success: Task 3.
- Atomic desired intent, restart adoption, serial canary, pending retry, and release-wide block: Tasks 4–5.
- Authenticated APIs, catalog request, schema-v3 quarantine, and separate UI: Tasks 6–7.
- Release discipline, full verification, controlled canary, and GenStudio handoff: Task 8.

There is no placeholder target, latest-GitHub fallback, hidden setting change, unbounded retry, or claim that PPS is repaired before it returns.
