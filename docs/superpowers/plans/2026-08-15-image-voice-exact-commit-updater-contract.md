# Image and Voice Exact-Commit Updater Contract Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Release matching Image Studio and Voice Studio updater capabilities so Studio Hub can command an exact approved commit rather than moving main.

**Architecture:** Reuse each repository's AutoUpdater, update lock, preflight, dependency/install, service restart, rollback, and busy-after-current state. Add one all-or-none managed target tuple to the existing authenticated update endpoint. No launcher change and no new dependency.

**Repositories:** Image: /Users/thengmacmini/pinokio/api/imagestudio-mac. Voice: /Users/thengmacmini/pinokio/api/voicestudio-mac.git.

**Release discipline:** Implement and verify each repository independently, then
make one final reviewed versioned release commit in that repository containing
its code, tests, `VERSION`, dated changelog, and frontend release notes. Do not
make an unversioned checkpoint commit. The actual Image and Voice SemVer values
are outputs of those separate releases and are reported in the handoff; neither
is assumed to be `2.0.0` or derived from Hub `2.8.0`.

## Shared Contract

~~~json
{"after_current":true,"target_commit":"<40-lowercase-hex>",
 "target_version":"<semver>","operation_id":"<bounded-opaque-id>"}
~~~

All managed fields are required together. Existing requests with only after_current stay ordinary moving-main controls. Status includes requested_commit, requested_version, operation_id, started_commit, completed_commit, and rollback_commit. Same operation/target adopts; different active target is 409.

Before mutation require canonical expected origin, main branch, clean checkout, fetch origin main, requested SHA resolves, target is ancestor of origin/main, current HEAD is ancestor of target, target VERSION equals target_version, and no remote rewrite. Merge only git merge --ff-only target_commit. Success requires health OK with app_version == target_version and app_commit == target_commit. Health/version or update status exposes app_commit and managed_exact_commit true. Busy defer persists the exact tuple. Rollback runs once with source/target recorded.

### Task 1: Implement and release Image Studio capability

**Files:**
- Modify: Image app/backend/auto_update.py
- Modify: Image app/backend/main.py
- Modify: Image app/tests/test_auto_update.py
- Modify: Image app/tests/test_api.py
- Modify: Image VERSION, CHANGELOG.md, frontend release notes.

**Interfaces:**
- Consumes: existing Image AutoUpdater._git_preflight, status persistence, service restart, rollback.
- Produces: trigger_update(after_current, target_commit, target_version, operation_id) plus commit attestation.

- [ ] **Step 1: Write failing managed tuple tests**

~~~python
def test_managed_target_requires_all_fields(updater):
    with pytest.raises(UpdateError, match="all be provided"):
        updater.trigger_update(target_commit="a" * 40)

def test_managed_target_only_merges_requested_sha(monkeypatch, updater):
    calls = fake_clean_git(monkeypatch, target="a" * 40, version=IMAGE_TARGET_VERSION)
    updater.trigger_update(target_commit="a" * 40, target_version=IMAGE_TARGET_VERSION, operation_id="op-1")
    assert ("merge", "--ff-only", "a" * 40) in calls

def test_managed_success_requires_exact_loaded_commit(client):
    assert "app_commit" in client.get("/api/health").json()
~~~

- [ ] **Step 2: Run and verify failure**

Run: conda_env/bin/python -m pytest -q app/tests/test_auto_update.py app/tests/test_api.py -k 'managed_target or app_commit'

Expected: FAIL because Image accepts only after_current.

- [ ] **Step 3: Implement exact target in Image AutoUpdater and route**

Thread the tuple from AutoUpdateRequestBody through automatic_update_run to trigger_update. Use list-form Git commands under existing flock; retain checks/rollback. Capture startup commit safely; add status capability. Persist exact busy-deferred request before helper spawn. Keep normal settings and scheduler code untouched.

- [ ] **Step 4: Run Image verification and release**

Run: conda_env/bin/python -m pytest -q

Run: conda_env/bin/python -m compileall -q app && git diff --check

Expected: PASS. Set the independently chosen Image release SemVer only now,
then stage only the reviewed paths below, inspect the staged boundary, and make
one final versioned Image release commit before pushing:

~~~bash
git add app/backend/auto_update.py app/backend/main.py app/tests/test_auto_update.py app/tests/test_api.py VERSION CHANGELOG.md app/frontend/index.html
git status --short
git diff --cached --name-status
git diff --cached --check
git commit -m "release: exact managed updater"
git push origin main
~~~

If Image's release-note file differs from `app/frontend/index.html`, substitute
that exact existing file rather than staging a directory. Record the resulting
version and commit in the handoff.

### Task 2: Implement and release Voice Studio capability

**Files:**
- Modify: Voice app/backend/auto_update.py
- Modify: Voice app/backend/main.py
- Modify: Voice app/tests/test_auto_update.py
- Modify: Voice app/tests/test_api.py
- Modify: Voice VERSION, CHANGELOG.md, frontend release notes.

**Interfaces:**
- Consumes: Voice AutoUpdater._git_preflight, status persistence, service restart, rollback.
- Produces: the identical Image contract.

- [ ] **Step 1: Copy the same failing contract tests**

Use the Image tests with Voice imports. Cover dirty/wrong-origin/detached/unknown-SHA/not-on-main/local-not-ancestor/version-mismatch/rewrite refusal; exact merge; dependency/start/health rollback; same-operation adoption/different-target conflict; busy tuple persistence; and normal Off/Notify/Auto unchanged.

- [ ] **Step 2: Run and verify failure**

Run: conda_env/bin/python -m pytest -q app/tests/test_auto_update.py app/tests/test_api.py -k 'managed_target or app_commit'

Expected: FAIL because Voice accepts only after_current.

- [ ] **Step 3: Implement identical shared contract**

Modify only Voice's existing updater/main surfaces. Do not create another scheduler, API family, dependency, or launcher. Report managed_exact_commit true only after the full preflight/attestation contract passes.

- [ ] **Step 4: Run Voice verification and release**

Run: conda_env/bin/python -m pytest -q

Run: conda_env/bin/python -m compileall -q app && git diff --check

Expected: PASS. Set the independently chosen Voice release SemVer only now,
then stage only the reviewed paths below, inspect the staged boundary, and make
one final versioned Voice release commit before pushing:

~~~bash
git add app/backend/auto_update.py app/backend/main.py app/tests/test_auto_update.py app/tests/test_api.py VERSION CHANGELOG.md app/frontend/index.html
git status --short
git diff --cached --name-status
git diff --cached --check
git commit -m "release: exact managed updater"
git push origin main
~~~

If Voice's release-note file differs from `app/frontend/index.html`, substitute
that exact existing file rather than staging a directory. Record the resulting
version and commit in the handoff.

### Task 3: Return compatibility evidence before Hub activation

**Files:**
- Create: /Users/thengmacmini/Developer/_handoffs/2026-08-15_to-gpt-studiofleet_from-image-voice_exact-managed-update-contract-ready.md

- [ ] **Step 1: Produce one sanitized report**

Include both versions/commits, endpoint/body/status additions, rejection rules, health attestation fields, focused/full test evidence, no-launcher/no-setting-change confirmation, and oldest version lacking capability.

- [ ] **Step 2: Apply the Hub activation gate**

Studio Hub activates only a manifest whose Image/Voice target versions appear in the report. Older nodes become retryable_failure with exact component updater unavailable; never fall back to update.js.

## Self-review

The plan implements one shared contract and two independent releases. It does not assume sibling support, change normal updater semantics, or add a moving-main fallback.
