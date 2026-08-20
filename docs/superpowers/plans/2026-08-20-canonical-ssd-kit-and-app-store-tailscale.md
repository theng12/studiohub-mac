# Canonical SSD Kit and App Store Tailscale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the mounted TerraNash SSD a Git-synchronized three-stage kit whose first stage installs only Pinokio, Yam Display, and Latest, with Tailscale delegated to the Mac App Store.

**Architecture:** Track the kit's text sources under `ssd_bootstrap/` and deploy them with one deterministic `tools/sync_ssd_bootstrap.py` command. Keep large installer/model assets and logs off Git, copy only an allowlist of source files, explicitly remove the retired Tailscale package, and regenerate a checksum inventory after synchronization.

**Tech Stack:** Python 3 standard library, zsh command wrappers, unittest, Git.

**Spec:** `docs/superpowers/specs/2026-08-20-canonical-ssd-kit-and-app-store-tailscale-design.md`

## Global Constraints

- Do not install, update, start, stop, enroll, or contact any fleet machine.
- Preserve SSD models, logs, backups, remaining installer DMGs, and unrelated files.
- Stage 1 contains exactly Pinokio, Yam Display, and Latest; Tailscale is App Store-only.
- Commit once as a versioned Studio Hub patch release after the entire matrix passes.

---

### Task 1: Import one canonical SSD source tree

**Files:**
- Create: `ssd_bootstrap/kit/**`
- Create: `ssd_bootstrap/root/START HERE - TerraNash Mac Setup.md`

**Interfaces:**
- Consumes: the mounted `/Volumes/ugreen-terranash/terranash-bootstrap` text files.
- Produces: a repository-owned source tree with no logs, backups, model bytes, credentials, or installer binaries.

- [x] Copy the current wrappers, Python tools, tests, manifest, and owner docs into `ssd_bootstrap/` using `apply_patch`.
- [x] Scan the imported tree for credential-shaped data and absolute owner paths.
- [x] Confirm imported wrappers still resolve all runtime paths relative to the mounted kit.

### Task 2: Remove Tailscale and simplify Stage 1 with TDD

**Files:**
- Modify: `ssd_bootstrap/kit/installers/MANIFEST.json`
- Modify: `ssd_bootstrap/kit/mac_apps.py`
- Modify: `ssd_bootstrap/kit/tests/test_mac_apps.py`
- Modify: `ssd_bootstrap/kit/README.md`
- Modify: `ssd_bootstrap/root/START HERE - TerraNash Mac Setup.md`

**Interfaces:**
- Consumes: `load_manifest(path: Path) -> list[InstallerAsset]`.
- Produces: a DMG-only three-application Stage 1 and manual App Store Tailscale instructions.

- [x] Add failing tests asserting manifest IDs are exactly `pinokio`, `yam-display`, and `latest`, source contains no package installer command, and completion output names the Mac App Store.
- [x] Run `python3 -m unittest ssd_bootstrap.kit.tests.test_mac_apps -v` and observe failure.
- [x] Delete the Tailscale manifest entry and package installation branch, constrain `kind` to `dmg`, and update operator copy.
- [x] Rerun focused tests and confirm they pass.

### Task 3: Add deterministic SSD synchronization with TDD

**Files:**
- Create: `tools/sync_ssd_bootstrap.py`
- Create: `app/tests/test_sync_ssd_bootstrap.py`

**Interfaces:**
- Produces: `sync(source_root: Path, volume_root: Path, *, check: bool) -> SyncResult` and CLI `--volume`, `--check`.

- [x] Write fixture tests proving allowlisted files copy, preserved directories survive, `Tailscale-*.pkg` is removed, inventory is regenerated, and `--check` reports drift without writing.
- [x] Run the focused test and observe failure because the module is absent.
- [x] Implement the smallest standard-library sync tool using atomic temporary-file replacement.
- [x] Run the focused test and canonical SSD tests until green.

### Task 4: Refresh the mounted SSD and ship the release

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html`
- Generated on SSD: `terranash-bootstrap/RELEASE-INVENTORY.sha256`

**Interfaces:**
- Consumes: `tools/sync_ssd_bootstrap.py` and `/Volumes/ugreen-terranash`.
- Produces: a verified mounted SSD and one pushed Studio Hub patch release.

- [x] Bump Studio Hub to 2.11.4 and add matching dated release notes.
- [x] Run the sync tool against the mounted SSD and verify the obsolete Tailscale package is absent.
- [x] Run every canonical and mounted SSD unittest, Python compile, zsh syntax, dry-run, secret scan, and inventory check.
- [x] Run Studio Hub's full tests, compile, dependency, JavaScript, shell, and diff checks.
- [x] Stage only reviewed source/release files, commit once, push `main`, and verify local, tracking, and remote SHAs match.

## Self-review

- Spec coverage: canonical ownership, Tailscale removal, preservation, sync/check, SSD refresh, and release are each assigned.
- Placeholder scan: no deferred implementation language or unnamed test remains.
- Type consistency: Task 3 defines the only new public Python interface used by Task 4.
