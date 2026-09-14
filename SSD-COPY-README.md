# TerraNash Studio SSD

The canonical kit source is [`ssd_bootstrap/`](ssd_bootstrap/). The mounted SSD
is a generated deployment copy; do not maintain a second hand-edited version.

## New Mac

Open `terranash-bootstrap` on the SSD and run the numbered commands in order:

1. **Install Mac Apps** installs Pinokio, Yam Display, and Latest. Install
   Tailscale separately from the Mac App Store.
2. Finish Pinokio's visible **Install Tools**, then **Install Studios**.
3. **Manage AI Models** and choose the normal restore action. On an 8 GB Mac,
   Voice restores Qwen3-TTS 0.6B Base, its Whisper quality checker, and Kokoro.
4. Use **Repair Studio Startup** only if an existing Mac has competing Pinokio
   and launchd ownership or stale cross-Studio requirements.

Each command is restartable and preserves completed work. The full owner and
future-assistant handoff is
[`ssd_bootstrap/root/START HERE - TerraNash Mac Setup.md`](ssd_bootstrap/root/START%20HERE%20-%20TerraNash%20Mac%20Setup.md).

## Refresh the mounted SSD

First ensure `terranash-bootstrap/installers/` contains the DMGs named in
`ssd_bootstrap/kit/installers/MANIFEST.json`, with matching SHA-256 checksums.
Older SSDs need the verified Pinokio 8.2.0 ARM64 installer copied in before
sync; the sync tool does not download installers.

From a current Studio Hub checkout:

```bash
python3 tools/sync_ssd_bootstrap.py --volume /Volumes/ugreen-terranash
python3 tools/sync_ssd_bootstrap.py --volume /Volumes/ugreen-terranash --check
```

The sync copies only tracked kit sources, removes explicitly retired kit files,
and regenerates `RELEASE-INVENTORY.sha256`. It preserves model packages,
remaining installer DMGs, logs, backups, and unrelated files.

Model-library refresh remains a separate owner action through **3 Manage AI
Models**. It is additive and does not automatically download every model.
After refreshing the model library, run the sync and check again so
`RELEASE-INVENTORY.sha256` includes the updated model manifest.
