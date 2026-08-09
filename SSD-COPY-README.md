# TerraNash Studio SSD

Choose the section that matches this Mac. Do not run the **SSD Maintainer**
commands on a new or existing fleet Mac.

## NEW MACHINE — install everything

1. Finish normal macOS setup and connect the SSD.
2. Open the SSD in Finder.
3. Open `terranash-bootstrap`.
4. Double-click **Install TerraNash Studios.command**.
5. Enter the Mac administrator password if asked.
6. Keep Pinokio open and finish any setup it shows. The installer waits for it.
7. Wait for **Complete**. Then open `http://127.0.0.1:47873`.

This installs Pinokio, Studio Hub, Image Studio, Voice Studio, their generation
dependencies, and only the SSD models that fit this Mac's RAM. Hub stays
Standalone; no Controller or Tailscale address is required yet.

## EXISTING MACHINE — models are missing

1. Connect the SSD.
2. Open `terranash-bootstrap` in Finder.
3. Double-click **Install TerraNash Studios.command**.
4. Wait for **Complete**.

Healthy installations and complete model caches are skipped. Missing or damaged
RAM-qualified models are copied, then Image and Voice restart.

## JOIN CONTROLLER — do this later

Only use this after the Mac can reach the Controller through Tailscale or the
private LAN. Replace the address and machine name:

```bash
"/Volumes/ugreen-terranash/terranash-bootstrap/Install TerraNash Studios.command" \
  --controller http://CONTROLLER-ADDRESS:47873 \
  --machine-name "FRIENDLY MACHINE NAME"
```

Enter the Controller registration code when prompted. Do not use `localhost`
as the Controller address: on this Mac, localhost means this Mac itself.

## REPAIR — an earlier run failed

1. Open Pinokio and finish any visible first-run setup.
2. Run **Install TerraNash Studios.command** again.
3. Completed steps are kept and skipped automatically.
4. If it fails again, open the newest file in
   `~/Library/Logs/TerraNash` and send that file for diagnosis.

If macOS mounted the SSD as `ugreen-terranash 1`, double-clicking from Finder
still works. For a Terminal command, drag **Install TerraNash Studios.command**
from Finder into Terminal instead of typing its path.

The normal fleet setup removes old models that are not stocked or cannot run on
this Mac. To keep them, run the installer from Terminal with `--no-prune`.

## MODELS DID NOT COPY — find the correct log

Do not send macOS `.diag` or `.shutdownStall` files. In Terminal, paste:

```bash
find "$HOME/Library/Logs/TerraNash" /private/tmp /Volumes -maxdepth 4 -type f \( -name 'terranash-bootstrap-*.log' -o -name 'bootstrap-*.log' \) -mmin -120 -print 2>/dev/null
```

Send the newest path it prints. If it prints nothing, rerun **Install TerraNash
Studios.command**, keep its Terminal window open, and send everything from
**Start workers and restore RAM-matched models** through the final `Log:` line.

## SSD MAINTAINER — main Mac only

Use these commands only on the main Mac that owns the Studio Hub repository:

```bash
cd ~/pinokio/api/studiohub-mac
git pull
python3 tools/studio_models.py stage --plan
python3 tools/studio_models.py stage
```

The SSD must use APFS so Hugging Face model symlinks are preserved. If Terminal
reports **Operation not permitted**, enable System Settings → Privacy &
Security → Files and Folders → Terminal → Removable Volumes.
