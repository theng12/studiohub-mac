# Remote Studio Update Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Studio Hub release that safely migrates legacy Voice/Image `ENVIRONMENT` update blockers locally or through authenticated controller-to-Agent fan-out.

**Architecture:** Extend the proven SSD migration engine with a Hub-only whole-file preservation mode and a one-target JSON CLI, then invoke that fixed tool from a durable fleet operation patterned after generation installation. Each Agent performs its own filesystem work, while the controller stores/polls per-Studio results and the Updates UI exposes one explicit repair action.

**Tech Stack:** Python 3 standard library, FastAPI/Pydantic, asyncio/httpx, Pinokio `pterm`, vanilla JavaScript, pytest/unittest, Git.

**Spec:** `docs/superpowers/specs/2026-08-24-remote-studio-update-repair-design.md`

## Global Constraints

- Preserve complete machine-local `ENVIRONMENT` bytes and file mode, including arbitrary legitimate settings such as `CPLUS_INCLUDE_PATH`.
- Refuse every dirty path other than root tracked `ENVIRONMENT`; refuse wrong origin, branch, upstream, symlink, divergence, and concurrent replacement.
- Run mutations only on a fixed registered local Image/Voice checkout below the resolved `PINOKIO_HOME/api`.
- Reuse each Studio's existing `update.js`, dependency convergence, restart, health, exact-commit, and rollback contracts.
- One Studio runs at a time per Mac; independent Macs may run in parallel.
- Models, caches, enrollment, fleet tokens, voices, jobs, and GenStudio code remain untouched.
- The Hub cannot repair its own blocked checkout; SSD Stage 5 remains that fallback.
- The published release performs no live fleet update or repair by itself.
- Release as Studio Hub `2.12.0` with synchronized VERSION, changelog, What's New, README, and capability documentation.

---

### Task 1: Whole-file migration mode and machine-readable command

**Files:**
- Modify: `ssd_bootstrap/kit/runtime_state_migration.py`
- Modify: `ssd_bootstrap/kit/tests/test_runtime_state_migration.py`

**Interfaces:**
- Produces: `MigrationEngine(..., preserve_machine_environment: bool = False)`.
- Produces: CLI `--app {imagestudio-mac,voicestudio-mac}`, `--preserve-machine-environment`, and `--json`.
- Preserves: strict SSD Stage 5 behavior when the new flag is absent.

- [ ] **Step 1: Write failing engine tests**

Add tests that create a legacy Voice checkout whose only dirty path is a regular
tracked `ENVIRONMENT` containing binary-safe arbitrary bytes and
`CPLUS_INCLUDE_PATH`, mode `0640`, then assert strict mode refuses it while
`preserve_machine_environment=True` migrates it and preserves exact bytes/mode.
Add separate refusal tests for an additional dirty path and a symlink.

- [ ] **Step 2: Run the focused failures**

Run:
`conda_env/bin/python -m pytest ssd_bootstrap/kit/tests/test_runtime_state_migration.py -q`

Expected: new whole-file preservation tests fail because the constructor flag
and acceptance path do not exist.

- [ ] **Step 3: Implement the smallest policy switch**

Add `preserve_machine_environment` to `MigrationEngine.__init__`. In
`_plan_studio` and `_validated_environment_snapshot`, accept only status
`((" M", "ENVIRONMENT"),)` when the flag is true; still require the existing
no-follow regular-file snapshot and all existing concurrency guards. Do not
parse or rewrite environment lines. Generalize recovery copy from “approved
original” to “original.”

- [ ] **Step 4: Add the bounded JSON CLI**

Filter `APP_SPECS` from fixed argparse choices only; never accept a path, URL,
or command. Emit one JSON object with `ok` and repository results, converting
paths to strings. Reject `--preserve-machine-environment` unless `--app` is
present so the physical all-app Stage 5 retains strict behavior.

- [ ] **Step 5: Verify engine and command compatibility**

Run:

```bash
conda_env/bin/python -m pytest \
  ssd_bootstrap/kit/tests/test_runtime_state_migration.py \
  ssd_bootstrap/kit/tests/test_commands.py -q
```

Expected: all pass, including existing exact-three-line SSD tests.

---

### Task 2: Trusted local Hub execution boundary

**Files:**
- Modify: `app/backend/control.py`
- Modify: `app/tests/test_control.py`

**Interfaces:**
- Consumes: Task 1 CLI.
- Produces: `run_studio_update_repair_sync(studio: dict, timeout: float = 45 * 60) -> dict`.

- [ ] **Step 1: Write failing control tests**

Assert the helper accepts only local `voice`/`image` registry entries, resolves
canonical and `.git` app folders through `resolve_app_dir`, runs only the fixed
repository-owned migration tool with fixed CLI arguments, parses JSON success,
and reports timeout/nonzero/malformed results without exposing raw environment
contents.

- [ ] **Step 2: Run focused control tests**

Run:
`conda_env/bin/python -m pytest app/tests/test_control.py -q`

Expected: failure because `run_studio_update_repair_sync` does not exist.

- [ ] **Step 3: Implement the fixed subprocess wrapper**

Resolve the tool below `LAUNCHER_ROOT/ssd_bootstrap/kit`, reject symlinks and
path escape, select `/usr/bin/python3` (the same standard-library runtime used
by SSD Stage 5), and invoke:

```text
runtime_state_migration.py --app <fixed-name> --preserve-machine-environment --json
```

Use captured output, no stdin, a bounded timeout, and a minimal error tail.

- [ ] **Step 4: Run focused control tests**

Run:
`conda_env/bin/python -m pytest app/tests/test_control.py -q`

Expected: all pass.

---

### Task 3: Durable local and remote fleet repair jobs

**Files:**
- Modify: `app/backend/fleet_ops.py`
- Modify: `app/backend/peers.py`
- Modify: `app/backend/main.py`
- Modify: `app/tests/conftest.py`
- Modify: `app/tests/test_fleet_ops.py`
- Modify: `app/tests/test_peers.py`
- Modify: `app/tests/test_startup_services.py`

**Interfaces:**
- Consumes: `run_studio_update_repair_sync` from Task 2.
- Produces: `start_studio_update_repairs(monitor, studio_ids=None, *, local_only=False) -> dict`.
- Produces: `studio_update_repair_snapshot(job_id=None)`.
- Produces: peer routes `POST/GET /api/hub/maintenance/studio-update-repairs`.
- Produces: `/api/version` field `studio_update_repair_schema: 1`.

- [ ] **Step 1: Write failing durable-job tests**

Cover fixed Voice/Image targeting, unknown IDs, persistence/reload, interrupted
job visibility, work drain, maintenance guard release, serial work per Mac,
parallel independent Macs, local success/refusal, remote 404 capability error,
authenticated peer request, polling, pending/retryable remote failure, and Hub
update blocker integration.

- [ ] **Step 2: Run focused backend failures**

Run:

```bash
conda_env/bin/python -m pytest \
  app/tests/test_fleet_ops.py \
  app/tests/test_peers.py \
  app/tests/test_startup_services.py -q
```

Expected: new repair job/routes are absent.

- [ ] **Step 3: Add durable state and local grouping**

Add `_studio_update_repairs` to the existing fleet state JSON and reset fixture.
Create a job with per-item `queued`, `draining`, `repairing`, `complete`, or
`failed` state. Group by machine; process each group sequentially and groups via
`asyncio.gather`. Reuse `finish_fleet_job`, `studio_has_active_work`, and
`broker.set_maintenance`.

- [ ] **Step 4: Add authenticated peer forwarding and polling**

Add a fixed peer helper posting `studio_ids: [modality], local_only: true` with
the fleet token. Poll the returned job with the same token. Treat 404 as “update
the Agent Hub first,” authentication rejection distinctly, and transport loss
as a visible retryable failure rather than success.

- [ ] **Step 5: Add API request model, routes, and capability**

Add a bounded request model matching generation installs, list/start/status
routes, and the exact integer schema field to `/api/version`. Rely on existing
Hub authentication middleware; do not add a second credential system.

- [ ] **Step 6: Run focused backend tests**

Run the Task 3 command again. Expected: all pass.

---

### Task 4: Updates workspace and operator documentation

**Files:**
- Modify: `app/frontend/index.html`
- Modify: `app/tests/test_startup_services.py`
- Modify: `README.md`
- Modify: `CAPABILITY_CONTRACT.md`

**Interfaces:**
- Consumes: Task 3 list/start/status routes.
- Produces: Updates action `Repair blocked Studio updates`, progress card, and retry guidance.

- [ ] **Step 1: Write failing UI contract assertions**

Assert stable element IDs, action function, endpoint strings, preservation
copy, remote-Hub prerequisite, and explicit no-model/no-enrollment language.

- [ ] **Step 2: Run UI contract failure**

Run:
`conda_env/bin/python -m pytest app/tests/test_startup_services.py -q`

Expected: new control is absent.

- [ ] **Step 3: Implement the minimal Updates card**

Add one primary button and one progress region beside generation maintenance.
Use the existing `api`, escaping, progress-bar, and polling patterns. The
confirmation explains this is a one-time legacy repair and that active Studios
may restart; the display names refused/offline rows and says they can be retried.

- [ ] **Step 4: Document use and remote boundary**

Document that an updated reachable Agent Hub can repair Voice/Image remotely,
that Hub self-bootstrap still needs Stage 5 locally, and that ordinary updates
handle declared dependencies after migration.

- [ ] **Step 5: Verify UI/docs contracts**

Run:

```bash
conda_env/bin/python -m pytest app/tests/test_startup_services.py -q
node --check < app/frontend/index.html
```

If direct HTML input is not accepted by Node, extract the existing `<script>`
body using the repository's established frontend syntax test command instead.

---

### Task 5: Release metadata, regression verification, commit, and push

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html`
- Modify: `app/tests/test_release_metadata.py`
- Track: `docs/superpowers/plans/2026-08-24-remote-studio-update-repair.md`

**Interfaces:**
- Produces: published Studio Hub `2.12.0` on `origin/main`.

- [ ] **Step 1: Add release truth tests and metadata**

Set VERSION to `2.12.0`; add a dated changelog section and first What's New
entry describing local/remote repair, whole-file preservation, dependency
convergence, Hub-first requirement, and no automatic fleet action.

- [ ] **Step 2: Run focused release and migration suites**

```bash
conda_env/bin/python -m pytest \
  app/tests/test_release_metadata.py \
  app/tests/test_control.py \
  app/tests/test_fleet_ops.py \
  app/tests/test_peers.py \
  app/tests/test_startup_services.py \
  ssd_bootstrap/kit/tests/test_runtime_state_migration.py \
  ssd_bootstrap/kit/tests/test_commands.py -q
```

- [ ] **Step 3: Run complete repository verification**

Discover and run the documented full pytest command, Python compile checks,
launcher JavaScript syntax checks, frontend script syntax check, dependency
check, SSD sync tests in check/dry-run mode only, release metadata tests, and
`git diff --check`. Do not run a live migration or fleet endpoint.

- [ ] **Step 4: Inspect scope and commit**

Require a clean staged diff containing only the files in this plan, no secrets,
models, caches, live state, or unrelated launcher changes. Commit the complete
implementation release after the existing approved design commit.

- [ ] **Step 5: Push and verify publication**

Push `main` to `origin`, then verify local `HEAD == origin/main`, the remote
VERSION is `2.12.0`, and `git status --short --branch` is clean. Do not trigger
any Hub or Studio update automatically.
