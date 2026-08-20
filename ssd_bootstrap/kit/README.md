# TerraNash New-Mac SSD Kit

Start with [`../START HERE - TerraNash Mac Setup.md`](../START%20HERE%20-%20TerraNash%20Mac%20Setup.md).
The canonical scripts and documentation live in Studio Hub's tracked
`ssd_bootstrap/` directory; this SSD folder is the synchronized deployment copy.

## The three owner commands

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

All three commands are restartable. A completed app/environment/package is
detected and skipped. When a command fails, fix the named prerequisite and run
that same command again.

## Model-library meaning

Refreshing the SSD discovers complete, locally cached catalog packages. It
does not automatically download every catalog model. New models using the
existing Hugging Face cache layout need no script change after they have been
downloaded in their Studio. Engines with a different storage layout require an
explicit `studio_models.py` update and verification first.

Normal restore never prunes. On an 8 GB Mac, Voice restore is an exact
allowlist: Qwen3-TTS 0.6B Base plus its required
`mlx-community/whisper-large-v3-turbo` quality checker. CustomVoice and every
unrelated audio generator are skipped. The tool preserves complete SSD
packages not visible on today's source Mac and protects conflicting local
fleet voices.

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
```

Developer verification:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile mac_apps.py fleet_bootstrap.py studio_models.py repair_startup.py tests/*.py
for file in ./*.command; do zsh -n "$file"; done
shasum -a 256 -c RELEASE-INVENTORY.sha256
```

No password, token, session, enrollment code, or fleet credential belongs on
this SSD.
