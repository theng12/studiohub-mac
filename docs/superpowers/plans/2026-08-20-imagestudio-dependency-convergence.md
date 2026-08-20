# Image Studio Dependency Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release Image Studio 1.30.3 as a dependency-neutral bridge whose install, update, automatic updater, and generation installer all use one repository-owned convergence command.

**Architecture:** A standard-library CLI owns Image Studio's fixed base and generation dependency commands. The four current entry points call it; the automatic updater retains its existing lock, rollback, restart, and health attestation.

**Tech Stack:** Python 3.12, subprocess/pathlib, Pinokio JavaScript, pytest, Git.

**Spec:** `/Users/thengmacmini/pinokio/api/studiohub-mac/docs/superpowers/specs/2026-08-20-unified-dependency-convergence-design.md`

## Global Constraints

- Work only in `/Users/thengmacmini/pinokio/api/imagestudio-mac`; preserve unrelated changes.
- `PINOKIO_HOME` is `/Users/thengmacmini/pinokio`; launcher paths stay relative and keep the existing `conda_env` contract.
- Cross-check launcher edits against `/Users/thengmacmini/pinokio/prototype/system/examples/mochi/install.js` and `update.js`.
- Add no package, requirement, model, catalog entry, startup mode, service change, URL-capture change, or reset change.
- `base` installs `app/requirements.txt`; `generation` installs `app/requirements-generation.lock.txt` and verifies `mflux`.
- `all-installed` runs generation only when `mflux` already exists.
- Status capabilities become `{"managed_exact_commit": true, "dependency_convergence": 1}`.
- Release `1.30.3` dated `2026-08-20`; stop after a reviewed local commit. No push or live app/fleet action.

---

### Task 1: Add the convergence command and route all callers through it

**Files:**
- Create: `app/backend/dependency_convergence.py`
- Create: `app/tests/test_dependency_convergence.py`
- Modify: `app/backend/auto_update.py`
- Modify: `app/tests/test_auto_update.py`
- Modify: `install.js`
- Modify: `update.js`
- Modify: `install_generation.js`

**Interfaces:**
- Consumes: active `sys.executable`, repository root from `Path(__file__).resolve()`, fixed checked-in requirements.
- Produces: `converge(mode: str, *, runner=subprocess.run) -> None`, `generation_installed() -> bool`, and CLI `python -m backend.dependency_convergence {base|generation|all-installed}`.

- [ ] **Step 1: Write failing selection and failure tests**

```python
def test_base_uses_only_base_requirements(fake_runner):
    convergence.converge("base", runner=fake_runner)
    assert any("requirements.txt" in str(row) for row in fake_runner.argv)
    assert all("requirements-generation" not in str(row) for row in fake_runner.argv)

def test_all_installed_skips_missing_generation(monkeypatch, fake_runner):
    monkeypatch.setattr(convergence, "generation_installed", lambda: False)
    convergence.converge("all-installed", runner=fake_runner)
    assert all("requirements-generation" not in str(row) for row in fake_runner.argv)

def test_generation_installs_lock_and_verifies_mflux(fake_runner):
    convergence.converge("generation", runner=fake_runner)
    assert any("requirements-generation.lock.txt" in str(row) for row in fake_runner.argv)
    assert [sys.executable, "-c", "import mflux; print('GEN_VERIFY_OK')"] in fake_runner.argv
```

- [ ] **Step 2: Run RED**

Run: `./conda_env/bin/python -m pytest -q app/tests/test_dependency_convergence.py`

Expected: collection fails because `backend.dependency_convergence` is absent.

- [ ] **Step 3: Implement the fixed command**

```python
APP = Path(__file__).resolve().parents[1]
MODES = {"base", "generation", "all-installed"}

def generation_installed() -> bool:
    return importlib.util.find_spec("mflux") is not None

def _pip(requirements: Path) -> list[str]:
    return [uv_executable(), "pip", "install", "--python", sys.executable,
            "-r", str(requirements)]

def converge(mode: str, *, runner=subprocess.run) -> None:
    if mode not in MODES:
        raise ValueError("mode must be base, generation, or all-installed")
    commands = []
    if mode in {"base", "all-installed"}:
        commands.append(_pip(APP / "requirements.txt"))
    if mode == "generation" or (mode == "all-installed" and generation_installed()):
        commands.extend([
            _pip(APP / "requirements-generation.lock.txt"),
            [sys.executable, "-c", "import mflux; print('GEN_VERIFY_OK')"],
        ])
    for argv in commands:
        runner(argv, cwd=APP, check=True, timeout=1800)
```

`uv_executable()` must resolve only the configured Pinokio home's fixed `bin/miniforge/bin/uv` and fail closed when absent. CLI input selects only the three modes and cannot supply commands, packages, channels, or paths.

- [ ] **Step 4: Replace only dependency commands in launchers**

Retain every current stop/restart/service/notify step. In each existing `shell.run`, keep `path: "app"` and `conda.path`, and use:

```javascript
message: ["python -m backend.dependency_convergence base"]
```

Use `all-installed` in `update.js` and `generation` in `install_generation.js`. Remove raw package-install and inline import-verification lists.

- [ ] **Step 5: Route the automatic updater and publish capability**

```python
def _install_dependencies(self) -> None:
    self._run(
        [str(self._python()), "-m", "backend.dependency_convergence", "all-installed"],
        cwd=self.root / "app", timeout=1800,
    )
```

Publish:

```python
"capabilities": {"managed_exact_commit": True, "dependency_convergence": 1},
```

Add an updater regression that makes this subprocess fail and asserts the update remains failed and the existing rollback path executes.

- [ ] **Step 6: Add static caller-contract tests**

Assert each launcher has exactly one module invocation with the correct mode, contains no `uv pip install`, `pip install`, or `conda install`, and retains its existing service-aware restart branches. Assert `_install_dependencies()` invokes `all-installed` and status exposes both capability keys.

- [ ] **Step 7: Run focused verification**

```bash
./conda_env/bin/python -m pytest -q app/tests/test_dependency_convergence.py app/tests/test_auto_update.py app/tests/test_release_metadata.py
node --check install.js
node --check update.js
node --check install_generation.js
python3 -m py_compile app/backend/dependency_convergence.py app/backend/auto_update.py
```

Expected: all pass; injected runners prevent package installation.

### Task 2: Release, review, and locally commit Image Studio 1.30.3

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html` (first embedded release note only)
- Review: Task 1 files

**Interfaces:**
- Consumes: Task 1's bridge-capable updater.
- Produces: clean local release `1.30.3` and its exact commit SHA.

- [ ] **Step 1: Add truthful release metadata**

State that all update entry points converge the same dependencies, optional generation remains installed-only, status advertises bridge capability, and no dependency, model, service mode, or live machine changed. Set `VERSION` to `1.30.3`.

- [ ] **Step 2: Validate metadata and the full repository**

```bash
python3 release_metadata_check.py
./conda_env/bin/python -m pytest -q app/tests
python3 -m compileall -q app/backend
for file in *.js; do node --check "$file"; done
./conda_env/bin/python -m pip check
git diff --check
```

Expected: zero failures and clean dependency/diff checks.

- [ ] **Step 3: Request read-only code review**

Review fixed-command safety, marker truth, four callers, rollback preservation, capability truth, launcher restart ownership, metadata, and absence of model/fleet effects. Resolve every Critical/Important finding and rerun the frozen-diff gates.

- [ ] **Step 4: Create the local release commit**

```bash
git add app/backend/dependency_convergence.py app/backend/auto_update.py app/tests/test_dependency_convergence.py app/tests/test_auto_update.py install.js update.js install_generation.js VERSION CHANGELOG.md app/frontend/index.html
git diff --cached --check
git commit -m "fix: unify Image dependency convergence"
git status --short
git rev-parse HEAD
```

Expected: one clean local commit and no push.
