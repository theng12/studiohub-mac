# TerraNash New-Mac SSD Kit

Start with [`../START HERE - TerraNash Mac Setup.md`](../START%20HERE%20-%20TerraNash%20Mac%20Setup.md).
The canonical scripts and documentation live in Studio Hub's tracked
`ssd_bootstrap/` directory; this SSD folder is the synchronized deployment copy.

## The six owner commands

1. Double-click **1 Install Mac Apps.command**. It installs only Pinokio, Yam
   Display, and Latest from the signed, checksum-pinned DMGs in `installers/`.
   It also creates the Pinokio login item. It never starts Apple's Command Line
   Tools installer and never installs a Studio. Install Tailscale separately
   from the Mac App Store so the App Store owns its updates.
2. Open Pinokio and finish its visible **Install Tools** first-run work. Install
   Tailscale from the Mac App Store, sign in, and approve Yam Display's requested permissions. Then
   double-click **2 Install Studios.command**. It installs/adopts Image Studio,
   Voice Studio, and Studio Hub, rebuilds required environments, and configures
   independent startup settings for all three. It neither copies models nor enrolls the
   Mac.
3. Double-click **3 Manage AI Models.command** and choose:
   - **1** to refresh the SSD from complete models and fleet voices already on
     this trusted source Mac;
   - **2** to restore only models suitable for this Mac's unified memory; or
   - **3** for the advanced restore-all action.
4. If an existing Mac shows Hub waiting for Image/Voice, or Pinokio and a
   launchd service both try to start the same app, double-click **4 Repair
   Studio Startup.command**. It changes startup settings only and does not
   start, stop, delete, reinstall, update, or re-enroll anything.
5. Use **5 Migrate Studio Updates.command** only as the one-time recovery path
   for an older Mac whose own Hub or Studio checkout cannot complete the normal
   remote update. First make sure Image Studio, Voice Studio, and Studio Hub are
   idle and keep Pinokio open. Stage 5 preserves the Mac's runtime settings,
   updates Image then Voice then Hub, and verifies each app before continuing.
   A refusal names the checkout that needs manual review; it never stashes,
   resets, deletes, or hides unrelated changes. A healthy enrolled Mac should
   instead receive Hub, Image, and Voice updates from the controller.
6. On an older Mac with unknown history, double-click **6 Inspect and Fix This
   Mac.command** after making sure all three Studios are idle and Pinokio is
   open. It first refuses unsafe or unexpected checkout state, restores only
   the RAM-suitable Choice 2 models, safely migrates legacy update state or
   updates already-current Image, Voice, and Hub checkouts with dependency
   convergence, then repairs startup ownership. It prunes SSD-recognized model
   packages outside the selected tier, but never selects restore-all, removes
   unknown/private packages, re-enrolls the Mac, or changes another machine.

All six commands are restartable. A completed app/environment/package is
detected and skipped. When a command fails, fix the named prerequisite and run
that same command again.

## Existing fleet Macs: remote first

For a reachable enrolled Mac, update the controller Hub first, then use
**Updates → Studio Hub updates (agent Macs) → Update ready Hubs**. After every
reachable Agent Hub is current, update only Image and Voice from **Studio
updates**. No SSD is required for that normal path. Bring this SSD to a Mac only
when its Hub is unreachable, too old to accept the controller update, or blocked
by a legacy checkout. Run Stage 5 for the narrow update-state repair, or Stage
6 when its history is uncertain and it should also receive RAM-suitable models
and startup repair. Future code and declared dependency updates can run through
the controller or overnight schedule.

## Model-library meaning

Refreshing the SSD discovers complete, locally cached catalog packages. It
does not automatically download every catalog model. New models using the
existing Hugging Face cache layout need no script change after they have been
downloaded in their Studio. Engines with a different storage layout require an
explicit `studio_models.py` update and verification first.

Normal Stage 3 restore never prunes; Step 6 explicitly prunes recognized
packages outside the selected tier. On an 8 GB Mac, Voice restore is an exact
allowlist: Qwen3-TTS 0.6B Base, its required
`mlx-community/whisper-large-v3-turbo` quality checker, and
`mlx-community/Kokoro-82M-bf16`. CustomVoice and every unrelated audio
generator are skipped. The tool preserves complete SSD packages not visible
on today's source Mac and protects conflicting local fleet voices.

## Folder-name compatibility

New checkouts use `imagestudio-mac`, `voicestudio-mac`, and `studiohub-mac`.
Existing folders ending in `.git` remain supported. Canonical wins if both
exist; the legacy folder is preserved for manual review. An existing checkout
with the wrong Git origin is refused.

## Latest coverage

Latest maintains supported third-party applications such as Yam Display. The
Mac App Store owns Tailscale updates. Pinokio and the Studios retain their own
update paths and are not delegated to Latest.

## Diagnostics and dry runs

Logs are written to `~/Library/Logs/TerraNash/` and, when writable, `logs/` on
the SSD. Inspect the newest relevant log before changing code.

No-write checks:

```sh
TERRANASH_NONINTERACTIVE=1 './1 Install Mac Apps.command' --dry-run
TERRANASH_NONINTERACTIVE=1 './2 Install Studios.command' --dry-run
TERRANASH_NONINTERACTIVE=1 './3 Manage AI Models.command' --dry-run --action stage
TERRANASH_NONINTERACTIVE=1 './3 Manage AI Models.command' --dry-run --action restore
./'4 Repair Studio Startup.command' --dry-run
./'5 Migrate Studio Updates.command' --dry-run
./'6 Inspect and Fix This Mac.command' --dry-run
```

Developer verification:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile mac_apps.py fleet_bootstrap.py studio_models.py repair_startup.py runtime_state_migration.py tests/*.py
for file in ./*.command; do zsh -n "$file"; done
shasum -a 256 -c RELEASE-INVENTORY.sha256
```

No password, token, session, enrollment code, or fleet credential belongs on
this SSD.

## Recreate another SSD

Studio Hub's tracked `ssd_bootstrap/` tree is the canonical copy. To reproduce
this kit on a second mounted SSD, use the current Studio Hub checkout rather
than copying or hand-editing this deployment folder:

```sh
python3 tools/sync_ssd_bootstrap.py --volume /Volumes/NAME-OF-SECOND-SSD
python3 tools/sync_ssd_bootstrap.py --volume /Volumes/NAME-OF-SECOND-SSD --check
```

The sync preserves model assets and logs, rebuilds the checksum inventory, and
keeps Tailscale out of the kit so it remains an App Store installation.
