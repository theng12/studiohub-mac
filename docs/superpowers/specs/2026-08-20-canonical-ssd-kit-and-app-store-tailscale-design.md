# Canonical SSD Kit and App Store Tailscale Design

## Outcome

The TerraNash SSD remains a three-stage, double-clickable new-Mac kit, but its
scripts and documentation have one canonical source in the Studio Hub Git
repository. Stage 1 installs only Pinokio, Yam Display, and Latest from verified
DMG files. Tailscale is installed manually from the Mac App Store and no
Tailscale package remains on the SSD.

## Root cause

The current three-stage kit was edited directly on the removable SSD while the
Studio Hub repository retained a different older bootstrap implementation.
Pushing Studio Hub therefore could not update the kit. The SSD log also proves
that the standalone Tailscale package's installer scripts failed and aborted
Stage 1 before the remaining application could install.

## Canonical source and synchronization

All executable text files, tests, the ordinary-app manifest, and owner
documentation live under `ssd_bootstrap/` in Studio Hub. Installer DMGs, model
packages, logs, backups, and machine-local artifacts remain outside Git.

`tools/sync_ssd_bootstrap.py` accepts an explicit mounted volume root. It copies
only the tracked canonical files, removes the obsolete Tailscale package,
regenerates `terranash-bootstrap/RELEASE-INVENTORY.sha256`, and verifies the
result. It preserves installers still referenced by the manifest, models,
logs, backups, and unrelated volume contents. It supports `--check` so future
sessions can detect drift without writing.

## Stage 1

The manifest contains exactly Pinokio 8.0.40, Yam Display 2.4.7, and Latest
0.11. Since all remaining installers are DMGs, the installer has no package or
root-package installation path. Completion instructions tell the owner to:

1. finish Pinokio's visible Install Tools flow;
2. install Tailscale from the Mac App Store and sign in;
3. approve Yam Display permissions;
4. open Latest and review supported applications.

Failure of one application remains explicit and restartable. No Studio,
model, fleet, enrollment, generation, or live-machine action occurs in Stage 1.

## Preservation and release

Stages 2–4 retain their current behavior, including independent Studio startup,
8 GB Qwen3-TTS 0.6B Base restoration, and startup repair. The mounted SSD is
refreshed from the canonical source only after tests pass. Studio Hub ships a
patch release with matching version, changelog, and What's New evidence.

## Verification

- manifest/test proof that Tailscale and all package-install code are absent;
- Stage 1 dry-run lists exactly the three DMGs and the App Store instruction;
- all SSD Python, command-wrapper, and fixture tests pass from canonical source;
- sync to a temporary fixture proves preservation, obsolete-package removal,
  inventory regeneration, and drift detection;
- the mounted SSD passes the same tests, Python compilation, command syntax,
  secret scan, and inventory verification;
- Studio Hub's complete test/release matrix passes before commit and push.
