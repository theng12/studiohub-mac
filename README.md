# Studio Hub KH

Control plane for the KH Studio family. One dashboard — and one canonical API — for
**Image Studio (47868)**, **Music Studio (47869)**, **Voice Studio (47870)**,
**Chat Studio (47871)**, **Video Studio (47872)** and the separate
**Render Studio (47874)** episode-assembly worker.

The Hub runs on fixed port **47873** and provides:

- **Live health grid** — up/down, version, latency and last-seen for every studio.
- **Unified model catalog** — every model across all generative studios in one searchable
  table (downloaded state, size, minimum unified-memory fit, local vs cloud lane).
  Per-model parameters are passed through verbatim — the Hub never flattens
  model-specific capabilities.
- **Resource monitor** — host unified-memory pressure + per-studio process memory
  (RSS) and CPU, resolved port → PID → process tree.
- **Host-aware registry** — studios on other machines (LAN/Tailscale) can be added
  via `studios.json`, the foundation for multi-machine federation and Swarm Batch.
- **Machine-level work leases** — image generation and final rendering take turns
  on each Mac without pausing active work. Waiting render jobs are assigned first,
  with faster M4 16 GB workers preferred when available.

See `SPEC.md` for the full architecture and phased roadmap (gateway, job broker,
Swarm Batch, recipes).

## How to use

1. **Install** — click *Install* in the Pinokio sidebar (creates a small `conda_env`
   with FastAPI/httpx/psutil; no AI bundle needed — the Hub runs no models).
2. **Start** — click *Start*. The dashboard opens at `http://localhost:47873`.
3. **Tabs**: *Overview* (studio cards), *Models* (unified catalog with search and
   filters), *Resources* (host memory bar + per-studio table).
4. The dashboard refreshes every 5 s automatically.

### Adding a studio on another machine

Create `studios.json` in this folder (it's gitignored — per-machine state):

```json
[
  { "id": "image-b", "modality": "image", "host": "100.101.102.103", "port": 47868,
    "machine": "mac-studio", "title": "Image Studio (Mac Studio)" }
]
```

Then `POST /api/hub/registry/reload` (or restart the Hub). Entries with an existing
id (`image`, `music`, `voice`, `chat`, `video`, `render`) override the local defaults instead.

## API

Base URL: `http://localhost:47873` (or your machine's LAN/Tailscale address).

| Endpoint | Description |
|---|---|
| `GET /api/health` | Hub liveness (same shape as the sibling studios) |
| `GET /api/version` | `{app_version, title}` |
| `GET /api/hub/studios` | Registry + live status per studio |
| `GET /api/hub/health` | Aggregate: totals + per-studio statuses |
| `GET /api/hub/catalog` | Raw per-studio catalog rows (annotated `hub_cached`, `hub_machine`). Query: `q`, `modality`, `downloaded`, `cloud`, `force` |
| `GET /api/hub/models` | **Deduped by repo** with per-machine availability (`cached_on`, `machines[]`). Query: `q`, `modality`, `downloaded` |
| `DELETE /api/hub/registry/machines/{machine}` | Unregister a discovered machine's studios |
| `GET /api/hub/fleet` · `POST /api/hub/fleet` | Fleet token status / set (`{token}`) — enables remote specs + control |
| `GET /api/hub/resources?local_only=true` | This machine only (peers call with this to prevent recursion) |
| `GET /api/hub/resources` | Host memory/CPU + per-studio process stats |
| `GET /api/hub/summary` | One-shot dashboard payload (studios + resources) |
| `POST /api/hub/studios/{id}/start` | Start a local studio (via Pinokio's `pterm` CLI) |
| `POST /api/hub/maintenance/preflight` | Check fleet auth, contracts, models, engines, disk, and updates |
| `POST /api/hub/maintenance/updates` | Start a drained, sequential rolling update |
| `GET /api/hub/maintenance/updates/{id}` | Follow rolling-update progress and health verification |
| `POST /api/hub/studios/{id}/stop` | Stop a local studio |
| `GET /api/hub/access` | Shareable LAN/Tailscale URLs (+ the token, loopback only) |
| `ANY /studio/{id}/{path}` | **Gateway** — proxies to that studio's API (streams/SSE included) |
| `POST /api/hub/registry/reload` | Re-read `studios.json` without restarting |
| `GET /api/hub/metrics?minutes=60` | Time-series (host memory/CPU + per-studio RSS, 15s samples, 24h) |
| `GET /api/hub/watchdog` · `POST /api/hub/studios/{id}/watchdog` | Auto-restart-if-down per studio (`{"enabled": true}`; 2-min cooldown, auto-off after 5 failed revives) |
| `POST /api/hub/broadcast/download` | `{repo, studios?}` — start the same model download on many studios |
| `POST /api/hub/broadcast/env` | `{key, value, studios?}` — set an env var in studios' ENVIRONMENT files (restart to apply) |
| `POST /api/hub/jobs` | **Swarm Batch** — submit a batch (envelope below) |
| `GET /api/hub/jobs` · `GET /api/hub/jobs/{batch}` · `DELETE /api/hub/jobs/{batch}` | Track / cancel batches |
| `GET /api/hub/assets` · `POST /api/hub/assets/scan` | Asset ledger (query: `q`, `modality`, `studio`, `batch_id`) |
| `POST /api/hub/assets/upload` | Upload a reference image once → `{asset_id}` (for img2img continuity) |
| `POST /api/hub/render-assets` | Stream an immutable render input; returns path, bytes, and SHA-256 |
| `GET /api/hub/jobs/{batch}/items/{index}/artifact` | Stream a completed worker video through Hub authentication |
| `POST /api/hub/jobs/{batch}/items/{index}/ack` | Confirm the main copy was verified and start worker retention |
| `GET /api/hub/stats[?hours=N]` | Generation analytics: by machine/modality/model + timeline |
| `POST /api/hub/recipes/run` | Run a recipe chain (`{recipe, brief}`) |
| `GET /api/hub/recipes/runs[/{id}]` | Recipe run status |
| `POST /api/hub/director` | `{brief, auto_run?}` — LLM plans a recipe from plain English |

## Client integration (Story Studio KH)

External apps never talk to studios directly — they talk to the Hub:

1. Store two values: the Hub URL (`http://<tailscale-ip>:47873`) and the token.
2. Submit work: `POST /api/hub/jobs` with `label` (your app's name) and,
   ideally, `webhook` — the Hub POSTs the batch summary (incl. per-item
   `artifact_url`) to that URL the moment the batch finishes. No polling.
3. Or poll `GET /api/hub/jobs/{batch_id}` — this survives Hub restarts
   (batches are persisted in `hub.db` and unfinished work resumes
   automatically; in-flight items are re-queued and redone).

```javascript
// Story Studio side
await fetch(`${HUB}/api/hub/jobs`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-Hub-Token": TOKEN },
  body: JSON.stringify({
    modality: "image", model: "flux-schnell-repo",
    label: "storystudio-kh",
    webhook: "http://my-host:PORT/hub-callback",
    items: scenes.map(s => ({ prompt: s.prompt, seed: s.seed }))
  })
});
```

## Dashboard

- **Overview** — group studios **All / By app / By machine**, in **Cards** or **List** view
  (your choice is remembered). Start/stop and auto-restart toggles per studio.
- **Models** — every model across all machines, deduped, with an **Availability** column
  showing exactly which machines have each one downloaded. "Downloaded" means *cached on at
  least one machine* — a model can be on your media server but not this Mac.
- **Resources** — this Mac's unified-memory bar + hour sparkline, per-studio process memory.
- **Jobs / Assets** — Swarm Batch submit + progress; searchable asset ledger.
- **Remote** — reachable URLs + token, **Discover & Add** a machine, and a permanent
  **Registered machines** list (with Remove).

## Run as an always-on service (auto-start)

Instead of clicking **Start** every time, install the Hub as a macOS launchd
service (same as the sibling studios): sidebar → **Install as Startup Service**.

- Starts automatically at login, restarts itself if it crashes, and a watchdog
  re-launches it if it stops answering `/api/health`.
- The service owns port 47873; the sidebar switches to "service mode" (Open
  Dashboard, Check Service Status, Restart, Uninstall) with no Start button —
  use the service **or** Pinokio's Start, not both.
- `Update` restarts the service automatically so new code is picked up.
- For unattended reboot recovery (power cut), do the one-time admin settings the
  installer prints: `sudo pmset -a autorestart 1`, enable Automatic Login, and
  turn FileVault off (a LaunchAgent needs a logged-in session to start).
- Remove it any time with **Uninstall Startup Service** (the app itself is
  untouched; Start still works).

## The fleet: remote specs + remote control

Health and models come over HTTP, but a machine's **RAM/specs** and
**start/stop** need OS-level access on that machine. So run a **Studio Hub on
every Mac** — each Hub is the authority for its own machine, and your primary
Hub aggregates them.

Setup (once):
1. Install + start Studio Hub on each Mac.
2. On the primary, copy the automatically generated token from **Remote → Fleet security token**.
3. Paste that **same token** into every other Mac's Hub (Remote → Fleet token).
   (Tip: it's one value for the whole fleet — keep it somewhere handy.)

Then, for any remote studio, the primary shows the machine's live **host RAM/CPU**
(Resources tab, per-machine cards) and enables **Start/Stop** on the studio card —
proxied to that machine's Hub, which runs `pterm` locally. Each machine's Hub
also watchdogs its own studios. Machines without a Hub (or with the wrong token)
still show health; specs/control read "no Studio Hub running here."

Security: the fleet token is a shared credential accepted by every Hub in
addition to its own local token. Loopback is always exempt. It lives in
`.fleet_token` (gitignored) or `STUDIOHUB_FLEET_TOKEN`.

## Multiple Macs (registry)

Every Mac keeps running its own studios; ONE Hub coordinates them all:

1. On each other Mac, install whichever studios it should serve (2, 3, or 5).
2. On the Hub Mac, open **Remote → Add another Mac's studios**, enter that
   Mac's Tailscale IP and a name. Two ways to add:
   - **Discover & Add** — probes the machine now and registers whatever answers
     (machine must be online). `POST /api/hub/registry/discover {host, machine}`.
   - **Add manually (offline OK)** — pre-registers the *checked* studios without
     probing, so you can set a machine up before it's online; it flips from
     "down" to live automatically when reachable.
     `POST /api/hub/registry/add {host, machine, modalities}`.
3. Remote studios join the health grid, catalog, gateway and **worker pools**
   automatically.

Heterogeneity is handled per-dispatch:
- **Different models per Mac** — a job is only sent to a studio whose own
  catalog shows that model *downloaded*. A one-model Mac only ever receives
  jobs for that model. Distribute models deliberately with
  `POST /api/hub/broadcast/download {"repo": ..., "studios": ["image@mac-b"]}`.
- **Different specs** — pull-based dispatch means fast Macs simply complete
  more items; nothing is statically split.
- **Memory** — the local machine is guarded by the Hub's governor; remote
  studios are paced by their own backends (one concurrent job each).

## Swarm Batch

Submit N independent prompts; the Hub queues them and free studios of that
modality pull the next item (work-stealing — a second machine in `studios.json`
automatically joins the pool). Failed items are retried up to 3 times. Every
result lands in the asset ledger with full provenance (prompt, model, resolved
seed, params, batch id) — reproducible by construction.

```bash
curl -X POST http://localhost:47873/api/hub/jobs \
  -H "Content-Type: application/json" -d '{
  "modality": "voice",
  "model": "hexgrad/Kokoro-82M",
  "items": [{"prompt": "Line one."}, {"prompt": "Line two."}],
  "sharedParams": {}
}'
```

The **memory governor** guards local dispatch: a model whose
`min_unified_memory_gb` exceeds the machine fails fast; one whose size doesn't
fit in currently-free memory waits (visible as `governor_note` on the batch).
Cloud-backed models bypass the governor entirely.

## Recipes & director

A recipe chains steps; `{{brief}}`, `{{prev.text}}`, `{{prev.artifact}}` carry
context forward. Chat steps produce text; generation steps run through the
broker (governor, retries, ledger included).

```bash
curl -X POST http://localhost:47873/api/hub/director \
  -H "Content-Type: application/json" \
  -d '{"brief": "a short spoken welcome message", "auto_run": true}'
```

The director uses your local Chat Studio to write the recipe, validates every
model against the live catalog (with one self-repair retry), and only runs it
when `auto_run` is set.

Lifecycle control works for **local** studios only (pterm talks to this machine's
Pinokio kernel); remote studios are controlled by their own machine's Hub. The
call returns immediately — poll `/api/hub/studios` to watch the status change.

## Remote access & auth

- **Local (this machine)** — no token needed; everything works as before.
- **Remote (LAN / Tailscale)** — every API call requires the Hub token via
  `Authorization: Bearer <token>`, `X-Hub-Token: <token>`, or `?token=`.
  StudioHub also creates an owner-only fleet token automatically and forwards it as
  `X-Studio-Token` to sibling Studio APIs. Local loopback use remains passwordless;
  remote Studio API, OpenAI-compatible, settings, upload, and output routes require it.
  The dashboard page and `/api/health`/`/api/version` stay public; the
  dashboard prompts for the token once and remembers it.
- The token is auto-generated into `.hub_token` (gitignored). See it in the
  dashboard's **Remote** tab (only shown when viewed on the Hub machine).
  Rotate it by deleting the file and restarting the Hub.
- **Control from anywhere:** install Tailscale on your phone/laptop, then open
  the Tailscale URL shown in the Remote tab. Your Mac stays the server; no
  cloud middleman.

## Gateway

One base URL for every studio API — clients store the Hub address instead of
five studio addresses:

```
{HUB}/studio/image/api/catalog          → Image Studio
{HUB}/studio/chat/v1/chat/completions   → Chat Studio (OpenAI-compatible)
{HUB}/studio/video/api/generate/stream  → Video Studio (SSE streams fine)
```

Works for local and remote registry entries alike. Intended for API traffic;
for browsing a studio's web UI, use the dashboard's direct "Open UI" links.

### curl

```bash
# Which studios are up?
curl http://localhost:47873/api/hub/health

# All downloaded local image models
curl "http://localhost:47873/api/hub/catalog?modality=image&downloaded=true&cloud=false"

# Memory picture
curl http://localhost:47873/api/hub/resources

# Start / stop a studio
curl -X POST http://localhost:47873/api/hub/studios/music/start
curl -X POST http://localhost:47873/api/hub/studios/music/stop
```

### JavaScript

```javascript
const HUB = "http://localhost:47873";

// Live status of every studio
const { studios } = await fetch(`${HUB}/api/hub/studios`).then(r => r.json());
const up = studios.filter(s => s.status === "up");

// Search the unified catalog
const { models } = await fetch(`${HUB}/api/hub/catalog?q=flux`).then(r => r.json());
// Each model carries hub_studio / hub_modality / hub_machine annotations;
// everything else is the studio's own catalog entry, verbatim.
```

### Python

```python
import httpx

HUB = "http://localhost:47873"

with httpx.Client() as client:
    health = client.get(f"{HUB}/api/hub/health").json()
    print(f"{health['studios_up']}/{health['studios_total']} studios up")

    resources = client.get(f"{HUB}/api/hub/resources").json()
    print(f"unified memory: {resources['host']['percent']}% used")

    models = client.get(f"{HUB}/api/hub/catalog", params={"downloaded": True}).json()
    for m in models["models"]:
        print(m["hub_studio"], m.get("label"), m.get("size_gb"), "GB")
```

## Files

```
studiohub-mac/
├── app/
│   ├── backend/          # FastAPI app (registry, monitor, resources)
│   ├── frontend/         # Dashboard (single page)
│   └── requirements.txt
├── SPEC.md               # Full architecture + roadmap
├── VERSION
├── install.js / start.js / update.js / reset.js
├── pinokio.js / pinokio.json
└── studios.json          # optional per-machine registry overrides (gitignored)
```

## Troubleshooting

- Launcher logs live in `logs/api/` (use the `latest` file first).
- If a studio shows **down** but its UI works, confirm its port matches the
  registry (defaults: image 47868, music 47869, voice 47870, chat 47871, video 47872).
- If port 47873 clashes on your machine, change it in `start.js` (one line).
