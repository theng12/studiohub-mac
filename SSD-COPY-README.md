# TerraNash Studio SSD

The two jobs are separate. Finish step 1 before step 2 on a new Mac.

## NEW MACHINE — two clicks

1. Finish normal macOS setup and connect this SSD.
2. Open `terranash-bootstrap` in Finder.
3. Double-click **1 Install Pinokio and Studios.command**.
4. Keep Pinokio open and finish any first-run setup it shows. Wait for
   **Complete**.
5. Double-click **2 Copy Models to This Mac.command**. Wait for **Complete**.

Step 1 installs Pinokio 8.0.40, Studio Hub, Image Studio, Voice Studio, their
dependencies, and one Pinokio-owned startup graph. It does not start the
Studios or touch model caches.

Step 2 copies complete Image and Voice model packages that fit this Mac's RAM,
plus the shared saved voices used by the fleet. It does not install, update,
start, stop, or prune other model caches. A conflicting local voice is kept.

## EXISTING MACHINE — models are missing

If Pinokio, Hub, Image, and Voice are already installed, skip step 1. Open
`terranash-bootstrap` and double-click **2 Copy Models to This Mac.command**.
Complete packages and matching saved voices already on the Mac are skipped.

## REPAIR

Rerun only the numbered step that failed. Completed work is detected and
skipped. Every run saves a log in `terranash-bootstrap/logs` on this SSD and a
second copy in `~/Library/Logs/TerraNash` on that Mac.

If a model-copy run fails, return the SSD to the main Mac. Do not send unrelated
macOS `.diag` or `.shutdownStall` files.

## JOIN CONTROLLER — later

Both steps keep Hub Standalone. No Tailscale or Controller address is needed.
After installation, start the Studios normally in Pinokio, open
`http://127.0.0.1:47873`, and use Hub's Remote workspace when ready.

## SSD MAINTAINER — main Mac only

```bash
cd ~/pinokio/api/studiohub-mac
git pull
python3 tools/studio_models.py stage --plan
python3 tools/studio_models.py stage
```

Staging is additive. Unchanged packages are not recopied, and older SSD models,
voices, and logs stay in place if one Studio is offline during maintenance.

The SSD must use APFS. If Terminal reports **Operation not permitted**, enable
System Settings → Privacy & Security → Files and Folders → Terminal →
Removable Volumes.
