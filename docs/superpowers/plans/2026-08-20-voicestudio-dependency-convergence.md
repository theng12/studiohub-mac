# Voice Studio Dependency Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release Voice Studio 2.4.2 as a dependency-neutral bridge whose local and controller-driven updates install and verify the same base, ffmpeg, and installed generation dependencies.

**Architecture:** A repository-owned standard-library CLI performs fixed Voice dependency commands. The launchers and automatic updater call it, closing the current controller gap where Python dependencies converge but `ffmpeg`/`ffprobe` do not. Models remain a separate explicit workflow.

**Tech Stack:** Python 3.12, Conda, uv, subprocess/pathlib, Pinokio JavaScript, pytest, Git.

**Spec:** `/Users/thengmacmini/pinokio/api/studiohub-mac/docs/superpowers/specs/2026-08-20-unified-dependency-convergence-design.md`

## Global Constraints

- Work only in `/Users/thengmacmini/pinokio/api/voicestudio-mac.git`; preserve unrelated changes.
- `PINOKIO_HOME` is `/Users/thengmacmini/pinokio`; launchers keep the existing relative paths and `conda_env`.
- Cross-check launcher edits against `/Users/thengmacmini/pinokio/prototype/system/examples/mochi/install.js` and `update.js`.
- Add no package/requirement, model, catalog row, startup/service change, download, or fleet action.
- `base` installs fixed Conda package `ffmpeg` into `sys.prefix`, verifies `<sys.prefix>/bin/ffmpeg` and `ffprobe`, then installs `app/requirements.txt`.
- `generation` installs `app/requirements-generation.txt` and runs the current full Voice generation verifier.
- `all-installed` runs generation only when `diffusers` already exists.
- Status capabilities become `{"managed_exact_commit": true, "dependency_convergence": 1}`.
- Release `2.4.2` dated `2026-08-20`; stop after a reviewed local commit. No push or live app/fleet action.

---

### Task 1: Add fixed Voice convergence and route all callers through it

**Files:**
- Create: `app/backend/dependency_convergence.py`
- Create: `app/tests/test_dependency_convergence.py`
- Modify: `app/backend/auto_update.py`
- Modify: `app/tests/test_auto_update.py`
- Modify: `app/tests/test_managed_media_tools.py`
- Modify: `install.js`
- Modify: `update.js`
- Modify: `install_generation.js`

**Interfaces:**
- Consumes: active `sys.prefix`, fixed Pinokio `conda`/`uv` executables, checked-in requirements.
- Produces: `converge(mode: str, *, runner=subprocess.run) -> None`, `generation_installed() -> bool`, and CLI `python -m backend.dependency_convergence {base|generation|all-installed}`.

- [ ] **Step 1: Write failing command and safety tests**

```python
def test_base_installs_and_verifies_media_tools(fake_runner):
    convergence.converge("base", runner=fake_runner)
    assert fake_runner.argv[0] == [
        convergence.conda_executable(), "install", "-y", "-p", sys.prefix,
        "-c", "conda-forge", "ffmpeg",
    ]
    assert [str(Path(sys.prefix) / "bin" / "ffmpeg"), "-version"] in fake_runner.argv
    assert [str(Path(sys.prefix) / "bin" / "ffprobe"), "-version"] in fake_runner.argv
    assert any("requirements.txt" in str(row) for row in fake_runner.argv)

def test_all_installed_does_not_bootstrap_generation(monkeypatch, fake_runner):
    monkeypatch.setattr(convergence, "generation_installed", lambda: False)
    convergence.converge("all-installed", runner=fake_runner)
    assert all("requirements-generation.txt" not in str(row) for row in fake_runner.argv)

def test_generation_runs_full_verifier(fake_runner):
    convergence.converge("generation", runner=fake_runner)
    verifier = next(row for row in fake_runner.argv if row[:2] == [sys.executable, "-c"])
    for name in ("torch", "torchaudio", "transformers", "diffusers", "mlx_audio",
                 "mistral_common", "f5_tts", "fugashi", "jieba"):
        assert name in verifier[2]
```

Also test that `conda_executable()`/`uv_executable()` use only a valid `CONDA_EXE` or fixed paths under the configured Pinokio home and fail closed when absent.

- [ ] **Step 2: Run RED**

Run: `./conda_env/bin/python -m pytest -q app/tests/test_dependency_convergence.py`

Expected: collection fails because the module is absent.

- [ ] **Step 3: Implement the fixed Voice command**

```python
APP = Path(__file__).resolve().parents[1]
MEDIA_BIN = Path(sys.prefix) / "bin"
GEN_VERIFY = (
    "import torch, torchaudio, transformers, diffusers, mlx, mlx_lm, mlx_audio, "
    "mistral_common, f5_tts, fugashi, jieba; from importlib.metadata import version; "
    "from misaki.ja import JAG2P; from misaki.zh import ZHG2P; "
    "assert version('mistral-common') == '1.11.5'; "
    "JAG2P(); ZHG2P(); print('GEN_VERIFY_OK')"
)

def converge(mode: str, *, runner=subprocess.run) -> None:
    if mode not in {"base", "generation", "all-installed"}:
        raise ValueError("mode must be base, generation, or all-installed")
    commands = []
    if mode in {"base", "all-installed"}:
        commands.extend([
            [conda_executable(), "install", "-y", "-p", sys.prefix,
             "-c", "conda-forge", "ffmpeg"],
            [str(MEDIA_BIN / "ffmpeg"), "-version"],
            [str(MEDIA_BIN / "ffprobe"), "-version"],
            pip_command(APP / "requirements.txt"),
        ])
    if mode == "generation" or (mode == "all-installed" and generation_installed()):
        commands.extend([
            pip_command(APP / "requirements-generation.txt"),
            [sys.executable, "-c", GEN_VERIFY],
        ])
    for argv in commands:
        runner(argv, cwd=APP, check=True, timeout=1800)
```

CLI input selects only the three modes. It must never accept a command, package, channel, executable, requirement path, registry value, or controller value. Error output is a bounded stage name, not environment data.

- [ ] **Step 4: Replace duplicated launcher dependency lists**

Keep all current Pinokio stop/restart/service/error/notify steps. Dependency steps become:

```javascript
message: ["python -m backend.dependency_convergence base"]
```

Use `all-installed` in `update.js` and `generation` in `install_generation.js`. Remove raw Conda/uv commands and inline import verification from launchers.

- [ ] **Step 5: Replace automatic updater dependency logic and publish capability**

```python
def _install_dependencies(self) -> None:
    self._run(
        [str(self._python()), "-m", "backend.dependency_convergence", "all-installed"],
        cwd=self.root / "app", timeout=1800,
    )
```

Publish `{"managed_exact_commit": True, "dependency_convergence": 1}`. Add a real updater-unit assertion that this path invokes `all-installed`, plus a failure regression proving an ffmpeg/convergence error blocks success and enters existing rollback.

- [ ] **Step 6: Add caller/media-tool regressions**

Assert the launchers each invoke the module once with the exact mode, contain no package-install list, and retain restart ownership. Assert internal updates verify both ffmpeg binaries. Assert a missing `diffusers` marker never installs generation requirements.

- [ ] **Step 7: Run focused checks**

```bash
./conda_env/bin/python -m pytest -q app/tests/test_dependency_convergence.py app/tests/test_auto_update.py app/tests/test_managed_media_tools.py app/tests/test_release_metadata.py
node --check install.js
node --check update.js
node --check install_generation.js
python3 -m py_compile app/backend/dependency_convergence.py app/backend/auto_update.py
```

Expected: all pass; injected runners prevent real Conda/uv execution.

### Task 2: Release, review, and locally commit Voice Studio 2.4.2

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html` (first embedded release note only)
- Review: Task 1 files

**Interfaces:**
- Consumes: Task 1's bridge-capable Voice updater.
- Produces: clean local release `2.4.2` and its exact commit SHA.

- [ ] **Step 1: Add truthful release metadata**

State that local/controller updates now share ffmpeg, base, and installed-generation convergence; optional generation remains explicit/installed-only; no model or live machine changed. Set `VERSION` to `2.4.2`.

- [ ] **Step 2: Run release and full repository gates**

```bash
python3 release_metadata_check.py
./conda_env/bin/python -m pytest -q app/tests
python3 -m compileall -q app/backend
for file in *.js; do node --check "$file"; done
./conda_env/bin/python -m pip check
git diff --check
```

Expected: zero failures and clean dependency/diff checks.

- [ ] **Step 3: Request read-only security/release review**

Review fixed commands, explicit tool resolution, ffmpeg/ffprobe checks, installed-only generation, rollback, bounded output, launcher restart ownership, metadata, and no model/fleet effect. Resolve every Critical/Important finding and rerun frozen-diff gates.

- [ ] **Step 4: Commit the local bridge release**

```bash
git add app/backend/dependency_convergence.py app/backend/auto_update.py app/tests/test_dependency_convergence.py app/tests/test_auto_update.py app/tests/test_managed_media_tools.py install.js update.js install_generation.js VERSION CHANGELOG.md app/frontend/index.html
git diff --cached --check
git commit -m "fix: unify Voice dependency convergence"
git status --short
git rev-parse HEAD
```

Expected: one clean local release commit and no push.
