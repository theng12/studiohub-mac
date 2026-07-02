# Studio Hub KH

Control plane for the KH Studio family. One dashboard — and one canonical API — for
**Image Studio (47868)**, **Music Studio (47869)**, **Voice Studio (47870)**,
**Chat Studio (47871)** and **Video Studio (47872)**.

The Hub runs on fixed port **47873** and provides:

- **Live health grid** — up/down, version, latency and last-seen for every studio.
- **Unified model catalog** — every model across all five studios in one searchable
  table (downloaded state, size, minimum unified-memory fit, local vs cloud lane).
  Per-model parameters are passed through verbatim — the Hub never flattens
  model-specific capabilities.
- **Resource monitor** — host unified-memory pressure + per-studio process memory
  (RSS) and CPU, resolved port → PID → process tree.
- **Host-aware registry** — studios on other machines (LAN/Tailscale) can be added
  via `studios.json`, the foundation for multi-machine federation and Swarm Batch.

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
id (`image`, `music`, `voice`, `chat`, `video`) override the local defaults instead.

## API

Base URL: `http://localhost:47873` (or your machine's LAN/Tailscale address).

| Endpoint | Description |
|---|---|
| `GET /api/health` | Hub liveness (same shape as the sibling studios) |
| `GET /api/version` | `{app_version, title}` |
| `GET /api/hub/studios` | Registry + live status per studio |
| `GET /api/hub/health` | Aggregate: totals + per-studio statuses |
| `GET /api/hub/catalog` | Unified model catalog. Query params: `q`, `modality`, `downloaded`, `cloud`, `force` |
| `GET /api/hub/resources` | Host memory/CPU + per-studio process stats |
| `GET /api/hub/summary` | One-shot dashboard payload (studios + resources) |
| `POST /api/hub/studios/{id}/start` | Start a local studio (via Pinokio's `pterm` CLI) |
| `POST /api/hub/studios/{id}/stop` | Stop a local studio |
| `POST /api/hub/registry/reload` | Re-read `studios.json` without restarting |

Lifecycle control works for **local** studios only (pterm talks to this machine's
Pinokio kernel); remote studios are controlled by their own machine's Hub. The
call returns immediately — poll `/api/hub/studios` to watch the status change.
Note the Hub binds on all interfaces like its siblings, so anyone who can reach
port 47873 on your LAN/tailnet can start/stop studios — same trust model as the
studios themselves (token auth arrives with the gateway phase).

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
