# Live Fleet Activity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Studio Hub Stats explain what every fleet Mac is doing now, how long it has been in that state, what it last completed, and how its comparable performance differs from peers, including work started directly in Image or Voice Studio.

**Architecture:** Image and Voice expose the same authenticated, sanitized activity snapshot from their existing generation managers. Studio Hub fetches that snapshot during its established five-second Studio poll, records only state transitions in the existing SQLite ledger, merges those observations with authoritative broker evidence, and renders a live machine board above the existing historical Stats page.

**Tech Stack:** Python 3.12, FastAPI, SQLite, pytest, existing single-file HTML/CSS/JavaScript dashboard. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-30-live-fleet-activity-design.md`

## Global Constraints

- Preserve all existing Stats controls and historical analytics beneath the new live fleet view.
- Track both Studio-direct and Hub-dispatched Image and Voice jobs without double-counting Hub jobs.
- Keep `/api/health` public and free of job identities; `/api/fleet/activity` uses existing fleet authentication.
- Never expose prompts, paths, generated assets, credentials, reference media, or complete request parameters.
- Preserve mixed-version compatibility: a missing activity endpoint is a visible limitation, not a health failure.
- Use no new service, port, credential, external store, or dependency.
- Retain activity transitions for 30 days and compare performance only within the same Studio/model with at least three timed successes per machine.
- Do not change GenStudio APIs, routing, scheduling, enrollment, updates, or memory controls.

---

### Task 1: Image Studio activity reporter

**Files:**
- Modify: `/Users/thengmacmini/pinokio/api/imagestudio-mac/.worktrees/live-fleet-stats/app/backend/generation.py`
- Modify: `/Users/thengmacmini/pinokio/api/imagestudio-mac/.worktrees/live-fleet-stats/app/backend/main.py`
- Create: `/Users/thengmacmini/pinokio/api/imagestudio-mac/.worktrees/live-fleet-stats/app/tests/test_fleet_activity.py`

**Interfaces:**
- Produces: `GenerationManager.activity_snapshot(observed_at: float | None = None) -> dict`
- Produces: authenticated `GET /api/fleet/activity` with schema `kh-studio.activity.v1`, `studio=image`, `observed_at`, `active`, and `latest`.

- [ ] **Step 1: Write failing reporter tests**

Create real `GenerationJob` rows and assert that running work becomes `active`, the newest terminal job becomes `latest`, models come only from `params.repo`, progress is clamped to `0..1`, runtimes are derived from timestamps, and prompt/path/reference fields never appear anywhere in the serialized response.

```python
def test_activity_snapshot_exposes_only_active_and_latest_safe_evidence():
    manager = GenerationManager.__new__(GenerationManager)
    manager._jobs = {
        "run": GenerationJob("run", "txt2img", {"repo": "org/model", "prompt": "secret"}, state="running", progress=0.4, started_at=20.0),
        "done": GenerationJob("done", "txt2img", {"repo": "org/model", "prompt": "secret"}, state="done", progress=1.0, started_at=10.0, finished_at=15.0, output_path="/private/output.png"),
    }
    result = manager.activity_snapshot(observed_at=25.0)
    assert result["active"]["id"] == "run"
    assert result["latest"]["runtime_s"] == 5.0
    assert "secret" not in repr(result)
    assert "/private/output.png" not in repr(result)
```

- [ ] **Step 2: Run the focused test and verify the expected missing-method failure**

Run from the Image `app` directory:

```bash
../../conda_env/bin/python -m pytest -q tests/test_fleet_activity.py
```

- [ ] **Step 3: Implement the minimal sanitized projection and route**

Add `created_at` to new/persisted jobs with backward-compatible fallback, implement one projection helper on `GenerationManager`, and return it from the authenticated route. Reuse the existing fleet-auth middleware and manager lock conventions. Do not reuse full `serialize()` output because it contains private request fields and paths.

- [ ] **Step 4: Run focused and full Image suites**

```bash
../../conda_env/bin/python -m pytest -q tests/test_fleet_activity.py tests/test_fleet_auth.py tests/test_api.py
../../conda_env/bin/python -m pytest -q
```

- [ ] **Step 5: Commit the Image reporter**

```bash
git add app/backend/generation.py app/backend/main.py app/tests/test_fleet_activity.py
git commit -m "feat: report direct image activity"
```

### Task 2: Voice Studio activity reporter

**Files:**
- Modify: `/Users/thengmacmini/pinokio/api/studiohub-mac/.worktrees/voice-live-fleet-stats/app/backend/generation.py`
- Modify: `/Users/thengmacmini/pinokio/api/studiohub-mac/.worktrees/voice-live-fleet-stats/app/backend/main.py`
- Create: `/Users/thengmacmini/pinokio/api/studiohub-mac/.worktrees/voice-live-fleet-stats/app/tests/test_fleet_activity.py`

**Interfaces:**
- Produces: the same `activity_snapshot()` and `/api/fleet/activity` contract as Task 1, with `studio=voice`.
- Voice marks `source=job` when `client_request_id` starts with `studiohub:`; otherwise `source=direct`.

- [ ] **Step 1: Write failing Voice reporter tests**

Exercise queued, running, done, error, and malformed progress. Assert model, timing, chunk progress, bounded safe error/error-code evidence, origin classification, and complete absence of prompt, reference audio, transcript, output path, and internal parameters.

```python
def test_activity_snapshot_classifies_hub_and_direct_jobs_without_private_data():
    manager = GenerationManager.__new__(GenerationManager)
    manager._lock = threading.Lock()
    manager._jobs = {
        "hub": GenerationJob("hub", "txt2speech", {"repo": "org/voice", "client_request_id": "studiohub:b:0", "text": "secret"}, state="running", progress=0.5, started_at=20.0),
        "direct": GenerationJob("direct", "txt2speech", {"repo": "org/voice", "text": "secret"}, state="done", progress=1.0, started_at=10.0, finished_at=14.0),
    }
    result = manager.activity_snapshot(observed_at=25.0)
    assert result["active"]["source"] == "job"
    assert result["latest"]["source"] == "direct"
    assert "secret" not in repr(result)
```

- [ ] **Step 2: Run the focused test and verify the expected missing-method failure**

```bash
/Users/thengmacmini/pinokio/api/voicestudio-mac.git/conda_env/bin/python -m pytest -q app/tests/test_fleet_activity.py
```

- [ ] **Step 3: Implement the contract by mirroring the reviewed Image shape**

Add no shared package or abstraction between repositories. Keep the two small helpers structurally identical where their job models agree, and preserve Voice-only chunk fields and error codes.

- [ ] **Step 4: Run focused and full Voice suites**

```bash
/Users/thengmacmini/pinokio/api/voicestudio-mac.git/conda_env/bin/python -m pytest -q app/tests/test_fleet_activity.py app/tests/test_fleet_auth.py app/tests/test_api.py
/Users/thengmacmini/pinokio/api/voicestudio-mac.git/conda_env/bin/python -m pytest -q
```

- [ ] **Step 5: Commit the Voice reporter**

```bash
git add app/backend/generation.py app/backend/main.py app/tests/test_fleet_activity.py
git commit -m "feat: report direct voice activity"
```

### Task 3: Studio Hub observation ledger and state model

**Files:**
- Create: `app/backend/activity.py`
- Modify: `app/backend/ledger.py`
- Modify: `app/backend/monitor.py`
- Modify: `app/backend/main.py`
- Modify: `app/tests/conftest.py`
- Create: `app/tests/test_activity.py`
- Modify: `app/tests/test_api.py`

**Interfaces:**
- Consumes: Task 1/2 `kh-studio.activity.v1` snapshots.
- Produces: `activity.observe_poll(registry, statuses, batches, now=None)`.
- Produces: `activity.fleet_snapshot(registry, statuses, batches, since_s, now=None) -> dict` returned as `fleet_activity` by `/api/hub/stats`.
- Produces ledger operations for idempotent job transitions, machine-state transitions, 30-day pruning, recent timeline, and utilization interval integration.

- [ ] **Step 1: Write failing contract, transition, state, and aggregation tests**

Use literal payloads. Prove invalid schemas/states/progress are ignored; repeated polls create one transition; Hub-owned `studio_job_id` overrides reporter `source`; 404/missing snapshots remain compatible; old rows prune; state precedence is deterministic; success after an error clears attention; just-finished is under 15 minutes; long-idle begins at two hours; utilization integrates working time only over reachable intervals; and medians require three comparable timed successes per machine.

```python
def test_machine_state_distinguishes_recent_completion_from_long_idle(reset):
    ledger.record_activity_event(machine="mac-a", studio="image@mac-a", job_id="one", state="done", model="org/model", source="direct", started_at=100.0, finished_at=110.0, runtime_s=10.0, observed_at=110.0)
    recent = activity.fleet_snapshot(_registry("mac-a"), _up_status(), {}, since_s=0.0, now=110.0 + 14 * 60)
    old = activity.fleet_snapshot(_registry("mac-a"), _up_status(), {}, since_s=0.0, now=110.0 + 2 * 3600)
    assert recent["machines"][0]["state"] == "just_finished"
    assert old["machines"][0]["state"] == "long_idle"
```

- [ ] **Step 2: Run focused tests and verify missing modules/tables fail for the intended reason**

```bash
/Users/thengmacmini/pinokio/api/studiohub-mac/conda_env/bin/python -m pytest -q app/tests/test_activity.py app/tests/test_api.py -k 'activity or stats'
```

- [ ] **Step 3: Implement validation and idempotent transition persistence**

Create only the two tables needed: job activity transitions and machine operational-state transitions. Store bounded scalar evidence and never raw payload JSON. Fetch `/api/fleet/activity` only after a successful health response through `studio_request()`. A 404 records reporter support as unavailable without degrading Studio health. Other failures preserve the last good evidence and expose a temporary limitation.

- [ ] **Step 4: Implement current state, timeline, utilization, and comparable performance**

Merge live reporter data with broker items by `studio_job_id`. Use the approved state precedence and thresholds. Integrate working intervals over reachable intervals from machine-state transitions. Derive performance medians from successful timed events grouped by `(studio family, model)` and emit relative comparison only when two machines each have at least three samples.

- [ ] **Step 5: Add `fleet_activity` to `/api/hub/stats` without changing existing keys**

Keep existing filters authoritative for the historical blocks. The machine activity board uses the selected window for totals/performance and always uses current live state for the state badge.

- [ ] **Step 6: Run focused and full Hub suites**

```bash
/Users/thengmacmini/pinokio/api/studiohub-mac/conda_env/bin/python -m pytest -q app/tests/test_activity.py app/tests/test_ledger.py app/tests/test_api.py app/tests/test_monitor.py
/Users/thengmacmini/pinokio/api/studiohub-mac/conda_env/bin/python -m pytest -q
```

- [ ] **Step 7: Commit the Hub backend**

```bash
git add app/backend/activity.py app/backend/ledger.py app/backend/monitor.py app/backend/main.py app/tests/conftest.py app/tests/test_activity.py app/tests/test_api.py
git commit -m "feat: aggregate live fleet activity"
```

### Task 4: Operations-first Stats interface

**Files:**
- Modify: `app/frontend/index.html`
- Create or modify: `app/tests/test_frontend_stats.py`
- Verify: `app/tests/test_frontend_syntax.py`

**Interfaces:**
- Consumes: `/api/hub/stats.fleet_activity` from Task 3.
- Preserves: all existing Stats filters, tiles, throughput chart, machine table, matrix, model table, and explanatory limitations.

- [ ] **Step 1: Write failing frontend behavior tests**

Extract the activity rendering helper and run it in Node with literal state fixtures. Assert every approved label, attention-first ordering, human duration text, progress semantics, direct-activity-unavailable note, accessible disclosure buttons, and historical section presence.

- [ ] **Step 2: Run the focused tests and verify the expected missing-renderer failure**

```bash
/Users/thengmacmini/pinokio/api/studiohub-mac/conda_env/bin/python -m pytest -q app/tests/test_frontend_stats.py app/tests/test_frontend_syntax.py
```

- [ ] **Step 3: Build the live operational section in the incumbent design system**

Add Fleet pulse cards and a responsive Machine activity table above a clearly labelled Historical performance section. Use semantic state colors already present, native buttons/disclosures, text plus color for every state, skeleton/loading and partial-evidence copy, no new chart library, and no inline modal.

- [ ] **Step 4: Run focused tests and the Impeccable detector**

```bash
/Users/thengmacmini/pinokio/api/studiohub-mac/conda_env/bin/python -m pytest -q app/tests/test_frontend_stats.py app/tests/test_frontend_syntax.py app/tests/test_frontend_typography.py
node /Users/thengmacmini/.codex/skills/impeccable/scripts/detect.mjs --json app/frontend/index.html
```

- [ ] **Step 5: Inspect one desktop and one narrow-width render, fix one bounded batch, and confirm once**

Verify hierarchy, table overflow, disclosure keyboard operation, focus visibility, meaningful empty/error states, and that the historical analysis remains reachable below the fold.

- [ ] **Step 6: Run the full Hub suite and commit the UI**

```bash
/Users/thengmacmini/pinokio/api/studiohub-mac/conda_env/bin/python -m pytest -q
git add app/frontend/index.html app/tests/test_frontend_stats.py
git commit -m "feat: make fleet activity clear in Stats"
```

### Task 5: Release evidence and cross-repository verification

**Files:**
- Modify: `VERSION`, `CHANGELOG.md`, and version metadata files required by each repository's existing release tests.
- Modify: `README.md` only where operator behavior needs durable explanation.

**Interfaces:**
- Produces compatible feature branches and pull requests for Studio Hub, Image Studio, and Voice Studio.

- [ ] **Step 1: Run all three full suites from clean feature worktrees**

Expected baseline floors before the new tests: Hub 1,298 passed with one skip, Image 172 passed, Voice 593 passed with two skips.

- [ ] **Step 2: Bump minor versions according to each repository's documented workflow**

This is a new endpoint/dashboard capability, not a patch. Add release notes that name the optional reporter contract, mixed-version behavior, state thresholds, privacy exclusions, and the fact that routing is unchanged.

- [ ] **Step 3: Run version, dependency/import, syntax, and full-suite checks again**

Run each repository's release metadata tests and normal full suite. Run `git diff --check` and confirm every worktree is clean after its release commit.

- [ ] **Step 4: Request independent code review and address only verified findings**

Review cross-repository contract equality, privacy, state precedence, interval math, query bounds, accessibility, and mixed-version compatibility.

- [ ] **Step 5: Push each `codex/live-fleet-stats` branch and create one pull request per repository**

Do not merge, tag, publish, deploy, or update any fleet machine.

### 2026-09-01 follow-up: subtitle transcription activity

- Extend Voice Studio's existing private reporter rather than introducing a
  second endpoint or service. Track direct and Hub-dispatched `/api/transcribe`
  work with a bounded in-memory job record and run inference off the API loop.
- Add the optional `operation` scalar to the established reporter contract.
  Preserve legacy defaults (`image` for Image Studio, `speech` for Voice
  Studio) and validate `transcription` only for Voice Studio.
- Correlate Hub transcription batch items with Voice activity using one stable
  `studio_task_id`/`activity_id`; retain operation in the existing activity
  ledger and performance key.
- Name Subtitle transcription explicitly in live rows, timelines, and the
  authenticated on-demand detail drawer. Keep ordinary polling content-free;
  do not retain uploaded audio or build a second history/archive.
- Prove privacy, live-to-terminal behavior, ownership, legacy compatibility,
  UI naming, ledger migration, and unchanged generation behavior in focused
  tests, then run both repositories' complete release suites.

### 2026-09-02 follow-up: historical Voice and Subtitle visibility

- Build one historical SQL source from existing generated assets plus only
  terminal `done` transcription activity. Do not add Image or Voice activity
  events, because their generated assets already supply the count.
- Apply the existing source, operation, machine, and window filters to the
  unified source, and include completed Subtitle work in totals, timeline,
  machine, matrix, and model aggregates.
- Keep Image, Voice TTS, and Subtitle UI controls and per-machine breakdowns
  visible with explicit zero counts. Use plain labels rather than internal
  operation names.
- Add focused ledger and frontend behavior tests, run the complete Hub suite,
  inspect the UI detector output, then follow the normal version/changelog and
  independent-review workflow.
