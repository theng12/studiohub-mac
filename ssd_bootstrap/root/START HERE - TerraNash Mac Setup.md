# TerraNash New-Mac Setup — Start Here

Status: implemented and offline-verified on the SSD.
Last reviewed: 2026-08-20 (Asia/Phnom_Penh)

This document is the durable owner handoff for the TerraNash fleet SSD. The
canonical scripts, tests, manifest, and documentation live in Studio Hub's
tracked `ssd_bootstrap/` directory; the mounted SSD is a synchronized deployment
copy. Read this document and the canonical Git source before changing anything
under `terranash-bootstrap/`.

## Purpose

Prepare a clean Apple-silicon Mac in three deliberate, restartable stages:

1. Install ordinary Mac applications.
2. Install Studio Hub, Image Studio, Voice Studio, and their dependencies.
3. Refresh or restore the SSD's AI model library.

The stages are separate so Pinokio's first-run **Install Tools** work never runs
at the same time as Studio installation. Each stage must be safe to rerun and
must keep completed work.

## Owner workflow

### 1. Install Mac Apps

Run `1 Install Mac Apps.command`.

It installs these signed, checksum-pinned applications from the SSD:

- Pinokio
- Yam Display
- Latest

It must not install any Studio. It must not invoke `xcode-select --install` or
try to own Apple's Command Line Tools installation. After the applications are
installed, the owner manually:

1. Opens Pinokio and completes its visible **Install Tools** / first-run work,
   including any Apple Command Line Tools prompt.
2. Installs Tailscale from the Mac App Store, opens it, signs in, and approves
   the requested macOS VPN/system extension permissions.
3. Opens Yam Display and approves any required permissions.
4. Opens Latest and reviews its detected applications.

Latest is a convenience updater, not the fleet update authority. It can update
supported third-party applications such as Yam Display. The Mac App Store owns
Tailscale updates. Pinokio uses its own Electron updater, and the three Studios
use their own Studio/Hub update contracts.

### 2. Install Studios

Run `2 Install Studios.command` only after Pinokio's visible first-run tools are
finished.

This stage must check readiness quickly. If Pinokio, `pterm`, Node, or a usable
Python are not ready, it exits with a clear instruction to finish Pinokio's
visible setup and rerun the same command. It must not sit for many minutes while
another installer works in parallel.

It then performs these actions serially:

1. Resolve `PINOKIO_HOME` from Pinokio's configuration or control plane.
2. Install or adopt the three checkouts:
   - `https://github.com/theng12/imagestudio-mac.git`
   - `https://github.com/theng12/voicestudio-mac.git`
   - `https://github.com/theng12/studiohub-mac.git`
3. Run each checked-in Pinokio `install.js` when its verified base environment
   is absent or incomplete.
4. Run `install_generation.js` for Image Studio and Voice Studio when their
   verified generation environments are absent or incomplete.
5. Configure Image Studio, Voice Studio, and Studio Hub to autostart
   independently. Hub must not declare Image or Voice as a Pinokio dependency;
   each app may instead be owned by its own launchd startup service.

This stage does **not** copy models and does **not** enroll the Mac into a site.
Enrollment remains a separate owner action after the machine identity and site
are known.

#### `-mac` and `-mac.git`

The canonical new folder names are:

- `imagestudio-mac`
- `voicestudio-mac`
- `studiohub-mac`

Older Pinokio installations may already use folders ending in `.git`. The
bootstrap must continue to support both forms:

- Prefer an existing canonical `*-mac` checkout.
- Otherwise adopt the matching legacy `*-mac.git` checkout.
- For a new install, create the canonical `*-mac` folder.
- Verify the Git origin before using an existing directory.
- If both forms exist, use the canonical folder and leave the legacy folder
  untouched for explicit later review. Never silently merge or delete either.
- Build Pinokio references from the folder that was actually selected. Do not
  create cross-Studio startup dependencies.

The SSD version inspected on 2026-08-19 already contains this compatibility
logic. A dry run correctly adopted the local legacy `voicestudio-mac.git`
checkout while using canonical Image Studio and Studio Hub folders.

### 3. Manage AI Models

Run `3 Manage AI Models.command` and choose one action.

#### Choice A — Update the SSD model library from this Mac

Use this on a trusted source Mac whose Studios contain the models and fleet
voices the owner wants to distribute.

The tool must:

- Discover the current Image Studio and Voice Studio catalogs.
- Stage only complete model packages that are already downloaded locally.
- Add newly downloaded catalog models without a code change whenever they use
  the existing Hugging Face cache layout.
- Replace a staged package only when its complete local package changed.
- Preserve valid staged packages that are not visible on today's source Mac.
- Stage required companion packages, such as a Whisper tokenizer/processor,
  when the Studio catalog requires them.
- Refresh `studio-models/MANIFEST.json` atomically after all copies succeed.
- Stage stable fleet voice references with checksums.
- Never copy Conda/Python environments, incomplete download fragments,
  credentials, sessions, tokens, or enrollment codes.

“Automatic model update” means the tool discovers and stages newly downloaded
catalog models. It does not download every model automatically. The owner still
chooses what to download in each Studio before refreshing the SSD.

If a future engine stores assets outside the existing Hugging Face cache or
needs a new kind of companion package, update and test `studio_models.py`
before claiming that engine is portable.

#### Choice B — Copy suitable models from the SSD to this Mac

The tool must:

- Require completed Stage 2 checkouts.
- Read the manifest and detect the Mac's unified memory.
- Copy Image packages whose recorded memory floor fits the Mac. On an 8 GB Mac,
  copy exactly Qwen3-TTS 0.6B Base,
  `mlx-community/whisper-large-v3-turbo`, and
  `mlx-community/Kokoro-82M-bf16` for Voice; do not copy CustomVoice or any
  other Voice generator. The advanced “copy all” option remains explicit.
- Skip already complete packages.
- Resume safely after interruption.
- Refuse to overwrite an unknown conflicting package.
- Restore verified fleet voices without replacing a locally modified conflict.
- Leave model loading and generation stopped during the copy.

### 4. Repair Studio startup conflicts

Run `4 Repair Studio Startup.command` on an existing Mac if Hub waits for
Image/Voice, Pinokio and launchd both try to own the same app, or both a
canonical and `.git` checkout appear in startup settings. The repair:

- clears all cross-Studio `PINOKIO_SCRIPT_REQUIRES` values;
- enables independent Pinokio autolaunch for the selected checkout when no
  startup service marker exists;
- disables Pinokio autolaunch when launchd already owns that app;
- disables autolaunch on a duplicate `.git` checkout while preserving it;
- does not start, stop, delete, reinstall, update, or re-enroll anything.

Restart Pinokio when convenient after the repair completes.

### 5. Recover an older Mac whose normal update is blocked

Reachable enrolled Macs should be updated remotely first: update the controller
Hub, use **Updates → Studio Hub updates (agent Macs) → Update ready Hubs**, then
update only Image and Voice from **Studio updates**. That normal path installs
declared dependencies and does not require the SSD.

Run `5 Migrate Studio Updates.command` locally only when an older Mac's own Hub
or Studio checkout cannot complete that remote flow. Keep Pinokio open and wait
until Image Studio, Voice Studio, and Studio Hub have no customer work,
downloads, installs, or updates in progress. The command:

- accepts both canonical and legacy `.git` checkout folder names;
- preserves the machine-local `ENVIRONMENT` bytes and file mode;
- updates Image Studio, then Voice Studio, then Studio Hub, stopping on the
  first refusal or failed verification;
- leaves Hub's repair journal and lock files in place while adding exact local
  Git ignore entries, without deleting them;
- requires the updated app to report healthy and dependency convergence
  capability `1` before continuing; and
- never stashes, resets, broad-cleans, deletes, re-enrolls, copies models, or
  contacts another fleet machine.

Use `./'5 Migrate Studio Updates.command' --dry-run` first for a no-write
inspection. If it reports an unknown local change, preserve that change and
stop for review. Once Stage 5 succeeds, future controller and overnight Studio
updates can install both code and declared dependencies automatically. A new
Mac installed from current Stage 2 may report `already_ready`; that is a safe
no-op.

## Target SSD layout after implementation

```text
ugreen-terranash/
├── AGENTS.md
├── START HERE - TerraNash Mac Setup.md
├── terranash-bootstrap/
│   ├── 1 Install Mac Apps.command
│   ├── 2 Install Studios.command
│   ├── 3 Manage AI Models.command
│   ├── 4 Repair Studio Startup.command
│   ├── 5 Migrate Studio Updates.command
│   ├── .terranash-bootstrap.command
│   ├── fleet_bootstrap.py
│   ├── studio_models.py
│   ├── repair_startup.py
│   ├── runtime_state_migration.py
│   ├── installers/
│   └── logs/
└── studio-models/
    └── MANIFEST.json
```

The older combined entry points were retained in a checksum-backed backup under
`terranash-bootstrap/backups/2026-08-19-before-three-stage/`, then removed from
the owner-facing folder only after the three replacements passed dry-run and
fixture verification.

## Current software context

Installer assets observed and pinned on 2026-08-19:

- Pinokio 8.0.40 — `Pinokio-8.0.40-arm64.dmg` — SHA-256
  `3c0f55f769efc2c02e5d0b8bc24e2ee7b0be54d42e6404663887e0cf8d3df3fd`.
- Yam Display 2.4.7 — `YamDisplay-2.4.7.dmg` — SHA-256
  `15ad34e950f078b66834f0f3ebc1ade53fcbb2f9ab6cfa51f720ba040dabbd46`.
- Latest 0.11 — `Latest-0.11.dmg` — SHA-256
  `e098ed410240dc90d75faa576b61d384888f51b93ca742c98a599514b02a197e`.

Current SSD repair/update context refreshed on 2026-08-25:

- Studio Hub 2.13.0;
- Image Studio 1.30.5; and
- Voice Studio 2.4.4.

Stage 2 always installs the current published `main`, not a historical commit
listed in this guide. Stage 5 performs the one-time migration needed by older
checkouts before ordinary automatic dependency-converging updates.
- Voice Studio 2.4.0 added internal Moonshine Base and Nemotron 3.5 ASR
  Streaming candidates. They were not downloaded on the source Mac during this
  audit, so they were not yet present in the SSD manifest.
- SSD maintenance does not update or restart any fleet Mac.

These Studio version numbers are historical context, not install pins. Stage 2
downloads the current repository checkout through Pinokio. Offline Mac-app
installers, by contrast, are pinned assets and must have their version,
signature identity, source URL, and SHA-256 recorded when refreshed.

## Security and preservation rules

- Store no passwords, owner sessions, Hub tokens, fleet tokens, Tailscale auth
  keys, enrollment codes, or other reusable secrets on the SSD.
- Never copy a working Studio's `conda_env`, service state, jobs, databases,
  credentials, or machine identity onto another Mac.
- Do not alter live fleet membership, update a fleet node, enroll a node, or
  start customer generation while maintaining this SSD.
- Verify downloaded installer hashes and Apple signing/notarization before
  making them available to the owner.
- Use official vendor download sources.
- Keep all operations idempotent. A rerun must skip verified completed work.
- Preserve existing model data unless an explicit, verified replacement is
  complete. No automatic pruning in the normal workflow.
- Logs may contain paths and diagnostics but must not contain prompted secrets.

## Logs and troubleshooting

Bootstrap logs live under `terranash-bootstrap/logs/` and, when writable, under
`~/Library/Logs/TerraNash/` on the target Mac. Inspect the newest relevant log
before changing code.

The previously observed “two instances” were not two calls to Apple's Xcode
installer. The old Stage 1 opened Pinokio, waited for Pinokio's own first-run
tools, and then continued into Studio installation while the owner could still
see Pinokio's **Install Tools** work. The three-stage design fixes the ownership
problem by ending Stage 1 before any Studio installation starts.

## Verification required before release

For bootstrap-source changes:

1. Run shell syntax checks for every `.command` file.
2. Run Python compilation checks for every Python file.
3. Run focused unit/fixture checks for installer selection, canonical/legacy
   checkout resolution, manifest updates, memory filtering, interruption
   recovery, and secret exclusion.
4. Run all three stages in a no-write dry-run against a temporary fake home.
5. Verify Stage 1 never starts a Studio or invokes `xcode-select --install`.
6. Verify Stage 2 performs no model copy and no enrollment.
7. Verify both Stage 3 choices without touching the live fleet.
8. Confirm no installer, Python test, model-copy, mount, or temporary listener
   remains active.
9. Record the checks, installer versions, hashes, and known limitations here.

Do not use the owner's production Mac as proof of a clean-new-Mac install. Use
a temporary fixture first, then an explicitly authorized spare Mac when the
owner chooses to validate it.

## Instructions for a future coding assistant

1. Read this file and the applicable workspace `AGENTS.md` files completely.
2. Inspect `terranash-bootstrap/logs/` before diagnosing a reported failure.
3. Inventory the mounted SSD and current Git status of all three Studio
   repositories before editing.
4. Treat Studio Hub's tracked `ssd_bootstrap/` directory as the canonical
   source and the SSD as the deployment artifact. Use
   `tools/sync_ssd_bootstrap.py --volume <mounted-volume>` to refresh it; do not
   hand-edit a second copy on the drive.
5. Use relative paths inside the SSD scripts. Resolve the target Mac's actual
   Pinokio home at runtime; never hardcode an owner's home directory.
6. Use Pinokio's supported `pterm` interfaces for Studio download/install/start
   operations. Do not bypass Pinokio with ad hoc environment commands.
7. Preserve canonical and legacy checkout compatibility exactly as described.
8. Keep the three stages independent. Do not recombine them into an all-in-one
   wizard without new owner approval.
9. Do not add background automation, fleet rollout, enrollment, or unattended
   model downloads to this bootstrap without explicit owner approval.
10. Update this document whenever behavior, installer assets, model layout, or
    operator steps change.

To reproduce the same kit on a second SSD, mount it and run the canonical
Studio Hub sync tool from the repository root:

```sh
python3 tools/sync_ssd_bootstrap.py --volume /Volumes/NAME-OF-SECOND-SSD
python3 tools/sync_ssd_bootstrap.py --volume /Volumes/NAME-OF-SECOND-SSD --check
```

Do not copy an older deployed folder over the canonical Git source. The sync
tool preserves SSD models/logs, rebuilds `RELEASE-INVENTORY.sha256`, and removes
retired Tailscale installer packages so Tailscale remains App Store-managed.

## Implementation record

The owner approved the three numbered scripts, the two-choice model manager,
and this durable SSD context on 2026-08-19. The implementation was completed
after that review without installing an application, updating a Studio, copying
a production model, enrolling a Mac, or contacting a live fleet node.

The frozen SSD gate includes Python unit/fixture tests, Python compilation,
zsh syntax checks, three-stage no-write dry runs, manifest checksum validation,
Apple Developer ID and notarization assessment for every installer, Studio Git
origin/cleanliness checks, model-manifest preservation, and an inventory check.
Exact commands and results are recorded in
`terranash-bootstrap/IMPLEMENTATION-REPORT.md`.
