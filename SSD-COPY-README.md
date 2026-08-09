# TerraNash fleet SSD — clean Mac setup

The SSD now provisions a clean Apple-silicon Mac, not just a Mac that already
has the Studios. It carries:

- the signed, checksum-pinned Pinokio 8.0.40 Apple-silicon installer;
- one rerunnable installer for Studio Hub, Image Studio, and Voice Studio;
- the stocked Voice and Image model caches, with Hugging Face symlinks intact;
- a manifest containing each model's measured unified-memory floor.

Python and Conda environments are **not copied between Macs**. They contain
absolute paths and native binaries and are not portable. Each Studio rebuilds
its own environment through its checked-in Pinokio `install.js` and
`install_generation.js` scripts.

## Quick copy-and-paste commands

These commands are available in this file specifically so they can be copied
on a fleet Mac without opening the original setup chat.

### Preview only — change nothing

```bash
"/Volumes/ugreen-terranash/terranash-bootstrap/Install TerraNash Studios.command" --dry-run
```

### Finish an existing Mac and copy its RAM-matched model cache

Use this when Pinokio, Hub, Image, and Voice are already installed. The
installer verifies and skips healthy environments, copies only missing or
damaged RAM-qualified model caches, restarts the workers, and leaves Hub
Standalone:

```bash
"/Volumes/ugreen-terranash/terranash-bootstrap/Install TerraNash Studios.command"
```

### Copy models and join the Controller in the same run

Replace the private Controller address and friendly machine name. The command
asks for the Controller registration code in a hidden prompt after caching:

```bash
"/Volumes/ugreen-terranash/terranash-bootstrap/Install TerraNash Studios.command" \
  --controller http://CONTROLLER-TAILSCALE-IP:47873 \
  --machine-name "FRIENDLY MACHINE NAME"
```

### Preserve every old model cache while installing

The normal fleet policy prunes models outside the stocked selection or above
the Mac's RAM tier. Add `--no-prune` when old caches must be retained:

```bash
"/Volumes/ugreen-terranash/terranash-bootstrap/Install TerraNash Studios.command" \
  --no-prune \
  --controller http://CONTROLLER-TAILSCALE-IP:47873 \
  --machine-name "FRIENDLY MACHINE NAME"
```

On an already-installed Mac, update Hub, Image, and Voice from their Pinokio
**Update** actions first if they might be older. The bootstrap keeps matching
existing Git checkouts; it verifies their environments but does not pull newer
Studio code. If macOS mounted the drive with a suffix such as
`ugreen-terranash 1`, replace the volume name in the command or drag
**Install TerraNash Studios.command** from Finder into Terminal.

## Part 1 — prepare or refresh the SSD once

The volume is named `ugreen-terranash`. The tools also recognize the old
`UGREEN-1TB` name and can find a uniquely mounted `studio-models/MANIFEST.json`,
so the display name is no longer a fragile hard-coded dependency.

On the main Mac, start Image Studio and Voice Studio, then run:

```bash
cd ~/pinokio/api/studiohub-mac && git pull
python3 tools/studio_models.py stage --plan
python3 tools/studio_models.py stage
```

The plan changes nothing. The real stage writes this layout:

```text
ugreen-terranash/
├── .terranash-fleet-ssd.json
├── READ-ME-FIRST.md
├── studio-models/
│   ├── MANIFEST.json
│   ├── image/
│   └── voice/
└── terranash-bootstrap/
    ├── Install TerraNash Studios.command
    ├── fleet_bootstrap.py
    └── installers/
        └── Pinokio-8.0.40-arm64.dmg
```

The Pinokio installer is downloaded only when missing or when its SHA-256 no
longer matches the official release. A verified copy is reused. Packages that
were dropped from the stocked model set are removed from `studio-models` only
after the current replacement copy succeeds; unrelated SSD folders are ignored.

If Terminal reports **Operation not permitted**, enable System Settings →
Privacy & Security → Files and Folders → Terminal → Removable Volumes, then run
the command again. The SSD must remain APFS so model-cache symlinks are
preserved.

## Part 2 — install a new Mac

1. Complete normal macOS setup and log into the account that will run the
   Studios.
2. Connect the Mac to the same LAN or Tailscale network as its Controller.
3. Plug in `ugreen-terranash`.
4. In Finder, open `terranash-bootstrap` and double-click
   **Install TerraNash Studios.command**.
5. Enter the Mac administrator password once when `/Applications/Pinokio.app`
   is installed. If Pinokio shows a first-run window, finish it; the bootstrap
   waits up to 15 minutes and is safe to rerun.

For an Agent that should join an existing Controller immediately, Terminal can
run the same file with the Controller's private address:

```bash
"/Volumes/ugreen-terranash/terranash-bootstrap/Install TerraNash Studios.command" \
  --controller http://CONTROLLER-TAILSCALE-IP:47873 \
  --machine-name FRIENDLY-MACHINE-NAME
```

The Controller registration code is requested in a hidden prompt. It is never
stored on the SSD, printed, placed in a URL, or added to shell history.

## What the installer does, in order

1. Verifies this is an Apple-silicon Mac and reports hostname and unified RAM.
2. Verifies the stocked Pinokio DMG checksum, installs the signed app, and
   verifies its code signature and Gatekeeper assessment using macOS-native
   tools. A factory-fresh Mac does not need Xcode or a preinstalled Python.
3. Adds one user LaunchAgent that opens Pinokio at macOS login. It does not run
   any Studio directly.
4. Waits for Pinokio to initialize its own home and bundled Git, Conda, Python,
   UV, and AI prerequisites. The rest of the bootstrap runs with Pinokio's
   bundled Python rather than assuming `/usr/bin/python3` exists.
5. Uses Pinokio's `pterm download` to install the current released Hub, Image,
   and Voice repositories under the resolved `PINOKIO_HOME/api`—never a guessed
   username or hard-coded home path.
6. Runs each app's real `install.js`. It then runs Image and Voice
   `install_generation.js`, including their own import verification.
7. Starts Image and Voice, asks their live catalogues where their caches live,
   and restores only models whose known memory floor fits this Mac. Complete
   caches are skipped, damaged caches are replaced, and `--prune` removes old
   models that cannot run on the detected RAM tier.
8. Restarts Image and Voice so their catalogues rescan the copied caches.
9. Configures one Pinokio startup graph:

   ```text
   Studio Hub (startup enabled)
   ├── requires Image Studio (startup entry disabled)
   └── requires Voice Studio (startup entry disabled)
   ```

   Pinokio therefore starts both workers, waits for them to become ready, and
   then starts Hub. It does not race three independent startup jobs, and it does
   not install the Studios' separate launchd startup services.
10. Starts Hub. When `--controller` was supplied, Hub detects this Mac's model,
    Apple chip, RAM, and matching reusable hardware profile, securely claims the
    Controller code, becomes an Agent, and registers its Studio endpoints in the
    same transaction.

If `--controller` is omitted, installation still completes and Hub remains
**Standalone**. Rerun the same command later with `--controller`; installed
repos, environments, and complete model caches are detected and skipped.

## Safety and recovery

- Run a no-change preview with:

  ```bash
  "/Volumes/ugreen-terranash/terranash-bootstrap/Install TerraNash Studios.command" --dry-run
  ```

- The installer never copies a Conda environment, fleet token, Hub token,
  enrollment code, database, job history, or machine identity from another Mac.
- It refuses to overwrite an existing app folder belonging to another Git
  repository.
- It keeps every successful step if a later download, install, or enrollment
  step fails. Fix the named issue and run it again. Every run also leaves a
  timestamped log under `terranash-bootstrap/logs/`.
- Use `--no-prune` if this is not a disposable/test machine and old model caches
  must be preserved for inspection.
- If the Controller reports no matching hardware profile for a brand-new Apple
  chip/RAM combination, add that reusable profile once on the Controller, update
  Hub, and rerun only the enrollment step.

## What is deliberately still manual

The installer does not disable FileVault, enable automatic login, sign into an
Apple ID, or change power-loss recovery settings. Those are machine-owner and
security choices. It also does not bundle Controller credentials on a removable
drive. Pinokio and Studio code/dependencies still need network access on their
first install; the large model downloads and Pinokio application download are
the parts stocked on the SSD.
