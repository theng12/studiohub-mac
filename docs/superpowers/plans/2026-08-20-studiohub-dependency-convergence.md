# Studio Hub Dependency Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release Studio Hub 2.11.5 with one Hub dependency command and controller inventory that truthfully reports which Image and Voice targets have the bridge capability.

**Architecture:** Hub install, local update, and automatic update call one repository-owned base convergence CLI. Fleet inventory passes through strict target capability evidence for later dependency-bearing releases. This dependency-neutral bridge does not add speculative exact pins or alter managed-release schemas.

**Tech Stack:** Python 3.12, FastAPI backend, asyncio/httpx, Pinokio JavaScript, pytest, Git.

**Spec:** `docs/superpowers/specs/2026-08-20-unified-dependency-convergence-design.md`

## Global Constraints

- Work only in `/Users/thengmacmini/pinokio/api/studiohub-mac`; preserve the approved spec/plans and unrelated changes.
- `PINOKIO_HOME` is `/Users/thengmacmini/pinokio`; cross-check launcher edits against `/Users/thengmacmini/pinokio/prototype/system/examples/mochi/install.js` and `update.js`.
- Hub `base` and `all-installed` both install `app/requirements.lock`; `generation` fails without changing files.
- Add no package, model, exact target pin, manifest field, GenStudio contract, service mode, or fleet mutation.
- Image/Voice evidence is exactly integer `dependency_convergence: 1`; missing/malformed values remain unknown and are never inferred from version.
- Old machines and ordinary dependency-neutral updates remain functional without capability. A later dependency-bearing release must add its own exact bridge gate.
- Release `2.11.5` dated `2026-08-20`; stop after a reviewed local commit. No push, deploy, service restart, or fleet update.

---

### Task 1: Add Hub convergence and route all local callers through it

**Files:**
- Create: `app/backend/dependency_convergence.py`
- Create: `app/tests/test_dependency_convergence.py`
- Modify: `app/backend/auto_update.py`
- Modify: `app/tests/test_auto_update.py`
- Modify: `install.js`
- Modify: `update.js`

**Interfaces:**
- Consumes: active Hub Python, configured Pinokio uv, fixed `app/requirements.lock`.
- Produces: `converge(mode: str, *, runner=subprocess.run) -> None` and CLI `python -m backend.dependency_convergence {base|all-installed}`; `generation` is rejected.

- [ ] **Step 1: Write failing command tests**

```python
@pytest.mark.parametrize("mode", ["base", "all-installed"])
def test_hub_modes_install_only_locked_base(mode, fake_runner):
    convergence.converge(mode, runner=fake_runner)
    assert fake_runner.argv == [[
        convergence.uv_executable(), "pip", "install", "--python", sys.executable,
        "-r", str(convergence.APP / "requirements.lock"),
    ]]

def test_hub_rejects_generation_without_commands(fake_runner):
    with pytest.raises(ValueError, match="Hub has no generation stack"):
        convergence.converge("generation", runner=fake_runner)
    assert fake_runner.argv == []
```

- [ ] **Step 2: Run RED**

Run: `./conda_env/bin/python -m pytest -q app/tests/test_dependency_convergence.py`

Expected: collection fails because the module is absent.

- [ ] **Step 3: Implement the minimal fixed Hub command**

```python
APP = Path(__file__).resolve().parents[1]

def converge(mode: str, *, runner=subprocess.run) -> None:
    if mode == "generation":
        raise ValueError("Hub has no generation stack")
    if mode not in {"base", "all-installed"}:
        raise ValueError("mode must be base or all-installed")
    runner(
        [uv_executable(), "pip", "install", "--python", sys.executable,
         "-r", str(APP / "requirements.lock")],
        cwd=APP, check=True, timeout=1200,
    )
```

Resolve uv only from the configured Pinokio home. CLI input cannot supply a command, package, executable, or path.

- [ ] **Step 4: Replace only launcher dependency lists**

Use `python -m backend.dependency_convergence base` in `install.js` and `all-installed` in `update.js`. Preserve Git pull, owned-listener recovery, service marker, `install_service.sh`, mutually exclusive `start.js`, and notify behavior.

- [ ] **Step 5: Replace automatic dependency logic and publish capability**

```python
def _install_dependencies(self) -> None:
    self._run(
        [str(self._python()), "-m", "backend.dependency_convergence", "all-installed"],
        cwd=self.root / "app", timeout=1200,
    )
```

Publish `{"managed_exact_commit": True, "dependency_convergence": 1}`. Add a regression proving command failure blocks succeeded state and uses existing rollback.

- [ ] **Step 6: Add caller-contract tests and run focused checks**

Assert both launchers invoke the module once with exact modes, contain no package-install list, and retain current service/start branches.

```bash
./conda_env/bin/python -m pytest -q app/tests/test_dependency_convergence.py app/tests/test_auto_update.py app/tests/test_release_metadata.py
node --check install.js
node --check update.js
python3 -m py_compile app/backend/dependency_convergence.py app/backend/auto_update.py
```

Expected: all pass without installing packages.

### Task 2: Surface strict dependency capability in fleet inventory and jobs

**Files:**
- Modify: `app/backend/fleet_auto_updates.py`
- Modify: `app/tests/test_fleet_auto_updates.py`

**Interfaces:**
- Consumes: target `/api/auto-update/status` capability object.
- Produces: inventory/job item field `dependency_convergence: int | None`.

- [ ] **Step 1: Write failing inventory and persistence tests**

```python
def test_inventory_reports_exact_dependency_capability(monkeypatch):
    row = asyncio.run(status_row_with_capability(1))
    assert row["dependency_convergence"] == 1

@pytest.mark.parametrize("value", [None, True, 0, 2, "1", {}, []])
def test_inventory_does_not_coerce_dependency_capability(value, monkeypatch):
    row = asyncio.run(status_row_with_capability(value))
    assert row["dependency_convergence"] is None
```

Add a job test that records `1` from the first successful pre-update status. Add a missing-capability case that records `None` but still completes a dependency-neutral ordinary update. Extend the interrupted-job test to prove the field survives `_persist()`, reconstruction, and `resume_pending()`.

- [ ] **Step 2: Run RED**

Run: `./conda_env/bin/python -m pytest -q app/tests/test_fleet_auto_updates.py -k dependency_convergence`

Expected: rows/items lack the new field.

- [ ] **Step 3: Add one strict normalizer and reuse it**

```python
def _dependency_convergence_capability(status: object) -> int | None:
    if not isinstance(status, dict):
        return None
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    value = capabilities.get("dependency_convergence")
    return 1 if type(value) is int and value == 1 else None
```

Use it in `_status_one()` and capture it on the durable item after the first successful updater-status read in `_update_one()`. Do not infer from version and do not block this bridge-neutral ordinary update.

- [ ] **Step 4: Run focused orchestration checks**

```bash
./conda_env/bin/python -m pytest -q app/tests/test_fleet_auto_updates.py app/tests/test_fleet_ops.py app/tests/test_auto_update.py
```

Expected: all pass; old-target behavior remains unchanged.

### Task 3: Release, cross-repository verify, review, and commit Hub 2.11.5

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html` (first embedded release note only)
- Add: `docs/superpowers/specs/2026-08-20-unified-dependency-convergence-design.md`
- Add: all three `docs/superpowers/plans/2026-08-20-*-dependency-convergence.md` files
- Review: Tasks 1–2 files

**Interfaces:**
- Consumes: clean local Image `1.30.3` and Voice `2.4.2` bridge commits.
- Produces: clean local Hub `2.11.5`, exact three-repository tuple, and owner handoff. No push/fleet action.

- [ ] **Step 1: Verify sibling outputs**

```bash
test "$(git -C ../imagestudio-mac status --short)" = ""
test "$(git -C ../voicestudio-mac.git status --short)" = ""
test "$(cat ../imagestudio-mac/VERSION)" = "1.30.3"
test "$(cat ../voicestudio-mac.git/VERSION)" = "2.4.2"
git -C ../imagestudio-mac rev-parse HEAD
git -C ../voicestudio-mac.git rev-parse HEAD
```

Expected: clean sibling releases and exact SHAs printed.

- [ ] **Step 2: Add truthful Hub metadata**

Set `VERSION` to `2.11.5`. State that Hub uses one convergence path; fleet inventory reports exact bridge capability; Image 1.30.3 and Voice 2.4.2 are dependency-neutral bridges; old machines keep working; later dependency-bearing releases need an exact gate; no live update occurred.

- [ ] **Step 3: Run focused and full Hub gates**

```bash
./conda_env/bin/python -m pytest -q app/tests/test_dependency_convergence.py app/tests/test_auto_update.py app/tests/test_fleet_auto_updates.py app/tests/test_fleet_ops.py app/tests/test_release_metadata.py
./conda_env/bin/python -m pytest -q app/tests
python3 -m compileall -q app/backend
for file in *.js; do node --check "$file"; done
./conda_env/bin/python -m pip check
git diff --check
```

Expected: zero failures and clean dependency/diff checks.

- [ ] **Step 4: Run cross-repository no-side-effect checks**

```bash
for repo in ../imagestudio-mac ../voicestudio-mac.git .; do git -C "$repo" status --short; done
rg -n "dependency_convergence" ../imagestudio-mac/app/backend ../imagestudio-mac/{install,update,install_generation}.js ../voicestudio-mac.git/app/backend ../voicestudio-mac.git/{install,update,install_generation}.js app/backend install.js update.js
rg -n "hf\.download|snapshot_download|model.*download" app/backend/dependency_convergence.py ../imagestudio-mac/app/backend/dependency_convergence.py ../voicestudio-mac.git/app/backend/dependency_convergence.py
```

Expected: all three repos expose the bridge; new modules have no model-download path; only Hub has an uncommitted release diff.

- [ ] **Step 5: Request final cross-repository review**

Review fixed authority, real controller Voice ffmpeg coverage, optional-generation preservation, rollback, capability type truth, old-machine compatibility, no speculative coercion/pins, metadata, and no fleet/model/GenStudio mutation. Resolve every Critical/Important finding and rerun frozen-diff gates.

- [ ] **Step 6: Commit Hub and planning artifacts**

```bash
git add app/backend/dependency_convergence.py app/backend/auto_update.py app/backend/fleet_auto_updates.py app/tests/test_dependency_convergence.py app/tests/test_auto_update.py app/tests/test_fleet_auto_updates.py install.js update.js VERSION CHANGELOG.md app/frontend/index.html docs/superpowers/specs/2026-08-20-unified-dependency-convergence-design.md docs/superpowers/plans/2026-08-20-imagestudio-dependency-convergence.md docs/superpowers/plans/2026-08-20-voicestudio-dependency-convergence.md docs/superpowers/plans/2026-08-20-studiohub-dependency-convergence.md
git diff --cached --check
git commit -m "fix: unify fleet dependency convergence"
git status --short
git rev-parse HEAD
```

Expected: one clean Hub release commit and no push.

- [ ] **Step 7: Write the owner handoff**

Record exact versions/SHAs, test counts, review verdicts, and deployment boundary: old machines remain supported; nothing was pushed or rolled out; pushing/controller rollout requires separate owner instruction. State that the first later release introducing a system dependency must add exact bridge pins and tests.
