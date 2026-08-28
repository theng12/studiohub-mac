# Studio Hub KH

Production control plane for **Image Studio (47868)** and **Voice Studio
(47870)**, with Studio Hub itself on **47873**. Studio Hub ignores legacy Music,
Chat, Video, and Render registrations, including rows saved by older releases.
Their app folders are left untouched and are no longer part of fleet tracking.

The Hub runs on fixed port **47873** and provides:

- **Live health grid** — up/down, version, latency and last-seen for every studio.
- **Unified local model catalog** — every on-device model across all generative
  studios in one searchable table (downloaded state, size, and minimum
  unified-memory fit). Hosted provider rows are discarded at the fleet boundary.
  Per-model parameters are passed through verbatim — the Hub never flattens
  model-specific capabilities.
- **Resource monitor** — host unified-memory pressure + per-studio process memory
  (RSS) and CPU, resolved port → PID → process tree. It also watches Pinokio's
  Caddy proxy for abnormal memory/file-descriptor growth caused by port conflicts.
- **Fleet model-memory control** — choose retained, 10-minute, 2-minute, or
  immediate idle release for individual local/remote Studios. Each Studio starts
  from its own memory-safe default; a manual release button unloads idle models
  without stopping an app or interrupting active work.
- **Shared voice library** — upload and transcribe one cloning reference in Hub,
  review the words, then synchronize the same stable ID, audio hash, and
  transcript to every Voice Studio Mac. Offline machines catch up automatically.
- **Host-aware registry** — studios on other machines (LAN/Tailscale) can be added
  with an automatically detected hardware profile and stable machine ID. The profile
  is published with live resources for routing and GenStudio operating-cost records.
  Profiles describe capacity and never cap how many machines can be registered.
- **Site-controller boundary** — the same Hub release can run as a standalone
  Hub, location controller, or agent. GenStudio owns customer jobs, attempts,
  billing, global retries, fencing, and cross-location routing. Optional
  PostgreSQL shadow mode publishes operational evidence only; SQLite remains
  permanently authoritative for Studio Hub's site-local scheduler.
- **Private site-capability contract** — GenStudio can authenticate with the Hub
  or fleet token and read one schema-versioned snapshot of machines, hardware,
  workers, models, controls, limits, revisions, and truthful current capacity.
- **Durable managed releases** — GenStudio can pin Hub, installed Image, and
  installed Voice to exact approved commits. Each controller persists and
  resumes its site job, rolls one machine at a time behind a canary, and keeps
  offline or busy machines pending without blocking healthy capacity.
- **Machine-level work leases** — image generation and final rendering take turns
  on each Mac without pausing active work. Waiting render jobs are assigned first,
  with faster M4 16 GB workers preferred when available.
- **Fleet local-backup protection** — each Mac automatically keeps disposable
  generated output within one combined 80 GB budget and clears completed files
  after three days. The main Hub can save or run the policy across all reachable
  peer Hubs without touching active jobs, source/reference uploads, shared voices,
  models, chat history, credentials, or results still awaiting delivery.

## How to use

1. **Install** — click *Install* in the Pinokio sidebar (creates a small `conda_env`
   with FastAPI/httpx/psutil; no AI bundle needed — the Hub runs no models).
2. **Start** — click *Start*. The dashboard opens at `http://localhost:47873`.
3. **Tabs**: *Overview* (studio cards), *Models* (unified catalog with search and
   filters), *Voices* (transcribe and share cloning references), and *Resources*
   (host memory bar + per-studio table).
4. The dashboard updates continuously over SSE, falls back to 5-second polling
   if the stream drops, and reconnects automatically with bounded backoff.

### Dashboard text and control sizing

The dashboard uses one shared readability scale: 12 px is the absolute floor
for compact labels and badges, secondary text is 13 px, body text is 14 px,
form controls are 15 px, and native picker options are 16 px. Standard controls
are at least 40 px high where the layout permits. These values live as CSS
tokens in `app/frontend/index.html`; the frontend typography regression tests
reject any future literal font size below the 12 px floor and verify that native
select menus keep their dedicated readable option size.

### Control model memory

Open **Memory** in Studio Hub. Every registered Image and Voice Studio appears
separately, including Studios reached through a peer Hub.
Select the workers you want and choose:

- **Performance** — preserve loaded models for the fastest repeat
  generation. Nothing unloads automatically.
- **Balanced** — release model and accelerator caches after 10 idle minutes.
- **Memory Saver** — release after 2 idle minutes.
- **Immediate** — release as soon as current work is finished.

Before you save an explicit choice, new Image and Voice installs and Hub's
bulk-control draft start on Immediate so another sibling can use the Mac as
soon as work finishes. Existing saved Studio choices remain authoritative.
Performance is always an explicit operator choice.

**Release selected now** is the manual equivalent. A Studio with queued or
running work refuses safely; other selected Studios still complete. Offline
workers and older versions are shown explicitly, so you can update or reconnect
only those workers and retry. Policies are persisted by each Studio, not the
Hub, and therefore survive Hub restarts and continue working when a remote Hub
is temporarily unavailable.

When queued generation or transcription work cannot meet its live free-memory
floor, Hub automatically asks the other tracked Studio on that same
Mac to release idle resident state. It refreshes the Mac's RAM telemetry and
reruns admission before assigning the job. A sibling with queued or active work
refuses the handoff, so this switches idle models without preempting work or
changing the operator's saved memory mode.

After dependency installation and the next Studio restart, macOS Activity
Monitor shows `Image Studio Mac`, `Voice Studio Mac`, and `Studio Hub Mac`
instead of a generic
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
Updates view refreshes automatically, retains last-known release
versions through temporary GitHub failures, and never uses an older worker cache
as the fleet target. You can change every app independently, **Check all**,
update one app, or **Update idle apps**. Fleet updates run one at a time,
reconnect through the expected restart connection drop, and require the updated
app to reach the published version and answer healthy before the next one starts.
Agent Hub rows retain their latest per-machine update outcome. A rescan refreshes
version and reachability without hiding a failed attempt, its reason, or its
Retry action; a later explicit successful attempt replaces that failure. A
legacy Pinokio restart timeout counts as complete only when exact target version,
commit, and health evidence already prove the new Hub is running.
Render Studio participates in the same inventory and controls, including a
shortcut to its local automatic-update card.
If a downloaded Hub update remains on disk while the old process is still
answering, use **Restart Hub now**. The authenticated action validates the Git
checkout and installed startup service, delays shutdown until the API response
has returned, then reconnects the dashboard after the expected version is
healthy. Active work is refused by default; an explicit forced API request is
available for supervised recovery.
If an update command never starts a restart, the operation fails visibly after
three minutes instead of looking busy for the entire 20-minute recovery window;
once a restart really begins, the longer window remains available for slow Macs.
Busy apps receive a durable update-after-current-work request on their own Mac;
rolling progress survives a Hub restart, transient connections retry with visible
attempt counts, and a failed subset can be retried centrally without selecting it
again. One unavailable node never blocks healthy targets: the operation completes
with reduced capacity when any target succeeds, and fails only when every selected
target fails.
Updates also contains startup-service checks, generation-dependency maintenance,
and the same simple version controls for Studios and agent
Hubs: rescan, compare running with latest, update everything ready, or update
one row. Studio
app tabs focus the action on one family; **All apps** targets the fleet. Slow
agent Hubs get four bounded connection attempts before they are reported offline,
and remote operation history remains visible across a primary-Hub restart.
The agent-Hub table also reports each Mac's Apple chip and unified RAM and can
be sorted by availability, machine name, chip generation, or RAM in either
direction. The selected order is remembered in that browser.

The Updates tab also has **Reinstall generation everywhere**. It is separate
from normal updates because it may download large dependencies and restart a
Studio. Each Mac's own Hub runs its trusted sibling `install_generation.js`
script; installs are serial per Mac, parallel across independent Macs, active
Hub work drains first, and the `GEN_VERIFY_OK` marker is required before a row
is reported successful. GenStudio can invoke the same site-owned action across
all connected locations.

**Repair blocked Studio updates** is a separate one-time bridge for legacy
Voice/Image checkouts whose tracked machine `ENVIRONMENT` prevents `git pull`.
The Agent Hub backs up and preserves the complete regular file (including
machine-specific compiler settings), migrates only an otherwise-clean expected
checkout, then runs the Studio's normal update, dependency convergence, restart,
and health verification. Repairs are serial per Mac and parallel across Macs;
offline or older Agents remain pending and can be retried. An Agent Hub must be
updated to a version advertising `studio_update_repair_schema: 1` before a
controller can repair its sibling Studios remotely. A Hub whose own checkout is
blocked still needs SSD Stage 5 once. Models, enrollment, fleet credentials,
voices, and jobs are never changed by this repair.

Every app independently enforces its expected GitHub origin and `main`, a clean
fast-forward, free disk, dependency/import checks, healthy restart, and exact
running version. Dirty, detached, divergent, or rewritten repositories are
refused without changing files. Failure makes one bounded rollback attempt;
rotating redacted logs are under `logs/auto_update/` in that app.

### Managed fleet releases

Managed releases are a separate GenStudio-owned path. GenStudio first sends a
canonical `genstudio.studio-fleet-release-intent` version 1 manifest to
`PUT /api/hub/maintenance/release-intent`, then activates that exact
`release_id`. The manifest pins the repository, SemVer, and lowercase 40-hex
commit for Hub plus installed Image and Voice. A duplicate intent or activation
adopts the existing durable state; it never creates duplicate component work.

Within a location, the first reachable remote machine is the canary. Hub,
installed Image, and installed Voice update serially on that Mac; remaining
agents follow in stable machine-ID order; controller Image and Voice run before
the controller Hub updates last. Managed code never calls the moving-`main`
`update.js` path. Success requires the restarted app to report both the exact
target version and `app_commit`, and each sibling must advertise
`managed_exact_commit: true`. The first qualified sibling targets are Image
Studio `1.30.1` and Voice Studio `2.3.0`; older installed siblings remain
retryable and are never silently sent through an ordinary updater.

Offline, busy, disk-limited, authentication-blocked, and target-local failures
remain nonterminal with bounded persisted retry. They reduce capacity but do
not block later healthy machines or locations. Only a malformed or mismatched
immutable manifest, an exact target mismatch, or the same clean-checkout health
failure on two machines blocks that frozen release. Nonterminal jobs resume
after Hub restart and a returning peer is scheduled immediately.

The Updates page shows this as a read-only **Managed release** card. It cannot
activate or retry a release and does not change ordinary per-app **Off**,
**Notify only**, **Auto**, schedule, maintenance-hour, or idle-only settings.
After software convergence Hub requests the existing approved model-catalog
reconciliation; that request is evidence, not a claim that downloads finished.

PPS predates the exact-update protocol. A legacy 2.6.x PPS controller that was
already offline remains `physical_bootstrap_required` and nonblocking. It must
receive a separately attested immutable bootstrap ancestor, or be observed on a
safe bootstrap before the owner approves a new descending intent. The legacy
moving-main updater is not a valid bootstrap proof.

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
| `GET /api/version` | Running Hub identity plus additive maintenance capabilities such as integer `studio_update_repair_schema: 1` |
| `GET /api/auto-update/status` · `GET /api/auto-update/readiness` | Hub updater settings/state and idle blockers |
| `POST /api/auto-update/settings` · `POST /api/auto-update/check` | Save and validate the opt-in schedule / check safely now |
| `POST /api/auto-update/update` · `POST /api/auto-update/retry` | Update now or after current work / retry a failed update |
| `POST /api/hub/maintenance/restart` | Restart the installed Hub startup service; JSON body `{"force": false}` refuses active work by default |
| `GET /api/hub/auto-updates` · `POST /api/hub/auto-updates/check-all` | Fleet automatic-update inventory / ask every app to check |
| `POST /api/hub/auto-updates/{target}/mode` | Change one app's Off, Notify, or Auto mode while preserving its schedule |
| `POST /api/hub/auto-updates/update-idle` | Start a staggered, health-gated update for selected idle sibling Studios |
| `POST /api/hub/auto-updates/jobs/{id}/retry` | Retry only the failed apps from a saved automatic fleet update |
| `GET` · `POST /api/hub/maintenance/studio-update-repairs` | List durable one-time Voice/Image update repairs / start fixed local-or-fleet repair targets |
| `GET /api/hub/maintenance/studio-update-repairs/{id}` · `POST …/{id}/retry` | Poll one durable repair / retry only its pending or failed Studios |
| `GET /api/hub/studios` | Registry + live status per studio |
| `GET /health/live` · `GET /health/ready` · `GET /health/capacity` | Controller liveness, site-execution readiness, and non-secret routing capacity; optional telemetry never gates readiness |
| `GET /api/hub/capabilities` | Private cache-only GenStudio capability snapshot (schema v3); active managed intent adds exact-release convergence evidence, while only manifest-block states suppress otherwise healthy capacity |
| `GET` · `PUT /api/hub/maintenance/release-intent` | Read sanitized managed-release status / controller-only machine-token write of one immutable desired manifest |
| `POST /api/hub/maintenance/release-intent/{release_id}/activate` | Controller-only activation or adoption of the durable site release job; optional `{ "genstudio_run_reference": "..." }` |
| `GET /api/hub/maintenance/release-jobs/{job_id}` | Read sanitized per-machine/component state, exact version/commit evidence, retry, and catalog request evidence |
| `POST /api/hub/maintenance/managed-update` · `GET /api/hub/maintenance/managed-update/{job_id}` | Agent-only authenticated child admission/adoption and polling; used by the location controller, not GenStudio directly |
| `GET /api/hub/model-exposures` | Cache-only audited candidate, fleet supply, and historical exposure inventory |
| `POST /api/hub/model-exposures/approve` | Owner-only approval of an exact model + operation + revision + contract hash |
| `POST /api/hub/model-exposures/revoke` | Owner-only stop for an exact exposure, including historical candidates no longer online |
| `POST /api/hub/catalog/refresh` | Owner-triggered bounded concurrent sibling catalogue refresh; retains last-good failures |
| `GET /api/hub/controller` · `PUT /api/hub/controller` | Read or configure this Hub's `standalone`, `controller`, or `agent` role, site identity, and optional evidence-shadow mode |
| `POST /api/hub/controller/check` | Verify the optional PostgreSQL evidence schema and publish an immediate heartbeat |
| `POST /api/hub/setup/controller` | Local simple setup for the first Mac at a new location; assigns identity and local hardware while forcing PostgreSQL off |
| `GET` · `POST` · `DELETE /api/hub/enrollment-codes` | Read, generate/rotate, or revoke the controller's permanent reusable enrollment code; owner access is required to reveal or change it |
| `GET /api/hub/enrollment/info` | Private read-only controller identity, version, role, enrollment readiness, and `repair_schema_version`; contains no credentials |
| `POST /api/hub/enrollment/claim` | Private LAN/Tailscale claim of the permanent code; optionally accepts machine/profile/modalities to register that Agent from its private source address and returns the site identity, fleet credential, and registration result |
| `POST /api/hub/setup/check-controller` | Read-only validation of a pasted private controller address; accepted locally or from an owner-authenticated browser |
| `POST /api/hub/setup/join` | Guided worker setup using a private controller address, permanent enrollment code, and local hardware profile; accepted locally or from an owner-authenticated browser |
| `GET /api/hub/startup-services` | Audit sibling Studio launchd service and watchdog readiness on this Hub and authenticated peer Hubs |
| `POST /api/hub/startup-services/{machine}/{studio}/install` | Install or repair one sibling's startup service on its own machine; refuses Hub-tracked active work |
| `POST /api/hub/registry/studios/{id}/enabled` | Pause/resume new jobs for one Studio with `{"enabled": false/true}`; running work and the process are untouched |
| `GET /api/hub/health` | Aggregate: totals + per-studio statuses |
| `GET /api/hub/catalog` | Local per-studio catalog rows (annotated `hub_cached`, `hub_machine`). Query: `q`, `modality`, `downloaded`, `force` |
| `GET /api/hub/models` | **Deduped by repo** with per-machine availability (`cached_on`, `machines[]`). Query: `q`, `modality`, `downloaded` |
| `GET /api/hub/transcription` | Fleet-wide Whisper inventory with `cached_on`, `available_on`, ready counts, and recommended default |
| `GET /api/hub/shared-voices` | List Hub-owned cloning references plus pending per-machine deletions |
| `POST /api/hub/shared-voices/transcribe` | Transcribe one multipart reference clip through the existing fleet queue and return editable plain text |
| `POST /api/hub/shared-voices` | Save one multipart reference + reviewed transcript and begin synchronization to all Voice Studio Macs |
| `PATCH /api/hub/shared-voices/{id}` · `POST /api/hub/shared-voices/{id}/sync` | Correct shared metadata/transcript and resynchronize / manually retry all targets |
| `DELETE /api/hub/shared-voices/{id}` · `POST /api/hub/shared-voices/{id}/delete-sync` | Remove the Hub master and exact managed fleet copies / retry pending removals |
| `GET /api/hub/shared-voices/{id}/audio` | Stream the canonical authenticated reference clip |
| `POST /api/hub/transcribe` | Multipart audio transcription routed to a free Voice Studio that has the selected Whisper model cached |
| `POST /api/hub/transcription/jobs` | Stream a multi-file episode transcription batch into the persistent fleet queue |
| `GET /api/hub/transcription/jobs` · `GET /api/hub/transcription/jobs/{batch}` | List batches/lifetime totals or read chapter-level status |
| `GET /api/hub/transcription/jobs/{batch}/items/{index}/artifact` | Download a verified completed SRT through Hub authentication |
| `DELETE /api/hub/transcription/jobs/{batch}` · `POST /api/hub/transcription/jobs/{batch}/retry` | Cancel unfinished chapters or retry failed/interrupted chapters only |
| `POST /api/hub/transcription/jobs/clear` · `POST /api/hub/transcription/jobs/{batch}/clear` | Permanently clear terminal transcription history and its Hub-local input/SRT files; active work is refused |
| `POST /api/hub/chat/jobs` | Submit visual or motion prompts as local worker packs of up to 10 scenes |
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
| `GET /api/hub/resources` | Host memory/CPU + per-studio process stats |
| `GET /api/hub/memory` | Read model-memory policy, loaded-model state, friendly process title, and reachability for every model-hosting Studio |
| `PUT /api/hub/memory-policy` | Apply `{mode, studio_ids?}` using `performance`, `balanced`, `memory_saver`, or `immediate` |
| `POST /api/hub/memory/release` | Release idle model/accelerator memory on selected Studios; returns one result per worker |
| `GET /api/hub/memory-admission` | Read catalog, Hub-default, and effective total/free RAM floors for locally brokered Image, Voice, Music, and Video models |
| `PUT /api/hub/memory-admission` · `DELETE /api/hub/memory-admission?model=...` | Save `{model, min_total_memory_gb, min_free_memory_gb}` or reset one model to its visible Hub default |
| `GET /api/releases` | Current Hub version and complete release details read from the shipped changelog |
| `GET /api/hub/summary` | One-shot dashboard payload (studios + resources + queues) |
| `POST /api/hub/studios/{id}/start` | Start a local studio (via Pinokio's `pterm` CLI) |
| `GET` / `POST /api/hub/maintenance/studio-versions` | Read saved or rescan running/latest Studio versions and reachability |
| `POST /api/hub/maintenance/updates` | Start a drained, sequential rolling update |
| `GET /api/hub/maintenance/updates/{id}` | Follow rolling-update progress and health verification |
| `GET /api/hub/maintenance/generation-installs` | List explicit fleet generation-install jobs |
| `POST /api/hub/maintenance/generation-installs` | Start generation installation for all registered sibling Studios, or `{"studio_ids": [...], "local_only": true}` for a peer-local request |
| `GET /api/hub/maintenance/generation-installs/{id}` | Follow dependency installation and verification progress |
| `POST /api/hub/studios/{id}/stop` | Stop a local studio |
| `GET /api/hub/access` | Shareable LAN/Tailscale URLs (+ the token, loopback only) |
| `ANY /studio/{id}/{path}` | **Gateway** — proxies to that studio's API (streams/SSE included) |
| `POST /api/hub/registry/reload` | Re-read `studios.json` without restarting |
| `GET /api/hub/metrics?minutes=60` | Time-series (host memory/CPU + per-studio RSS, 15s samples, 24h) |
| `GET /api/hub/watchdog` · `POST /api/hub/studios/{id}/watchdog` | Auto-restart-if-down per studio (`{"enabled": true}`; 2-min cooldown, auto-off after 5 failed revives) |
| `POST /api/hub/broadcast/download` | `{repo, studios?}` — start and durably track the same model download on many studios |
| `GET /api/hub/broadcast/downloads` | Read retained per-worker bytes, percent, speed, ETA, completion, and reachability |
| `DELETE /api/hub/broadcast/downloads/{run}/studios/{studio}` | Cancel one active worker download without stopping the other targets |
| `GET` / `POST /api/hub/model-baselines` | Read or enable the site-local required-model baseline for Voice Studio workers: Whisper Tiny, Kokoro 82M, VibeVoice Realtime 0.5B 4-bit, and Fish Audio S2 Pro 8-bit |
| `POST /api/hub/model-baselines/reconcile` | Recheck every required model on every registered Voice Studio now; cache anything missing and retain offline model targets for automatic retry |
| `POST /api/hub/broadcast/env` | `{key, value, studios?}` — set an env var in studios' ENVIRONMENT files (restart to apply) |
| `POST /api/hub/jobs` | **Swarm Batch** — submit a batch (envelope below) |
| `POST /api/hub/execution-assets/voice-references` | Temporarily stage one checksum-bound GenStudio customer voice reference for a site attempt |
| `DELETE /api/hub/execution-assets/voice-references/{asset_id}` | Remove a staged private reference early; automatic expiry remains the fallback |
| `GET /api/hub/jobs` · `GET /api/hub/jobs/{batch}` · `DELETE /api/hub/jobs/{batch}` | Track / cancel batches; terminal history remains visible across Hub restarts |
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

### Terminal Image result (reproducible evidence)

Completed Image items include a path-free `terminal_result` with the stable Hub
artifact URL plus `width`, `height`, `steps`, `resolved_seed`,
`runtime_revision`, `worker_id`, and `machine_id`. `machine_id` is Studio Hub's
registered physical-machine identity, so it joins directly to the schema-v2
capability snapshot and never depends on parsing the worker name. For completed
items the legacy top-level `seed` also contains the resolved seed, preserving
existing GenStudio consumers while making a worker-selected random seed
reproducible.

The Hub copies only these allowlisted facts from the authenticated worker. It
does not expose a worker path, prompt, credential, or GenStudio customer job or
attempt identity.

### Terminal voice result (billable audio)

Completed job items include a path-free `terminal_result` envelope. For a
validated WAV voice result it contains `asset_id`, Hub-relative `artifact_url`,
`media_type` (`audio/wav`), `format`, `bytes`, `sha256`,
`audio_duration_ms`/`audio_duration_s`, `sample_rate_hz`, `channels`, and
`runtime_s`. `runtime_s` is the worker processing time; it is deliberately
separate from decoded audio duration. `duration_s` remains temporarily as a
backward-compatible alias for `runtime_s` only.

Voice Studio local jobs also include `resource_usage` using the versioned
`voicestudio.resource-telemetry` v1 schema. Studio Hub retains the worker's
observed host-memory, pressure, swap, process-tree RSS, and MLX peak evidence in
the durable batch and displays it on the exact per-item Jobs row. The Hub does
not calculate a second value or infer a model RAM minimum from one attempt; it
whitelists and relays the worker-owned measurement for fleet qualification.

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

### Controller-managed enrollment repair API

Studio Hub 2.9.0 implements Controller-managed repair for an already registered
remote Agent whose saved location identity or parent Controller is wrong. It is
not a new-enrollment path: it does not use the permanent enrollment code, re-key
the Controller registry, merge machines, or infer identity from a hostname or
display label.

The six repair endpoints are:

| Endpoint | Authorization and purpose |
|---|---|
| `GET /api/hub/enrollment-repairs/eligibility` | Controller owner read of every registered remote Mac's exact eligibility and current request state |
| `POST /api/hub/enrollment-repairs` | Controller owner creates or adopts a stable-order one-machine/batch request |
| `GET /api/hub/enrollment-repairs/{batch_id}` | Controller owner reads sanitized durable batch and per-machine state |
| `POST /api/hub/enrollment-repair/apply` | Exact-current fleet-token service dispatch to the bound Agent and private source |
| `POST /api/hub/enrollment-repair-tickets/redeem` | Exact-current fleet-token callback that atomically consumes one bound ticket on the Controller |
| `GET /api/hub/enrollment-repair/status/{request_id}` | Exact-current fleet-token, source-bound read of sanitized Agent terminal evidence |

The three owner routes accept only Controller loopback or that Controller's
valid remembered owner-browser session. They do not accept the unique Hub
token, the shared fleet token, a recovery credential, or an owner session from
another Hub as initiation authority. Existing same-origin browser-write
protection still applies. Each of the three service routes independently
requires the exact current `X-Hub-Token` fleet header, a private direct client,
the expected request source, and no cookie, `Authorization`, query credential,
forwarded-host trust, redirect, or environment proxy fallback.

A successful Agent apply performs one atomic replacement of
`controller_settings.json` and changes exactly these five keys:

- `role`
- `site_id`
- `site_name`
- `controller_id`
- `parent_controller_url`

Every other settings key is retained. The Controller stores only SHA-256 ticket
and fleet-token digests. A random target-bound ticket is single-use and becomes
ineligible for first redemption at its persisted absolute deadline,
approximately 120 seconds after issuance. Redemption revalidates the request,
ticket, expiry, registry key/host/address, direct source, Controller
role/site/name/ID snapshot, and exact current fleet credential in one durable
transaction. The Agent journals uncertainty before callback and never resumes a
forward settings write during restart recovery.

Fresh batch dispatch is sequential. If an exact terminal result does not arrive
within the bounded 15-second scheduling interval, that Mac becomes
`confirmation_pending`; it is parked without extending or revoking any accepted
claim, and the next eligible Mac continues. A parked target receives no second
ticket. The Controller can later adopt source-bound `complete`, `never_applied`,
or `needs_review` status. It marks completion only when the returned identity is
exact, then invalidates that peer cache and wakes the existing managed-release
reconciler for the same stable registry key.

Before dispatch, the Controller reads the registered Agent's
`repair_schema_version` through one pinned, bounded, no-redirect private socket
request. An absent, old, malformed, mismatched, or unreachable capability fails
closed as **Hub update required** and is never sent `/apply`. Repair never starts
an Agent update: update that Mac through its ordinary manual or overnight Hub
update path, then retry. It never updates Image Studio, Voice Studio, or models.

Repair preserves the permanent enrollment code and its use count, both
fleet-token files byte-for-byte, unique Hub token, owner password and sessions,
Controller registry key and every Studio endpoint, labels and hardware profile,
whole-machine and per-Studio Off flags, jobs and artifacts, shared voices,
models and caches, catalog/download state, updater settings/history, and
`release_reconciliation.json` including release intent, activation/job IDs,
machine/component/child operation rows, retries, catalog evidence, and leases.
Capability schema remains `studiohub.site-capabilities` version 3.

Deleting and re-enrolling is an emergency procedure only. It is especially
unsafe during an active degraded managed release: deleting a registry key does
not clean its durable release machine row, child operation/job IDs, retry
counters, or evidence. Before emergency re-enrollment, those durable rows need
a separate reviewed cleanup or migration. Enrollment repair never performs that
cleanup implicitly.

## Dashboard

- **Overview** — group studios **All / By app / By machine**, in **Cards** or **List** view
  (your choice is remembered). Start/stop and auto-restart toggles per studio.
- **Models** — every model across all machines, deduped, with an **Availability** column
  showing exactly which machines have each one downloaded. "Downloaded" means *cached on at
  least one machine* — a model can be on your media server but not this Mac.
- **Resources** — this Mac's unified-memory bar + hour sparkline, per-studio process memory.
- **Jobs / Assets** — Swarm Batch submit + progress, durable Image/Voice/Render
  batch search by Hub ID, label, or model, per-attempt local failure/resource
  evidence, and a searchable asset ledger.
- **Remote** — reachable URLs + token, **Discover & Add** a machine, and a permanent
  **Registered machines** list. Registration starts with a reusable hardware
  profile and suggested stable ID; profiles remain editable later. Each Studio
  has its own new-job pause/resume switch, while the machine switch remains the
  master control. **Automatic startup across the fleet** audits every sibling's
  launchd service and watchdog, with per-Studio and install-all repair controls.

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

The existing Hub token is still required for GenStudio, scripts, API clients,
peer Hubs, and recovery. It is shown plainly in a Controller's **Remote →
Machine mode** settings, alongside the Agent registration code and fleet token;
copy the Hub token into GenStudio when connecting that Controller. A local
dashboard or an owner-authenticated Tailscale browser can read it. Replacing
the owner password signs every remembered browser out immediately.

## Machine modes and fleet setup

For a clean Apple-silicon Mac, the fleet SSD can install Pinokio, Studio Hub,
Image Studio, Voice Studio, both generation environments, the RAM-qualified
model set, SHA-256-bound shared voice references, and independent Pinokio
startup settings before performing the Agent join. Staging is additive: unchanged packages
are skipped and an offline Studio cannot erase previously staged payloads.
Synchronize the canonical kit with `python3 tools/sync_ssd_bootstrap.py
--volume /Volumes/NAME-OF-SSD`, then stage model packages separately with
`python3 tools/studio_models.py stage` and follow
[`SSD-COPY-README.md`](SSD-COPY-README.md). The permanent Controller registration
code is prompted securely on the new Mac and is never stored on the SSD.

On an 8 GB Mac, normal SSD restore uses an exact Voice allowlist: Qwen3-TTS
0.6B Base for cloning plus `mlx-community/whisper-large-v3-turbo` for its
quality check. The preset-only CustomVoice checkpoint and unrelated Voice
generators are not copied. Run **4 Repair Studio Startup.command** on an
existing Mac if Hub waits for Image/Voice or Pinokio and launchd both try to
own a Studio; the repair changes only `ENVIRONMENT` startup keys and does not
touch processes, services, models, jobs, or enrollment.

Open **Remote → Machine mode** and choose the role this Mac should perform. The
mode is always visible: **Standalone** is orange, **Agent** is green, and
**Controller** is red. The machine name is a friendly display name only; Site ID
and Hub ID remain the stable routing identities.

### Controller

Set a Controller machine name, Site ID, Site display name, and Hub ID. Saving
Controller mode automatically creates a permanent registration code and fleet
token when either is missing. The same Controller panel plainly displays the
Hub token for GenStudio, the registration code for Agents, the fleet token, and
the owner sign-in password. The registration code remains reusable until the
owner rotates or revokes it; it is not a replacement for the Hub token.

### Agent

On each worker Mac, choose **Agent** and enter:

1. Confirm the automatically detected hardware profile. A friendly machine name
   is optional; Hub uses the Mac hostname when it is blank.
2. The Controller's private LAN/Tailscale address. Paste an HTTP/HTTPS URL,
   Tailscale IP, or MagicDNS hostname; direct HTTP addresses without a port use
   47873 automatically.
3. The Controller's permanent registration code.

The Agent receives its site identity and fleet token automatically, retains them
locally, and registers its Studio endpoints on the Controller in that same step.
Do not paste the fleet token or add the online Mac again on the Controller during
normal Agent enrollment.

### Standalone

Choose **Standalone** for a local-only Hub. It keeps the same local Studio
controls and does not require a Controller address or fleet enrollment.

The enrollment code contains at least 256 bits of entropy and remains reusable
until an owner explicitly rotates or revokes it. The controller's claim database
stores only its SHA-256 hash and use metadata; a separate mode-0600 owner-private
file keeps the value available to Reveal after a Hub restart, like the Hub and
fleet credentials. Claims are accepted only from loopback, private LAN, or
Tailscale source addresses. The worker join endpoint accepts only a
credential-free address that resolves entirely to loopback, private LAN, or
Tailscale IPs. It is callable from that Mac itself or an owner-authenticated
remote browser; a fleet token alone cannot reconfigure a Hub. Failed network or
validation steps do not change local settings, and a failed local commit is
rolled back as far as the filesystem permits.

Codes and fleet credentials are sent in request bodies/headers, never URLs.
The dashboard masks them by default and does not put them in localStorage. The
existing role, PostgreSQL-shadow, and manual credential controls remain under
**Technical recovery settings · rarely needed**.

## Fleet startup-service control

Open **Remote → Automatic startup across the fleet → Check startup**. The
controller asks each reachable peer Hub to inspect only its own Mac. Each row
reports whether the Studio app exists, its trusted `install_service.sh` is
available, both launchd plists exist, and both the server and watchdog labels
are loaded. Older peer Hubs report **Hub update needed** instead of guessing.

**Install** or **Repair** runs that sibling's existing idempotent installer on
the target Mac; **Install missing** performs the same operation sequentially.
The Hub validates that the installer is a regular file inside the registered
Studio app, drains new Hub dispatch with maintenance mode, and refuses the
operation if that Studio still has Hub-tracked active work. Installation can
restart the Studio as launchd takes ownership, so direct jobs started outside
the Hub should also be allowed to finish first. No startup service is installed
automatically merely by opening or refreshing the audit.

Legacy Music, Chat, Video, and Render apps are deliberately absent from this
audit. Studio Hub does not register, monitor, update, start, or stop them, and
an old peer payload cannot put them back into the controller UI. No cleanup
action is offered; existing folders and data remain untouched for manual handling.

Hub, Image, and Voice automatic schedules use named executable wrappers
(`studiohub-updater.sh`, `imagestudio-updater.sh`, and
`voicestudio-updater.sh`). The first restart after updating reconciles a legacy
raw-Python LaunchAgent automatically. macOS Background Activity therefore has
identifiable production updater entries.

## The fleet: remote specs + remote control

Health and models come over HTTP, but a machine's **RAM/specs** and
**start/stop** need OS-level access on that machine. So run a **Studio Hub on
every Mac** — each Hub is the authority for its own machine, and your primary
Hub aggregates them.

Setup (once): use **Set up this Mac** for normal controller/agent enrollment.
The manual shared-token process below remains available under Advanced settings
for recovery and older installations.

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

Schema `studiohub.site-capabilities`, version `2`, includes controller/site
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

### Unattended reliability alerts

The dashboard alert center records Studio outages and recoveries, peer Agent
Hubs that remain unreachable for three consecutive resource checks, repeated
worker restart rates reported by a Studio, and genuine GenStudio lease expiry
while local work is still queued or running. Webhook and desktop delivery use
the existing optional alert settings. Each condition is edge-triggered so a
persistent outage does not create an alert storm, and recovery is reported once.

These alerts are read-only operating signals. They do not restart workers,
claim or transfer GenStudio jobs, or replace SQLite site-local scheduling.

## Release discipline

Every committed Studio Hub change increments `VERSION` and adds matching
changelog and dashboard **What's New** entries, including documentation-only
updates. See [`RELEASE_POLICY.md`](RELEASE_POLICY.md); the test suite verifies
that all three release metadata sources identify the same newest release.

## Multiple Macs (registry)

Every Mac keeps running its own studios; one location controller coordinates the
workers registered at that site. GenStudio will choose between locations:

1. On each other Mac, install Image and Voice and use **Join an existing
   location** to enroll its Hub as an agent. Hub detects the Mac model, Apple
   chip, and unified RAM automatically; the Controller registers the production
   Studio endpoints without a hardware-profile choice. A manual profile remains
   available only when detection cannot match future hardware.
2. For an older or offline Hub only, open **Remote → Add another Mac's studios**
   on the Controller. Online discovery reads the Mac family, chip, and RAM from
   its peer Hub; offline setup lets you choose the profile and edit the suggested
   stable ID. Two fallback paths remain:
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
M1/M3 memory classes. Profiles classify hardware for routing and operating-cost
records; their historical planned-unit values are inventory targets and never
limit how many machines can register. Use **Add another hardware profile to this
list** for a future machine class. Existing machines can be assigned or corrected
directly in **Registered machines** without re-registering their Studios.

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
- **Audio first** — when a worker frees up and audio work is queued, it takes
  audio before image. The image job in flight is never interrupted, so the
  longest an audio item waits is one image generation. No Mac is reserved or
  held idle: audio only claims a worker that meets its model's declared
  `min_unified_memory_gb` and has the model downloaded, and every worker audio
  cannot use takes the queued image job in the same scheduler pass. Each time
  audio goes ahead of ready image work the Hub logs one `audio priority` line
  naming the machine and both batches.

## Swarm Batch

Submit N independent prompts; the Hub queues them and free studios of that
modality pull the next item (work-stealing — a second machine in `studios.json`
automatically joins the pool). Transient connection and gateway failures get a
bounded 30-minute self-healing window with progressive backoff; genuine
generation failures retain three attempts, while authentication and validation
failures stop immediately with their original reason. Every
result lands in the asset ledger with full provenance (prompt, model, resolved
seed, params, batch id) — reproducible by construction.

For an operator-run qualification, open **Jobs → Swarm Batch**, choose Image or
Voice and a downloaded model, and add a descriptive batch label. Clone-capable
Voice models also show synchronized entries from **Voices**; the Hub routes the
batch only to workers that hold the selected reference and carries that library
entry's language and reviewed transcript automatically. Image art styles remain
plain prompt instructions, so one labeled batch can contain a different style
on each line without introducing a separate style service or cloud dependency.

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
`lease_expires_at`, `site_id`, and `operation` (plus optional model/voice
revisions). Studio Hub hashes the idempotency key, rejects stale externally
issued fences, and returns the same local batch for an exact replay. GenStudio
renews the lease through `POST /api/hub/executions/leases`. An expired lease
cannot be revived: unfinished work is cancelled instead of requeued after a
Hub restart, and late worker output is not adopted into the asset ledger.
Studio Hub never issues or increments the global fence:

```json
{
  "genstudio_job_id": "job_01...",
  "genstudio_attempt_id": "attempt_01...",
  "idempotency_key": "stable-non-secret-attempt-key",
  "fencing_token": 42,
  "lease_expires_at": "2026-07-23T10:05:00+00:00",
  "site_id": "phnom-penh-1",
  "operation": "tts"
}
```

The **fleet memory governor** uses live host telemetry from each connected peer
Hub before local or remote dispatch. Open **Models -> RAM admission guard** to
see the worker catalog value, Hub default, effective minimum total RAM, and
minimum currently-free RAM for every local model. The effective floors can be
overridden and reset without restarting workers or interrupting active jobs.

A machine below the effective total-RAM floor is skipped for a compatible
larger Mac. A machine below the live free-RAM floor waits (visible as
`governor_note`) until pressure clears or another worker takes the item. Model
download `size_gb` is never used as a RAM estimate. FLUX.2 Klein 4B MLX 4-bit
defaults to the fleet-qualified 8 GB total / 2 GB free policy even though its
conservative ImageStudio catalog currently says 16 GB. Qwen3 0.6B standard
voice defaults to 8 GB total / 3.2 GB free for a safe cold load.

Lowering a floor is an explicit operator choice and can increase memory-failure
risk. A worker MemoryGuard remains the final authority where implemented; a
memory refusal waits/rebalances without consuming an attempt. Physical-machine
leases prevent sibling Studios from overlapping heavy work, repeated connection
failures temporarily pause that Mac. Hosted models are not accepted by Hub.

## Chat prompt packs

Chat work uses a saved queue separate from media generation. Each item is one
local LLM request containing at most 10 scenes. One eligible Chat Studio leases
one pack at a time. This is an adaptive wave size,
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
- The token is auto-generated into `.hub_token` (gitignored). See it in a
  Controller's **Remote → Machine mode** settings on the Hub machine or in an
  owner-authenticated Tailscale browser.
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

# All downloaded image models
curl "http://localhost:47873/api/hub/catalog?modality=image&downloaded=true"

# Memory picture
curl http://localhost:47873/api/hub/resources

# Save the default local-backup policy to every reachable Mac
curl -X PUT http://localhost:47873/api/hub/storage-policy \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"retention_days":30,"max_gb":80}'

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
