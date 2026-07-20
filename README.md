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
  (RSS) and CPU, resolved port → PID → process tree. It also watches Pinokio's
  Caddy proxy for abnormal memory/file-descriptor growth caused by port conflicts.
- **Fleet model-memory control** — keep models loaded for speed by default, or
  opt individual local/remote Studios into 10-minute, 2-minute, or immediate
  idle release. A manual release button unloads idle models without stopping an
  app or interrupting active work.
- **Cloud audio readiness** — Voice Studio cards show which provider gateways are
  configured and live on each machine without exposing provider credentials.
- **Central ElevenLabs gateway** — cloud ElevenLabs batches always use Voice
  Studio on the main Hub Mac, where the named account pool, quotas, per-account
  voice IDs, and safe paid-call recovery live. Remote Voice Studios need no
  ElevenLabs keys and remain available for local TTS models.
- **Shared voice library** — upload and transcribe one cloning reference in Hub,
  review the words, then synchronize the same stable ID, audio hash, and
  transcript to every Voice Studio Mac. Offline machines catch up automatically.
- **Host-aware registry** — studios on other machines (LAN/Tailscale) can be added
  with a reusable hardware profile and stable machine ID. The selected profile is
  published with live resources for routing and GenStudio operating-cost records.
- **Site-controller boundary** — the same Hub release can run as a standalone
  Hub, location controller, or agent. GenStudio owns customer jobs, attempts,
  billing, global retries, fencing, and cross-location routing. Optional
  PostgreSQL shadow mode publishes operational evidence only; SQLite remains
  permanently authoritative for Studio Hub's site-local scheduler.
- **Private site-capability contract** — GenStudio can authenticate with the Hub
  or fleet token and read one schema-versioned snapshot of machines, hardware,
  workers, models, controls, limits, revisions, and truthful current capacity.
- **Machine-level work leases** — image generation and final rendering take turns
  on each Mac without pausing active work. Waiting render jobs are assigned first,
  with faster M4 16 GB workers preferred when available.
- **Fleet local-backup protection** — each Mac automatically keeps disposable
  generated output within one combined 80 GB budget and clears completed files
  after three days. The main Hub can save or run the policy across all reachable
  peer Hubs without touching active jobs, source/reference uploads, shared voices,
  models, chat history, credentials, or results still awaiting delivery.

See `SPEC.md` for the full architecture and phased roadmap (gateway, job broker,
Swarm Batch, recipes).

## How to use

1. **Install** — click *Install* in the Pinokio sidebar (creates a small `conda_env`
   with FastAPI/httpx/psutil; no AI bundle needed — the Hub runs no models).
2. **Start** — click *Start*. The dashboard opens at `http://localhost:47873`.
3. **Tabs**: *Overview* (studio cards), *Models* (unified catalog with search and
   filters), *Voices* (transcribe and share cloning references), and *Resources*
   (host memory bar + per-studio table).
4. The dashboard updates continuously over SSE, falls back to 5-second polling
   if the stream drops, and reconnects automatically with bounded backoff.

### Control model memory

Open **Memory** in Studio Hub. Every registered Image, Chat, Video, Music, and
Voice Studio appears separately, including Studios reached through a peer Hub.
Select the workers you want and choose:

- **Performance (default)** — preserve loaded models for the fastest repeat
  generation. Nothing unloads automatically.
- **Balanced** — release model and accelerator caches after 10 idle minutes.
- **Memory Saver** — release after 2 idle minutes.
- **Immediate** — release as soon as current work is finished.

**Release selected now** is the manual equivalent. A Studio with queued or
running work refuses safely; other selected Studios still complete. Offline
workers and older versions are shown explicitly, so you can update or reconnect
only those workers and retry. Policies are persisted by each Studio, not the
Hub, and therefore survive Hub restarts and continue working when a remote Hub
is temporarily unavailable.

After dependency installation and the next Studio restart, macOS Activity
Monitor shows `Image Studio Mac`, `Chat Studio Mac`, `Video Studio Mac`,
`Music Studio Mac`, `Voice Studio Mac`, and `Studio Hub Mac` instead of a generic
Python title. The Python process remains the app's backend; the friendly name
only changes how that same process is presented.

### Manage local generated backups

Open **Jobs → Fleet local-backup protection**. The default is enabled, keeps
completed disposable files for three days, and applies one combined 80 GB limit
to each physical Mac rather than giving every Studio a separate 80 GB allowance.
Use **Save to fleet** after changing either value. **Check & clean now** performs
the retention sweep immediately, then removes the oldest eligible files until
each reachable Mac is under its combined limit.

Each peer Hub repeats the same local check hourly, so enforcement does not depend
on the main dashboard staying open. Offline Macs retain their last saved policy
and self-heal locally; the main Hub shows nodes that could not be contacted. Each
Studio also exposes the same policy in its own interface for per-app inspection
and manual cleanup. Protected or active data is never forced out merely to make
the usage bar green, so a Mac can remain visibly over limit when its excess data
is not safe to delete.

### Add a shared cloning voice

1. Open **Voices**, choose the short reference recording, name, language, voice
   type, and usage rights.
2. Pick a ready Whisper model and click **Transcribe in Hub**. Hub uses the same
   durable fleet transcription queue as episode work. Review or correct the
   editable transcript.
3. Confirm permission and click **Save & sync to all Macs**. The card shows every
   machine separately. Offline or restarting Macs remain pending and retry every
   30 seconds; **Sync again** is also available.

Use **Rename** on a card to change the display name without changing its stable
voice ID, reference audio, provider mappings, or existing project references.
The metadata update synchronizes to every Mac, including a fresh pass when a
rename happens during an active sync. **Delete** removes the Hub master audio
and only hash-matching Hub-managed copies on Voice Studio workers. A tiny
deletion tombstone is retained so offline and later-returning Macs remove the
voice automatically; unrelated machine-local voices are never deleted.

Studio Hub stores the canonical files under its ignored `shared_voices/` state
folder. Existing Voice Studio library entries are left untouched. New workers
need Voice Studio v1.19.0 or later to accept the authenticated stable-ID sync.

## Automatic updates (optional)

Open **Updates** and choose Off (the default), Notify only, or Download and
install automatically for this Hub. Checks can run daily or weekly at the
selected maintenance hour. Saving reports success only after the short-lived
LaunchAgent is actually validated; switching Off unloads it immediately.

Keep **Update only while idle** enabled. Active generation, Chat, transcription,
fleet leases, and rolling maintenance defer installation without cancelling
work. **Update after current work** creates a one-time retry even if the regular
mode is Off. Installed/latest versions, last/next checks, live state, the exact
defer or failure reason, release notes, and Retry are shown in the same card.

Studio Hub checks every app's canonical GitHub `VERSION` file once per minute,
independently of the registered app's own scheduled updater. The visible
Updates and Remote views refresh automatically, retain last-known release
versions through temporary GitHub failures, and never use an older worker cache
as the fleet target. You can change every app independently, **Check all**,
update one app, or **Update idle apps**. Fleet updates run one at a time,
reconnect through the expected restart connection drop, and require the updated
app to reach the published version and answer healthy before the next one starts.
Render Studio participates in the same inventory and controls, including a
shortcut to its local automatic-update card.
If an update command never starts a restart, the operation fails visibly after
three minutes instead of looking busy for the entire 20-minute recovery window;
once a restart really begins, the longer window remains available for slow Macs.
Busy apps receive a durable update-after-current-work request on their own Mac;
rolling progress survives a Hub restart, transient connections retry with visible
attempt counts, and a failed subset can be retried centrally without selecting it
again.
Remote uses the same simple version controls for Studios and agent Hubs: rescan,
compare running with latest, update everything ready, or update one row. Studio
app tabs focus the action on one family; **All apps** targets the fleet. Slow
agent Hubs get four bounded connection attempts before they are reported offline,
and remote operation history remains visible across a primary-Hub restart.
The agent-Hub table also reports each Mac's Apple chip and unified RAM and can
be sorted by availability, machine name, chip generation, or RAM in either
direction. The selected order is remembered in that browser.

Every app independently enforces its expected GitHub origin and `main`, a clean
fast-forward, free disk, dependency/import checks, healthy restart, and exact
running version. Dirty, detached, divergent, or rewritten repositories are
refused without changing files. Failure makes one bounded rollback attempt;
rotating redacted logs are under `logs/auto_update/` in that app.

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
| `GET /api/auto-update/status` · `GET /api/auto-update/readiness` | Hub updater settings/state and idle blockers |
| `POST /api/auto-update/settings` · `POST /api/auto-update/check` | Save and validate the opt-in schedule / check safely now |
| `POST /api/auto-update/update` · `POST /api/auto-update/retry` | Update now or after current work / retry a failed update |
| `GET /api/hub/auto-updates` · `POST /api/hub/auto-updates/check-all` | Fleet automatic-update inventory / ask every app to check |
| `POST /api/hub/auto-updates/{target}/mode` | Change one app's Off, Notify, or Auto mode while preserving its schedule |
| `POST /api/hub/auto-updates/update-idle` | Start a staggered, health-gated update for selected idle sibling Studios |
| `POST /api/hub/auto-updates/jobs/{id}/retry` | Retry only the failed apps from a saved automatic fleet update |
| `GET /api/hub/studios` | Registry + live status per studio |
| `GET /health/live` · `GET /health/ready` · `GET /health/capacity` | Controller liveness, site-execution readiness, and non-secret routing capacity; optional telemetry never gates readiness |
| `GET /api/hub/capabilities` | Private schema-versioned GenStudio capability snapshot; requires a Hub/fleet token header even on loopback |
| `GET /api/hub/controller` · `PUT /api/hub/controller` | Read or configure this Hub's `standalone`, `controller`, or `agent` role, site identity, and optional evidence-shadow mode |
| `POST /api/hub/controller/check` | Verify the optional PostgreSQL evidence schema and publish an immediate heartbeat |
| `POST /api/hub/registry/studios/{id}/enabled` | Pause/resume new jobs for one Studio with `{"enabled": false/true}`; running work and the process are untouched |
| `GET /api/hub/health` | Aggregate: totals + per-studio statuses |
| `GET /api/hub/catalog` | Raw per-studio catalog rows (annotated `hub_cached`, `hub_machine`). Query: `q`, `modality`, `downloaded`, `cloud`, `force` |
| `GET /api/hub/models` | **Deduped by repo** with per-machine availability (`cached_on`, `machines[]`). Query: `q`, `modality`, `downloaded` |
| `GET /api/hub/transcription` | Fleet-wide Whisper inventory with `cached_on`, `available_on`, ready counts, and recommended default |
| `GET /api/hub/shared-voices` | List Hub-owned cloning references plus pending per-machine deletions |
| `POST /api/hub/shared-voices/transcribe` | Transcribe one multipart reference clip through the existing fleet queue and return editable plain text |
| `POST /api/hub/shared-voices` | Save one multipart reference + reviewed transcript and begin synchronization to all Voice Studio Macs |
| `PATCH /api/hub/shared-voices/{id}` · `POST /api/hub/shared-voices/{id}/sync` | Correct shared metadata/transcript and resynchronize / manually retry all targets |
| `DELETE /api/hub/shared-voices/{id}` · `POST /api/hub/shared-voices/{id}/delete-sync` | Remove the Hub master and exact managed fleet copies / retry pending removals |
| `GET /api/hub/shared-voices/{id}/audio` | Stream the canonical authenticated reference clip |
| `GET /api/hub/providers` | Fleet-wide cloud audio provider readiness, configuration counts, and reporting Voice Studio endpoints |
| `POST /api/hub/transcribe` | Multipart audio transcription routed to a free Voice Studio that has the selected Whisper model cached |
| `POST /api/hub/transcription/jobs` | Stream a multi-file episode transcription batch into the persistent fleet queue |
| `GET /api/hub/transcription/jobs` · `GET /api/hub/transcription/jobs/{batch}` | List batches/lifetime totals or read chapter-level status |
| `GET /api/hub/transcription/jobs/{batch}/items/{index}/artifact` | Download a verified completed SRT through Hub authentication |
| `DELETE /api/hub/transcription/jobs/{batch}` · `POST /api/hub/transcription/jobs/{batch}/retry` | Cancel unfinished chapters or retry failed/interrupted chapters only |
| `POST /api/hub/transcription/jobs/clear` · `POST /api/hub/transcription/jobs/{batch}/clear` | Permanently clear terminal transcription history and its Hub-local input/SRT files; active work is refused |
| `POST /api/hub/chat/jobs` | Submit visual or motion prompts as adaptive worker packs (10 local/free-cloud; up to 30 paid-cloud) |
| `GET /api/hub/chat/jobs` · `GET /api/hub/chat/jobs/{batch}` | Read compact fleet history or full pack/scene results |
| `DELETE /api/hub/chat/jobs/{batch}` · `POST /api/hub/chat/jobs/{batch}/retry` | Cancel unfinished packs or retry only missing scene IDs |
| `GET /api/hub/transcription/settings` · `POST /api/hub/transcription/settings` | Read/set SRT and upload retention (`1`, `3`, `7`, `15`, `30`, or `90` days; default `3`) |
| `POST /api/hub/transcription/cleanup` | Clean expired terminal transcription files; active batches are never removed |
| `GET /api/hub/storage-policy` | Read the common policy plus per-Mac/per-app disposable output usage across reachable peer Hubs |
| `PUT /api/hub/storage-policy` | Save and propagate `{enabled, retention_days, max_gb}` to every reachable Hub and Studio |
| `POST /api/hub/storage-policy/cleanup` | Run the three-day sweep and combined per-Mac capacity enforcement immediately |
| `GET` / `POST /api/hub/job-storage` · `POST /api/hub/job-storage/cleanup` | Compatibility API for the Hub transcription store; defaults to enabled, three days, and 80 GB |
| `GET /api/auth/status` · `POST /api/auth/login` · `POST /api/auth/logout` | Browser password-sign-in capability, 90-day remembered-device session, and sign-out |
| `POST /api/auth/setup` | Set or replace the owner password; accepted only through loopback on the Hub Mac |
| `GET` / `POST /api/hub/registry/hardware-profiles` | List the reusable hardware catalog and assignments / add a future hardware class |
| `PUT /api/hub/registry/machines/{machine}/hardware-profile` | Assign, change, or clear an existing machine's profile with `{"hardware_profile_id": "mac-mini-m4-16gb"}` |
| `DELETE /api/hub/registry/machines/{machine}` | Unregister a machine and purge its live inventory/update state (history is retained) |
| `GET /api/hub/fleet` · `POST /api/hub/fleet` | Fleet token status / set (`{token}`) — enables remote specs + control |
| `GET /api/hub/resources?local_only=true` | This machine only (peers call with this to prevent recursion) |
| `GET /api/hub/resources` | Host memory/CPU + per-studio process stats, including key-free Voice provider health |
| `GET /api/hub/memory` | Read model-memory policy, loaded-model state, friendly process title, and reachability for every model-hosting Studio |
| `PUT /api/hub/memory-policy` | Apply `{mode, studio_ids?}` using `performance`, `balanced`, `memory_saver`, or `immediate` |
| `POST /api/hub/memory/release` | Release idle model/accelerator memory on selected Studios; returns one result per worker |
| `GET /api/releases` | Current Hub version and complete release details read from the shipped changelog |
| `GET /api/hub/summary` | One-shot dashboard payload (studios + resources + cloud provider inventory) |
| `POST /api/hub/studios/{id}/start` | Start a local studio (via Pinokio's `pterm` CLI) |
| `GET` / `POST /api/hub/maintenance/studio-versions` | Read saved or rescan running/latest Studio versions and reachability |
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
| `POST /api/hub/jobs/clear` · `POST /api/hub/jobs/{batch}/clear` | Clear terminal generation history and Hub-owned ledger/files only; remote worker output is never removed |
| `GET /api/hub/assets` · `POST /api/hub/assets/scan` | Asset ledger (query: `q`, `modality`, `studio`, `batch_id`) |
| `POST /api/hub/assets/upload` | Upload a reference image once → `{asset_id}` (for img2img continuity) |
| `POST /api/hub/render-assets` | Stream or reuse an immutable content-addressed render input; returns path, bytes, and SHA-256 |
| `GET /api/hub/render-assets/by-sha/{sha256}` | Look up and refresh a retained render input by checksum before uploading it again |
| `GET /api/hub/jobs/{batch}/items/{index}/artifact` | Stream a completed worker video through Hub authentication |
| `POST /api/hub/jobs/{batch}/items/{index}/ack` | Confirm the main copy was verified and start worker retention |
| `GET /api/hub/stats[?hours=N]` | Generation analytics: by machine/modality/model + timeline |
| `POST /api/hub/recipes/run` | Run a recipe chain (`{recipe, brief}`) |
| `GET /api/hub/recipes/runs[/{id}]` | Recipe run status |
| `POST /api/hub/director` | `{brief, auto_run?}` — LLM plans a recipe from plain English |

### Shared voice API examples

The dashboard is the easiest workflow because it performs transcription and
lets you review the text first. Programmatic clients can submit the reviewed
reference directly.

```bash
curl -X POST "$HUB/api/hub/shared-voices" \
  -H "X-Hub-Token: $HUB_TOKEN" \
  -F 'audio=@aiden.wav' -F 'name=Aiden' -F 'language=en' \
  -F 'gender=m' -F 'license=self-owned' \
  -F 'transcript=The exact reviewed words spoken in the clip.' \
  -F 'permission_acknowledged=true'
```

```javascript
const body = new FormData();
body.append("audio", referenceFile);
for (const [key, value] of Object.entries({
  name: "Aiden", language: "en", gender: "m", license: "self-owned",
  transcript: reviewedTranscript, permission_acknowledged: "true",
})) body.append(key, value);
const sharedVoice = await fetch(`${HUB}/api/hub/shared-voices`, {
  method: "POST", headers: {"X-Hub-Token": token}, body,
}).then(response => response.json());
```

```python
with open("aiden.wav", "rb") as audio:
    response = httpx.post(
        f"{HUB}/api/hub/shared-voices",
        headers={"X-Hub-Token": token},
        files={"audio": ("aiden.wav", audio, "audio/wav")},
        data={"name": "Aiden", "language": "en", "gender": "m",
              "license": "self-owned", "transcript": reviewed_transcript,
              "permission_acknowledged": "true"},
    )
response.raise_for_status()
```

## Client integration

Customer-facing and GenStudio-routed applications call GenStudio KH, never a
Studio, Studio Hub, or inference provider. GenStudio's private adapter then
assigns an execution attempt to a location controller. The existing direct
Story Studio/Hub route remains available only as the explicitly selected legacy
or internal route during migration:

1. Store two values: the Hub URL (`http://<tailscale-ip>:47873`) and the token.
2. Submit work: `POST /api/hub/jobs` with `label` (your app's name) and,
   ideally, `webhook` — the Hub POSTs the batch summary (incl. per-item
   `artifact_url`) to that URL the moment the batch finishes. No polling.
3. Or poll `GET /api/hub/jobs/{batch_id}` — this survives Hub restarts
   (batches are persisted in `hub.db`; in-flight items are safely re-queued).

### Terminal voice result (billable audio)

Completed job items include a path-free `terminal_result` envelope. For a
validated WAV voice result it contains `asset_id`, Hub-relative `artifact_url`,
`media_type` (`audio/wav`), `format`, `bytes`, `sha256`,
`audio_duration_ms`/`audio_duration_s`, `sample_rate_hz`, `channels`, and
`runtime_s`. `runtime_s` is the worker processing time; it is deliberately
separate from decoded audio duration. `duration_s` remains temporarily as a
backward-compatible alias for `runtime_s` only.

The evidence maps to the Audio Job Result v1 contract as follows: `asset_id`,
`artifact_url`, and the media facts map to `audio`; `runtime_s * 1000` maps to
`execution.runtime_ms`. Hub batch IDs and worker IDs remain site-local execution
identities and must never be presented as GenStudio customer job or global
attempt ownership IDs. GenStudio supplies those IDs separately and owns its
final object-store `object_key`. The Hub never includes a worker-local
`artifact_path` in this public result.

For current WAV-producing Voice Studio workers, Hub downloads and validates the
artifact once at terminal completion, then stores the facts with the batch so
later polling and artifact reads do not recompute its checksum or duration.
Non-WAV workers should provide the equivalent validated audio metadata before
their output is treated as billable by a contract consumer.

### Episode transcription contract

Submit one multipart request with repeated `files` and matching repeated `item_ids`.
The item IDs must be stable chapter slugs or names and must appear in the same order
as their files. `project` and `episode` are optional, but supplying both gives active
submissions stable idempotency across client retries.

```bash
curl -X POST "$HUB/api/hub/transcription/jobs" \
  -H "X-Hub-Token: $HUB_TOKEN" \
  -F 'files=@DK0039_Introduction.mp3' \
  -F 'files=@DK0039_Chapter_01.mp3' \
  -F 'item_ids=Introduction' \
  -F 'item_ids=Chapter_01' \
  -F 'model=mlx-community/whisper-large-v3-turbo' \
  -F 'language=en' \
  -F 'word_timestamps=true' \
  -F 'label=Story Studio KH' \
  -F 'project=dark-kingdom' \
  -F 'episode=DK0039'
```

An accepted request returns immediately. Repeating the same active project, episode,
model, item IDs, and filenames returns the original `batch_id` with `duplicate: true`.

```json
{"batch_id":"abc123def456","items":2,"queued":2}
```

Poll `GET /api/hub/transcription/jobs/abc123def456`:

```json
{
  "id": "abc123def456",
  "status": "running",
  "project": "dark-kingdom",
  "episode": "DK0039",
  "model": "mlx-community/whisper-large-v3-turbo",
  "total": 2,
  "queued": 0,
  "running": 1,
  "done": 1,
  "error": 0,
  "cancelled": 0,
  "items": [{
    "index": 0,
    "item_id": "Introduction",
    "filename": "DK0039_Introduction.mp3",
    "state": "done",
    "studio": "voice@macmini-m4-001",
    "studio_task_id": null,
    "duration_seconds": 18.4,
    "media_duration_seconds": 301.2,
    "artifact_url": "/api/hub/transcription/jobs/abc123def456/items/0/artifact",
    "error": null,
    "tries": 1,
    "metadata": {"text":"...","language":"en","duration":301.2,"segments":[],"vtt":"WEBVTT..."}
  }]
}
```

Download `artifact_url` with the same Hub token. Cancel with `DELETE` on the batch URL;
completed SRTs remain available. Retry with `POST .../{batch_id}/retry`; its response is
`{"batch_id":"abc123def456","retried":1,"status":"queued"}` and successful chapters
are not retranscribed.

JavaScript submission:

```javascript
const body = new FormData();
for (const chapter of chapters) {
  body.append("files", chapter.file, chapter.filename);
  body.append("item_ids", chapter.id);
}
body.append("model", "mlx-community/whisper-large-v3-turbo");
body.append("language", "en");
body.append("word_timestamps", "true");
body.append("project", "dark-kingdom");
body.append("episode", "DK0039");
const batch = await fetch(`${HUB}/api/hub/transcription/jobs`, {
  method: "POST", headers: {"X-Hub-Token": token}, body
}).then(response => response.json());
```

Python submission:

```python
with httpx.Client(headers={"X-Hub-Token": token}) as client:
    response = client.post(
        f"{HUB}/api/hub/transcription/jobs",
        data=[("item_ids", "Introduction"), ("item_ids", "Chapter_01"),
              ("model", "mlx-community/whisper-large-v3-turbo"),
              ("project", "dark-kingdom"), ("episode", "DK0039")],
        files=[("files", ("intro.mp3", open("intro.mp3", "rb"), "audio/mpeg")),
               ("files", ("chapter-01.mp3", open("chapter-01.mp3", "rb"), "audio/mpeg"))],
    )
    batch = response.raise_for_status().json()
```

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
  **Registered machines** list. Registration starts with a reusable hardware
  profile and suggested stable ID; profiles remain editable later. Each Studio
  has its own new-job pause/resume switch, while the machine switch remains the
  master control.

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

## Remote browser sign-in

For normal dashboard use, open **Remote → Owner sign-in** on the Hub Mac and
set any non-empty password. Remote browsers on your Tailscale network
then see a normal sign-in screen; Chrome can save the password and a successful
sign-in is remembered for 90 days. The password is salted and scrypt-hashed,
and the Hub keeps only hashes of remembered-device sessions.

Password sign-in is deliberately accepted only through the Hub's Tailscale
address, not the ordinary HTTP LAN address. Use the LAN address with the Hub
token only when necessary for API/recovery access.

The existing Hub token is still required for scripts, API clients, peer Hubs,
and recovery. It is shown only locally in **Remote**. Replacing the owner
password signs every remembered browser out immediately.

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

The first Hub creates the owner-only local value; treat the primary Hub as the
source of truth. **Save & verify** rotates connected peers using the previously
trusted value and then verifies the replacement. To repair a rejected value,
open that Mac's Hub locally, paste the primary value, save it once, and
restart/update Studios that still show an authentication warning. The value is
sent only in headers or an HttpOnly same-site session—never in a URL.

### Controller and agent roles

All Macs install the same Studio Hub repository and version. Configure one Hub
per location as a `controller`; configure worker-node Hubs as `agent`. Agent Hubs
remain the local authority for their Studios, reject customer-style queue
submissions, and must not receive PostgreSQL credentials.

GenStudio is the sole global customer-job and business authority. It selects a
healthy location, issues the execution attempt and fencing token, and owns
cross-location recovery. Studio Hub performs only site-local admission,
dispatch, safe local retry, and execution evidence. Its optional PostgreSQL
integration is permanently shadow-only and cannot claim or transfer work. See
[`CONTROLLER_ARCHITECTURE.md`](CONTROLLER_ARCHITECTURE.md) for setup, environment
variables, external-attempt validation, and the permanent ADR-0007 boundary.

### Private GenStudio capability snapshot

GenStudio reads one current site snapshot composed from the Hub's existing
health, registry, catalog, hardware, resource, and scheduler sources. Catalog
reads use the existing monitor cache and read-only Studio catalog API. This
endpoint never drains, dispatches, claims, or retries work:

```bash
curl "$HUB/api/hub/capabilities" \
  -H "Authorization: Bearer $HUB_TOKEN"
```

`X-Hub-Token` is also accepted, and either this Hub's token or the shared fleet
token may be used. Unlike the normal operator API, this machine-to-machine
contract requires a header token even from loopback; browser sessions and
cookies are not accepted.

Schema `studiohub.site-capabilities`, version `1`, includes controller/site
identity, machine hardware profiles, worker readiness and shared physical-Mac
capacity, supported operations, and sanitized model controls. A model's
`runtime_revision` is populated only when the Studio catalog reports a full
immutable hash. Otherwise it remains `null` with
`availability.revision_pinning_ready=false`; Studio Hub never invents a model
revision.

Capability telemetry never includes customer prompts/text, generated content,
artifact paths, cache paths, credentials, GenStudio job or attempt IDs,
idempotency keys, or fencing tokens.

The complete stable field and availability semantics are documented in
[`CAPABILITY_CONTRACT.md`](CAPABILITY_CONTRACT.md).

## Release discipline

Every committed Studio Hub change increments `VERSION` and adds matching
changelog and dashboard **What's New** entries, including documentation-only
updates. See [`RELEASE_POLICY.md`](RELEASE_POLICY.md); the test suite verifies
that all three release metadata sources identify the same newest release.

## Multiple Macs (registry)

Every Mac keeps running its own studios; one location controller coordinates the
workers registered at that site. GenStudio will choose between locations:

1. On each other Mac, install whichever studios it should serve (2, 3, or 5).
2. On the Hub Mac, open **Remote → Add another Mac's studios**, choose its
   hardware profile, and enter its Tailscale IP. Studio Hub suggests a stable ID
   such as `macmini-m2-8gb-001`; you may edit it before saving. Two ways to add:
   - **Discover & Add** — probes the machine now and registers whatever answers
     (machine must be online).
     `POST /api/hub/registry/discover {host, machine, hardware_profile_id}`.
   - **Add manually (offline OK)** — pre-registers the *checked* studios without
     probing, so you can set a machine up before it's online; it flips from
     "down" to live automatically when reachable.
     `POST /api/hub/registry/add {host, machine, modalities, hardware_profile_id}`.
3. Remote studios join the health grid, catalog, gateway and **worker pools**
   automatically.

The built-in list covers the approved Mac mini M1/M2/M4, MacBook M4, and iMac
M1/M3 memory classes. Use **Add another hardware profile to this list** for a
future machine class. Existing machines can be assigned or corrected directly
in **Registered machines** without re-registering their Studios.

Use each Studio switch in **Remote → Registered machines** to dedicate a Mac to
only the job types you want. For example, pause Voice, Chat, and Render while
leaving Image ready. A pause affects only future dispatch: an active job finishes,
health monitoring and updates continue, and the saved choice survives Hub restarts.
The machine-wide switch overrides every Studio switch without erasing them.

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
automatically joins the pool). Transient connection and gateway failures get a
bounded 30-minute self-healing window with progressive backoff; genuine
generation failures retain three attempts, while authentication and validation
failures stop immediately with their original reason. Every
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

GenStudio may additionally assign the site execution by supplying
`genstudio_job_id`, `genstudio_attempt_id`, `idempotency_key`, `fencing_token`,
`site_id`, and `operation` (plus optional model/voice revisions). Studio Hub
hashes the idempotency key, rejects stale externally issued fences, and returns
the same local batch for an exact replay. It never issues or increments the
global fence:

```json
{
  "genstudio_job_id": "job_01...",
  "genstudio_attempt_id": "attempt_01...",
  "idempotency_key": "stable-non-secret-attempt-key",
  "fencing_token": 42,
  "site_id": "phnom-penh-1",
  "operation": "tts"
}
```

The **fleet memory governor** uses live host telemetry from each connected peer
Hub before local or remote dispatch. A model whose `min_unified_memory_gb`
exceeds a machine is skipped for a compatible larger Mac; one whose size does
not fit in currently-free memory waits (visible as `governor_note`). The Hub
can apply a stricter live-free-memory floor for a workload when its technical
minimum alone would not leave reliable operating headroom. GenStudio's Qwen3
0.6B standard-voice model is supported on 8 GB Apple-silicon Macs, but is sent
there only when at least 3.2 GB is currently free for a safe cold load. When
that capacity is unavailable, the item waits or uses another eligible worker;
the 8 GB machine remains available for image work throughout. A worker's own
MemoryGuard remains the final authority, and its refusal waits/rebalances
without consuming an attempt. Repeated connection failures temporarily pause
that physical Mac and let another worker steal the item. Cloud-backed models
bypass the memory governor entirely.

## Chat prompt packs

Chat work uses a saved queue separate from media generation. Each item is one
LLM request containing at most 10 local/free-cloud scenes or 30 paid-cloud
scenes. Story Studio defaults paid cloud to 20 for output reliability. One
eligible Chat Studio leases one pack at a time. This is an adaptive wave size,
not a 100-scene limit: 70 scenes
can use seven compatible servers at once; 200 scenes with five compatible
servers continue over four waves. A batch may contain up to 5,000 scenes.
Workers pull another pack as soon as they finish, so faster Macs naturally do
more work. The oldest runnable episode receives every compatible free worker
before newer episodes. A newer episode can still use a server that cannot run
the oldest batch's model, so capable hardware is not left idle.

```bash
curl -X POST http://localhost:47873/api/hub/chat/jobs \
  -H "Content-Type: application/json" -d '{
  "model": "mlx-community/Llama-3.2-3B-Instruct-4bit",
  "model_cost_tier": "local",
  "kind": "visual",
  "project": "dozing-knight",
  "episode": "DK0001",
  "packs": [{
    "pack_id": "visual-0001-0010",
    "scene_ids": ["scene-001", "scene-002"],
    "messages": [
      {"role": "system", "content": "Return JSON prompts keyed by scene_id."},
      {"role": "user", "content": "Create prompts for the supplied scenes."}
    ],
    "params": {"temperature": 0.4, "max_tokens": 4096}
  }]
}'
```

The model response may be an array under `results` or `prompts`, or an object
mapping scene IDs to text. Array rows use `scene_id` plus `visual_prompt`,
`motion_prompt`, `prompt`, or `text`. Unknown IDs are ignored. Valid IDs are
persisted immediately; incomplete packs automatically retry only missing IDs.
Transient failures also retry automatically, with a maximum of three attempts.
After that, **Retry missing** starts a fresh retry only for incomplete/failed
packs and never discards completed scene results. Poll
`GET /api/hub/chat/jobs/{batch_id}` for full per-scene text.

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
  `Authorization: Bearer <token>` or `X-Hub-Token: <token>`.
  StudioHub also creates an owner-only fleet token automatically and forwards it as
  `X-Studio-Token` to sibling Studio APIs. Local loopback use remains passwordless;
  remote Studio API, OpenAI-compatible, settings, upload, and output routes require it.
  The dashboard page and `/api/health`/`/api/version` stay public; the
  dashboard prompts for the token once and establishes an HttpOnly same-site
  session for its live stream. Tokens in query strings are rejected so they
  cannot leak through browser history or access logs.
- The token is auto-generated into `.hub_token` (gitignored). See it in the
  dashboard's **Remote** tab (only shown when viewed on the Hub machine).
  Rotate it by deleting the file and restarting the Hub.
- **Control from anywhere:** install Tailscale on your phone/laptop, then open
  the Tailscale URL shown in the Remote tab. Your Mac stays the server; no
  cloud middleman.

Runtime dependency ranges remain in `app/requirements.txt`; the exact tested
transitive set used by Install and Update is in `app/requirements.lock`.

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

# Save the default local-backup policy to every reachable Mac
curl -X PUT http://localhost:47873/api/hub/storage-policy \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"retention_days":3,"max_gb":80}'

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

// Enforce retention and the combined per-Mac cap immediately
const storage = await fetch(`${HUB}/api/hub/storage-policy/cleanup`, {
  method: "POST",
}).then(r => r.json());
console.log(storage.machines.map(m => [m.machine, m.used_bytes, m.over_limit]));
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

    storage = client.get(f"{HUB}/api/hub/storage-policy").json()
    for machine in storage["machines"]:
        print(machine["machine"], machine["used_bytes"], machine["over_limit"])
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
