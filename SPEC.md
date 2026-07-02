# Studio Hub KH — SPEC

> Control plane, data plane, and intelligence plane for the KH Studio family
> (Image / Chat / Voice / Music / Video Studio) running on Apple Silicon via Pinokio.

- **Status:** Draft v0.1 (planning)
- **Launcher folder:** `PINOKIO_HOME/api/studiohub-mac`
- **PINOKIO_HOME:** resolved from `~/.pinokio/config.json` → `home`
- **Type:** App launcher (FastAPI backend + web dashboard), same scaffolding as the sibling studios.
- **Proposed port:** `47873` (fixed; next after Video Studio's `47872`, no clash), host `0.0.0.0`.

---

## 1. Why this exists

Today the five Studio apps are fully decoupled — each is an independent FastAPI server on a
fixed port, individually launched and monitored through Pinokio. That is great for isolation
but means there is:

- No single place to see health, memory, or models across all five.
- No coordination of **unified memory** — the real bottleneck on Apple Silicon, since all
  studios share one RAM pool.
- No single endpoint for a client (e.g. **Story Studio KH**) to talk to — it would otherwise
  hardcode five IPs/ports.
- No way to spread one big batch of work across multiple machines.

**Studio Hub KH** is the missing layer that makes the family *coherent and work together*.
Its outward API is designed to be **the canonical contract** that clients (Story Studio KH
first, others later) converge on — so clients target one Hub instead of five studios.

## 2. The five studios (managed targets)

All are FastAPI (uvicorn), host `0.0.0.0`, `daemon: true`, fixed ports, and share a consistent
API surface. Launch URL is captured in each `start.js` via the regex
`/Uvicorn running on (http:\/\/[0-9.:]+)/` → `local.set` `url`.

| Studio | Port | Notable endpoints beyond the shared set |
|---|---|---|
| Image Studio KH | 47868 | `/api/imports/scan`, `/api/imports` |
| Music Studio KH | 47869 | `/api/settings` |
| Voice Studio KH | 47870 | `/api/imports/scan`, `/api/imports` |
| Chat Studio KH  | 47871 | OpenAI-compatible `/v1/models`, `/v1/chat/completions` |
| Video Studio KH | 47872 | async jobs: `/api/generate/jobs`, `/api/generate/jobs/{id}`, `/api/generate/stream` (SSE) |

**Shared surface across all five** (the foundation the Hub leans on):

- `GET /api/health` — liveness
- `GET /api/version` — running version (backed by a root `VERSION` file)
- `GET /api/catalog` — `{ models, families }` with cache/download state
- `GET /api/generate/diagnostics` — runtime/resource diagnostics
- `GET|POST|DELETE /api/downloads`, `GET /api/downloads/stream` (SSE) — model management

Each studio installs a conda env at `./conda_env`, uses an HF cache at `./cache/HF_HOME`, and
persists settings in `app/backend/settings.json` and an `ENVIRONMENT` file.

## 3. Architecture: three planes

Studio Hub is one app conceptually split into three planes, built up gradually.

### 3.1 Control plane — *see & steer*
Observe and manage the studios. Read-mostly, lowest risk, built first.

### 3.2 Data plane — *work flows through the Hub*
The Hub becomes the single address clients talk to: gateway routing, config/model
broadcasting, unified assets, and the job broker.

### 3.3 Intelligence plane — *the Hub decides*
The Hub schedules, orchestrates pipelines, and (eventually) accepts natural-language direction.

```
                        ┌───────────────────────────────────────┐
      clients  ────────▶│              STUDIO HUB KH             │
  (Story Studio KH,     │  ┌──────────┬──────────┬───────────┐  │
   scripts, LLMs)       │  │ Control  │  Data    │ Intellig. │  │
                        │  │ plane    │  plane   │ plane     │  │
                        │  └────┬─────┴────┬─────┴─────┬─────┘  │
                        └───────┼──────────┼───────────┼────────┘
                    health/RAM  │  gateway/ │  scheduler/
                    catalog     │  jobs/    │  recipes/
                                │  assets   │  director
             ┌──────────┬───────┴───┬───────┴───┬──────────┐
        ┌────▼───┐ ┌────▼───┐ ┌────▼───┐  ┌────▼───┐  ┌────▼───┐
        │ Image  │ │ Music  │ │ Voice  │  │ Chat   │  │ Video  │
        │ 47868  │ │ 47869  │ │ 47870  │  │ 47871  │  │ 47872  │
        └────────┘ └────────┘ └────────┘  └────────┘  └────────┘
          (one or many machines, addressed over LAN / Tailscale)
```

## 4. Capability menu (tiered)

Legend: ⭐ high-value + uniquely enabled by this setup · ○ strong · ◦ nice-to-have.

### Control plane
- ⭐ **Unified-memory governor** — Knows total unified RAM + each model's footprint + what is
  currently loaded across *all* studios. Acts as admission control: refuses/queues a job that
  would not fit, auto-unloads idle models, and advises "unload X to run Y." Only something that
  sees all five can do this. **Scope: local models only** — cloud-backed models consume ~zero
  local RAM and are bounded by API rate limits (see credential pool), not the memory governor.
- ○ **Time-series metrics** — RAM/CPU/throughput graphed over time per studio (not just a live
  snapshot); surfaces thermal throttling and memory creep.
- ○ **Watchdog + auto-restart** — Detect a crashed/unresponsive studio and restart it (builds on
  each studio's existing launchd/service scaffolding).
- ◦ **Log aggregation** — Tail all studios' logs in one pane.

### Data plane
- ⭐ **Unified asset ledger** — Every generation (image/clip/voice/track/text) recorded in one
  searchable, taggable library with full **reproducibility**: prompt + model + version + seed +
  params stored, enabling "regenerate" and "remix with variation." Cross-modal, which no single
  studio offers. (Ownership model: see §7 decision.)
- ⭐ **Config / model broadcaster** — Push one setting (HF token, provider keys) or one model
  download to *all* studios at once, instead of editing five `ENVIRONMENT` files. Snapshot and
  restore studio configs.
- ⭐ **Credential pool / key rotation** — For cloud-backed models, manage multiple accounts per
  provider (Pollinations, OpenRouter, NVIDIA, Groq, …) and rotate keys to maximize free-tier
  throughput and avoid rate limits. Since cloud models don't touch local unified memory, this
  (not the memory governor) is what lets cloud-backed Swarm Batch scale — concurrency is capped
  by available keys/rate limits, not RAM.
- ○ **Unified gateway + auth** — One base URL that reverse-proxies to the correct studio
  (`/image/*`→47868, `/chat/*`→47871, …). Optional per-client token auth + rate limiting so the
  Hub can be safely exposed over Tailscale. Includes a "share this studio" flow (auto-detect
  Tailscale IP, generate link/QR).
- ○ **Update orchestration** — Compare all five running versions against upstream and update with
  one click (each has `update.js` + `/api/version`).
- ◦ **Webhooks** — Notify external services / Story Studio when jobs complete.

### Intelligence plane
- ⭐ **Pipeline / recipe engine** — Chain studios into a DAG (brief → Chat expands → Image →
  Video animates → Voice narrates → Music scores → mux). Save chains as reusable **recipes**; the
  Hub holds intermediate assets and passes each output to the next. Story Studio KH becomes a thin
  client that triggers a recipe.
- ⭐ **Agentic "director"** — Point an LLM (via Chat Studio's OpenAI-compatible API) at the Hub's
  own API so a plain-English brief ("30s narrated lighthouse video with ambient music") is turned
  into model choices, per-studio prompts, and a sequenced job plan.
- ○ **Smart scheduler** — Queue jobs across studios respecting the memory governor; run heavy
  jobs overnight; predictive preloading (load the next model while the current job renders).

## 5. ⭐ North star: Swarm Batch (distributed data-parallel generation)

The crown capability where federation + memory governor + broadcaster + job broker pay off at
once. Image (and any single-modality) generation is **embarrassingly parallel** — each prompt is
independent — so a batch can be spread across every machine that has the model loaded, for
near-linear speedup.

**Example — 300 images:**
1. Broadcast: download the chosen image model to all participating machines.
2. `POST /batch { modality: "image", model, prompts: [300], seedBase, sharedParams }`.
3. The Hub holds the 300 as a **work queue** and dispatches **pull-based** (work-stealing):
   each machine grabs the next prompt when a worker slot frees up.
4. Results stream back into the **asset ledger**, tagged by prompt index + seed, downloadable as
   a set.

**Why pull-based, not a static split:** machines are heterogeneous (M1 vs M3 Max). A static
1–100 / 101–200 / 201–300 split leaves fast machines idle. Work-stealing means fast machines do
more, everyone finishes together, and a machine that dies mid-job has its prompt requeued
automatically (fault tolerant).

**Generalizes:** TTS-line batches swarm the voice machines, music-clip batches swarm the music
machines, and a **mixed queue** routes each job to the correct *worker pool* by modality.

**Design dependency (confirm before building):** whether each studio's *generate* endpoint is
synchronous (`POST` returns the artifact) or async (submit → poll/SSE, like Video Studio). Async
scales far better for batch; any sync-only studio should gain an async job endpoint before the
swarm scales widely.

## 6. Data model & contracts (canonical interface)

The Hub's schemas are versioned and treated as the stable contract clients converge on.

### 6.1 Studio registry — **host-aware from day one**
Not just a port list. Each entry is an addressable instance so a future second machine / farm
works with no redesign:
```jsonc
{
  "studios": [
    { "id": "image",  "modality": "image", "host": "127.0.0.1", "port": 47868, "machine": "local" },
    { "id": "chat",   "modality": "chat",  "host": "127.0.0.1", "port": 47871, "machine": "local" }
    // future: { "id": "image-b", "modality": "image", "host": "100.101.102.103", "port": 47868, "machine": "studio-2" }
  ]
}
```
- Registry is editable/config-driven so adding a studio or a machine is one entry.
- `machine` + `host` enable federation and Swarm Batch worker pools.

### 6.2 Job envelope — batch is first-class, params are pass-through
Even before the broker is built, the shape anticipates N-way batches:
```jsonc
{
  "modality": "image",
  "model": "flux-schnell",
  "items": [ { "prompt": "...", "seed": 123, "params": {} } ],  // 1..N; params opaque per model
  "routing": "pool",          // "pool" | "studio:<id>" | "auto"
  "sharedParams": {}          // opaque; merged into each item then forwarded verbatim
}
```
**Models are NOT unified.** Each model exposes different knobs, so the Hub unifies only the
*coordination* fields (`modality`, `model`, `prompt`, `seed`, `routing`). Everything model-
specific lives in `params`/`sharedParams` as an **opaque pass-through blob** the Hub forwards
untouched to the target studio. The Hub surfaces each model's own parameter schema/capabilities
**from that studio's `/api/catalog`** rather than imposing a lowest-common-denominator schema.

### 6.3 Asset ledger record
```jsonc
{
  "id": "...", "modality": "image", "createdAt": "...",
  "sourceStudio": "image", "machine": "local",
  "model": "flux-schnell", "modelVersion": "...", "seed": 123,
  "prompt": "...", "params": {},
  "artifact": { "kind": "file|link", "path_or_url": "..." },
  "batchId": "...", "index": 7,
  "provenance": { "parentAssetId": null, "recipeId": null }
}
```

## 7. Key architectural decisions

- **Asset ownership — DECIDED: index + link, never copy.** The Hub maintains the central,
  reproducible asset library by **indexing and linking** to artifacts that stay in each studio's
  own output folder. No duplication of large image/video/audio files. The ledger stores metadata
  + a link/path to the source artifact; it does not hold copies.
- **Model params — DECIDED: opaque pass-through, not unified.** See §6.2. The Hub never
  normalizes per-model params; it forwards them verbatim and reads per-model schemas from each
  studio's `/api/catalog`.
- **Cloud vs local models — DECIDED: two lanes.** Local models are governed by the unified-memory
  governor (RAM-bound). Cloud-backed models bypass the memory governor entirely and are governed
  by the credential pool (rate-limit-bound). Swarm Batch treats them as separate worker pools.
- **Registry host-aware from day one** — even though Phase 1 is single-machine, so federation and
  Swarm Batch need no rework later.
- **Lifecycle control mechanism** — user wants full start/stop control. Pinokio provides
  `script.start` / `script.stop` (kernel RPC methods) whose `uri` can target another app's
  `start.js`, so cross-app control is supported *in principle*. The **unverified** part: these are
  kernel-side RPCs, but the Hub's start/stop button lives in a web page → FastAPI backend (a
  plain process, not a Pinokio script). So the backend must reach the Pinokio kernel (HTTP server
  at `127.0.0.1:42000`, also `100.101.102.103:42000` over Tailscale). Whether the kernel exposes a
  documented HTTP endpoint to trigger a script-run from outside must be confirmed before Phase 2;
  fallback is Hub-shipped wrapper scripts bridged through the kernel. **Do not assume.** Monitoring
  ships first regardless.
- **Exposure** — default bind matches siblings (`0.0.0.0`) for Tailscale-readiness; token auth is
  added at the gateway step, not before, and only when the user chooses to expose the Hub.

## 8. Phased roadmap

| Phase | Plane | Deliverable |
|---|---|---|
| **1 (now)** | Control | Monitoring dashboard: host-aware registry, health grid, aggregated catalog, host + per-studio memory (psutil), read-only memory-governor foundation. |
| **2** | Control | Lifecycle control (start/stop/restart) once the Pinokio control API is verified; watchdog/auto-restart; time-series metrics. |
| **3** | Data | Unified gateway + token auth + Tailscale share; config/model broadcaster; asset ledger (index/link). |
| **4** | Data/Intel | Job broker (single + batch envelope); **Swarm Batch** across federated machines. |
| **5** | Intelligence | Recipe/pipeline engine; then agentic director; smart scheduler. |

Story Studio KH is retrofitted to consume the Hub's canonical API as those phases land.

## 9. Phase 1 scope (build target)

**Backend (FastAPI on `47873`):**
- Config-driven **host-aware registry** (seeded with the five local studios).
- Poller hitting each `/api/health` + `/api/version` → live status, version, last-seen.
- Aggregator merging all `/api/catalog` → unified searchable model table (downloaded vs
  available, cache size, RAM-fit hint).
- Resource monitor: host RAM + memory pressure via `psutil`; per-studio memory by matching the
  listening port → PID → process RSS/CPU%. (True per-app VRAM deferred — unified memory needs
  elevated tooling; show system pressure for now.)
- Hub API (canonical, versioned): `GET /api/hub/studios`, `/api/hub/health`,
  `/api/hub/catalog`, `/api/hub/resources`, `/api/hub/version`.

**Frontend:** single auto-refreshing dashboard — status grid, models tab, resources tab.

**Launcher files (mirroring the sibling studios' patterns):**
`install.js` (conda env + `uvicorn`, `fastapi`, `httpx`, `psutil`), `start.js` (uvicorn launch,
capture `Uvicorn running on …` URL, `local.set` `url`), `reset.js`, `update.js`, `pinokio.js`,
`pinokio.json`, `README.md` (JS / Python / curl API docs), `.gitignore` (conda_env, cache,
dynamic state).

**Explicitly deferred:** gateway, broadcaster, job broker, Swarm Batch, recipes, director,
lifecycle control — foundations only in Phase 1 (host-aware registry + batch-ready job shape).

## 10. Non-goals (for now)
- Not a replacement for the studios' own UIs — the Hub coordinates, it does not re-implement
  per-studio generation UIs.
- No Docker — native cross-machine over LAN/Tailscale, consistent with the family.
- No cloud dependency for the Hub's *own* control-plane function (health, memory, catalog,
  routing all work fully offline). Studios themselves may still use cloud-backed models freely —
  those are managed via the credential pool, not the memory governor.

## 11. Decisions & open questions

**Decided:**
- Asset ledger: **index/link, never copy** (§7).
- Model params: **opaque pass-through, not unified**; per-model schema from `/api/catalog` (§6.2).
- Cloud vs local: **two lanes** — local = memory governor, cloud = credential pool (§7).

**Resolved by code inspection (2026-07-02):**
- **Generate endpoints are ALREADY async** in all four generation studios: each exposes
  `POST /api/generate/<mode>` → `GET /api/generate/jobs`, `/api/generate/jobs/{id}`,
  `/api/generate/jobs/{id}/<artifact>`, and SSE `/api/generate/stream`. Chat is sync/streaming
  (correct for chat). **Swarm Batch's async dependency is satisfied — no studio changes needed.**
- **Catalog is richer than assumed**: models carry `size_gb`, `min_unified_memory_gb`, `fit`,
  `is_cloud`, `cloud_provider`, `cache` (downloaded state), `capabilities` — so RAM-fit display
  and the local/cloud two-lane split are Phase-1-implementable with zero studio changes.
- **Health payloads** are `{ok, version, app_version, hf_home, ...}`; Chat additionally reports
  `loaded_model`, `idle_seconds`, `auto_unload` — free memory-governor telemetry.
- **Port 47873 verified free** on this machine.
- **Registry storage**: defaults for the 5 local studios are baked into the Hub backend; an
  optional per-machine `studios.json` at the launcher root overrides/extends them (gitignored —
  it is machine state, like the siblings' `settings.json`).
- **Install**: the Hub runs no local models → **no `requires: {bundle: "ai"}`**; light conda env
  (`fastapi`, `uvicorn`, `httpx`, `psutil`), mirroring the siblings' `conda_env` convention.
- **Self-consistency**: the Hub exposes its own `/api/health` + `/api/version` in the same shape
  as the siblings, so the Hub itself is monitorable by the same convention (federation-friendly).

**Still open (not blocking Phase 1):**
1. Confirm whether the **Pinokio kernel** (`:42000`) exposes an HTTP endpoint to trigger
   script start/stop from the Hub backend — gating for Phase 2 lifecycle control (§7).
2. When exposing over Tailscale: auth model (single shared token vs per-client keys). Deferred to
   the gateway phase; default local-first until then.
