# Runtime-State Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship durable Image/Voice/Hub runtime-state handling and a reproducible SSD command that safely migrates old Macs once.

**Architecture:** Image and Voice move mutable `ENVIRONMENT` data out of Git into an ignored file seeded from a tracked template. Hub ignores its runtime journal. A standard-library SSD tool proves and backs up only the known historical mutations, invokes the existing local Pinokio update scripts, and verifies the new baseline.

**Tech Stack:** Python 3 standard library, Bash/Zsh, Pinokio launcher APIs, Git, pytest/unittest.

**Spec:** `docs/superpowers/specs/2026-08-21-runtime-state-migration-design.md`

## Global Constraints

- Release Image 1.30.4, Voice 2.4.3, and Hub 2.11.6.
- Do not weaken dirty-checkout, exact-commit, dependency-convergence, health, or rollback gates.
- Do not delete runtime journal/lock files or use broad Git cleanup operations.
- Do not copy machine secrets or backups to the SSD.
- Keep the SSD kit canonical in Studio Hub and deploy only through `tools/sync_ssd_bootstrap.py`.
- No live fleet update, restart, repair, model, enrollment, roster, or GenStudio action.

---

### Task 1: Image machine-local environment

**Files:**
- Move: `ENVIRONMENT` to `ENVIRONMENT.example`
- Modify: `.gitignore`, `install.js`, `install_service.sh`, `app/backend/auto_update.py`, `README.md`, `VERSION`, `CHANGELOG.md`, `app/frontend/index.html`
- Test: `app/tests/test_startup_ownership.py`, `app/tests/test_auto_update.py`, release metadata tests

**Interfaces:**
- Produces: ignored `ENVIRONMENT`, tracked `ENVIRONMENT.example`, seed-if-missing install behavior, exact porcelain path rendering.

- [x] Write failing tests that require `ENVIRONMENT.example` to be tracked, `ENVIRONMENT` to be ignored, install to seed only when absent, service installation to preserve unrelated settings, rollback to a legacy tracked layout to preserve exact machine bytes/mode, and `" M ENVIRONMENT"` to render as `ENVIRONMENT`.
- [x] Run the focused tests and confirm they fail for the current tracked-file and parser behavior.
- [x] Move the template, add the root ignore, add a minimal seed-before-convergence launcher step using a relative path, and parse porcelain status columns without calling `strip()` on the complete output.
- [x] Update Image release metadata to 1.30.4 and document that existing machine settings are preserved.
- [x] Run focused tests, Node/shell syntax, metadata tests, full Image tests, dependency check, and diff check.
- [x] Obtain an independent review before integration.

### Task 2: Voice machine-local environment

**Files:**
- Move: `ENVIRONMENT` to `ENVIRONMENT.example`
- Modify: `.gitignore`, `install.js`, `install_service.sh`, `app/backend/auto_update.py`, `README.md`, `VERSION`, `CHANGELOG.md`, `app/frontend/index.html`
- Test: `app/tests/test_service_startup_settings.py`, `app/tests/test_auto_update.py`, release metadata tests

**Interfaces:**
- Produces: the same machine-local environment contract as Image with Voice-specific defaults.

- [x] Write failing tests for tracked/ignored state, seed-without-overwrite, service preservation, legacy rollback preservation, and the exact `NVIRONMENT` regression.
- [x] Run the focused tests and confirm the expected failures.
- [x] Apply the minimal template/ignore/seed/parser changes while preserving service ownership and dependency convergence.
- [x] Update Voice release metadata to 2.4.3.
- [x] Run focused tests, Node/shell syntax, metadata tests, full Voice tests, dependency check, and diff check.
- [x] Obtain an independent review before integration.

### Task 3: Hub runtime journal baseline

**Files:**
- Modify: `.gitignore`, updater parser source if affected, `VERSION`, `CHANGELOG.md`, `app/frontend/index.html`
- Test: updater and release metadata tests

**Interfaces:**
- Produces: ignored `.enrollment_repair_journal.json` with no change to durable journal semantics.

- [x] Write failing tests requiring the root journal and existing lock files to be ignored and filename rendering to preserve the first character.
- [x] Run the focused tests and confirm the expected failures.
- [x] Add only the exact journal ignore and minimal parser fix.
- [x] Update Hub release metadata to 2.11.6.
- [x] Run focused updater/repair/metadata tests and diff check.

### Task 4: Local migration engine

**Files:**
- Create: `ssd_bootstrap/kit/runtime_state_migration.py`
- Test: `ssd_bootstrap/kit/tests/test_runtime_state_migration.py`

**Interfaces:**
- Consumes: existing `fleet_bootstrap.resolve_pinokio_home()` and installed app-name compatibility.
- Produces: `MigrationEngine.plan()` and `MigrationEngine.run()` results with per-repository status, backup path, and refusal reason.

- [x] Write temporary-Git-repository tests for clean old state, exact three-line mutation, unknown edit, unknown dirty path, legacy `.git` directory name, absent/new environment, backup mode 0600, interrupted update restoration, and idempotent migrated state.
- [x] Run the tests and confirm collection/behavior failures before the engine exists.
- [x] Implement NUL-safe Git inspection, exact historical-delta validation, local backup, exact `.git/info/exclude` bridge, supported Pinokio update invocation, fail-fast behavior, and post-update verification using only the standard library.
- [x] Run the focused migration tests and confirm all pass.

### Task 5: SSD owner command and documentation

**Files:**
- Create: `ssd_bootstrap/kit/5 Migrate Studio Updates.command`
- Modify: `ssd_bootstrap/kit/README.md`, `ssd_bootstrap/root/START HERE - TerraNash Mac Setup.md`, `ssd_bootstrap/kit/tests/test_commands.py`, sync-tool tests

**Interfaces:**
- Produces: a double-clickable Stage 5 and `--dry-run` path backed by Task 4.

- [x] Write failing command tests proving Stage 5 invokes only the migration engine, dry-run performs no writes or updates, and the docs describe physical-use timing and second-SSD reproduction.
- [x] Run tests and confirm the command is absent.
- [x] Add the thin three-line direct wrapper matching the existing Stage 4 house pattern; do not route no-write dry runs through the logging dispatcher or add a second installation framework.
- [x] Update owner documentation and sync/inventory expectations.
- [x] Run command, migration, sync, and shell syntax tests.
- [x] Perform a fixture-only dry run; do not execute the migration against the control Mac.

### Task 6: Cross-repository acceptance

**Files:**
- Modify tests only if a missing acceptance seam is proven.
- Report: `.superpowers/sdd/2026-08-21-runtime-state-migration/final-report.md`

**Interfaces:**
- Consumes: all prior task contracts.
- Produces: evidence that an old service-owned environment can cross the Git deletion/untracking transition without data loss and subsequent updates see clean checkouts.

- [x] Build three disposable fixture repositories at the old layouts and run the real migration engine against fake Pinokio update commands that fast-forward to the new layouts.
- [x] Assert preserved settings/service ownership, clean Git status, ignored runtime files, no deleted journals, and no secret/backup data on the SSD fixture.
- [x] Run all three full repository suites plus compile, Node, shell, pip/dependency, metadata, and diff checks.
- [x] Run independent security/durability and release-scope reviews; resolve every Critical/Important finding with TDD.

### Task 7: Release, synchronize, and preserve

**Files:**
- Commit all reviewed release files in their owning repositories.
- Synchronize: `/Volumes/ugreen-terranash` from Hub `ssd_bootstrap` canonical source.

**Interfaces:**
- Produces: published main commits and a byte-matching physical SSD while retaining the Git source for future SSDs.

- [ ] Stage reviewed explicit file lists only and confirm staged diffs exclude unrelated files, secrets, models, caches, and launchers outside scope.
- [ ] Commit and push Voice and Hub according to their release rules; use the protected-main PR flow for Image if required.
- [ ] Rebase/merge local mains to the published commits and rerun release metadata/diff checks.
- [ ] Run `tools/sync_ssd_bootstrap.py --volume /Volumes/ugreen-terranash`, then rerun it with `--check` and require zero drift.
- [ ] Verify `RELEASE-INVENTORY.sha256`, command executable modes, no Tailscale installer, no secret material, and exact canonical/SSD byte equality.
- [ ] Report published versions/commits, test counts, SSD path, canonical source path, and the deliberate no-fleet-update boundary.
