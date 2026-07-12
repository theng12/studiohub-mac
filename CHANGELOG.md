# Changelog — Studio Hub KH

All notable changes to Studio Hub KH (the control plane for the KH Studio family) are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change to the Hub API, DB schema, or config. Re-install / migrate.
- **MINOR** (1.1.x → 1.2.x) — new feature, endpoint, or dashboard tab. **Update** from the Pinokio sidebar (restart the service if you run it as a startup service).
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak. **Just Update.**

> Entries before 1.16.0 are condensed summaries reconstructed from git history — this changelog began at 1.16.0.

---

## [1.32.3] — 2026-07-13

### Fixed — remote workers hidden by stale Studio authentication

- Protected requests to a remote Studio now travel through that machine's connected peer Hub. This preserves fleet authentication while avoiding a stale in-memory Studio token that previously made fully cached models look absent and excluded those workers from Chat packs.
- Remote catalog and transcription inventory calls now reject HTTP errors instead of caching a `401` response as an empty model inventory.
- Model downloads and fleet Hugging Face settings use the same peer-authority route, with direct Studio access retained when no connected peer Hub is available.

145 tests.

## [1.32.2] — 2026-07-13

### Fixed — Jobs refresh independently of the live summary stream

- Chat and transcription rows now refresh from their own endpoints every three seconds while Jobs is visible. A delayed or stalled summary stream can no longer freeze pack progress until the user switches tabs.
- Returning to the Hub window triggers an immediate Jobs refresh, while the existing in-flight guard prevents overlapping requests.

144 tests.

## [1.32.1] — 2026-07-13

### Fixed — restart-honest Hub version

- `/api/version`, health, summary, and update status now report the version loaded when the Hub process started. Pulling a newer `VERSION` file without restarting can no longer make stale backend code claim the new release is already active.

143 tests.

## [1.32.0] — 2026-07-13

### Changed — visible LLM workers and oldest-episode priority

- Overview now includes Chat and transcription leases in its working state. Active Chat cards say **LLM working** and show the current episode and pack instead of looking idle while they produce prompts; the header and filter now use the inclusive **Working** label.
- Chat scheduling now fills the oldest runnable episode across every compatible free Chat Studio before leasing packs from newer episodes. A newer batch may still use a server that cannot run the older batch's model, avoiding needless idle capacity.
- Chat batch status is now `running` only while a pack is actually active. A batch with completed work plus queued packs correctly says `queued` instead of showing `running · 0 running`.
- Jobs now refreshes Chat alongside the other live queues and shows priority position, visible active worker/pack rows, elapsed time, attempt `N/3`, automatic-wait reasons, and both failure text and missing scene IDs without requiring expansion.
- A busy Chat Studio that temporarily cannot answer the 3-second health poll during synchronous inference now remains **LLM working** instead of flapping down/up and flooding the alert bell. It becomes down normally if health still fails after the lease releases.
- Automatic behavior remains bounded and lossless: incomplete or transiently failed packs retry up to three attempts, preserving successful scene results; **Retry missing** is the manual recovery after those attempts are exhausted.

142 tests.

## [1.31.0] — 2026-07-13

### Fixed — version comparison + honest update outcome; preflight sorting

- **A peer Hub newer than the cached "latest" no longer shows "update available → &lt;older version&gt;".** Two causes: the primary cached the published `latest` for 6h (so it lagged after a push), and the comparison used exact `==`. Now **rescan force-refreshes `latest`** (`POST /api/hub/maintenance/hub-versions`), and the UI compares **numerically** (`verGte`), so a Hub at or above latest reads "up to date".
- **A remote Hub update that restarts but comes back on the *same* version is now reported as `failed`** ("restarted but still on vX — update didn't apply; git pull or deps failed on that Mac"), instead of a misleading "complete". `_update_hub_one` only reports success when the version actually advances; a timeout says it's still on the old version.
- **Fleet preflight is sortable** by Machine / App / Status / Version (remembered).

## [1.30.0] — 2026-07-13

### Added — clear finished Chat prompt batches

- Chat prompt packs could only be cancelled or retried — finished/errored ones piled up with no way to remove them. Added `chat_jobs.remove_batch(id)` / `clear_terminal()` (drop finished batches from memory + the `chat_batches` DB, keeping running ones) with `POST /api/hub/chat/jobs/{id}/clear` (409 if still running) and `POST /api/hub/chat/jobs/clear`. The Jobs tab gains a per-batch **Clear** on any terminal batch and a **Clear finished** button for the section. 137 tests.

## [1.29.0] — 2026-07-13

### Added — persistent fleet versions, rescan, and a preflight check legend

- **Rescan versions** on the "Studio Hub updates (agent Macs)" card: `POST /api/hub/maintenance/hub-versions` queries each agent Mac's Hub `/api/version` and shows its current version, when it was last checked, and whether it's up to date vs the latest. Results are **persisted to `fleet_versions.json`**, so the last-known version survives a Hub restart and never just disappears (unreachable machines keep their cached version). A completed fleet Hub update also refreshes the cache.
- **Fleet preflight** now shows **"last scanned … ago"** and persists its snapshot across restarts, plus a collapsible **"What the checks mean"** legend explaining health / port / capability contract / fleet authentication / models / generation engine / update workflow / disk space / memory, and the badge colours. 136 tests.

## [1.28.0] — 2026-07-13

### Fixed — studio updates: version column + fleet-auth 401 no longer blocks

- **Studio preflight now shows each studio's `version`** (fetched from the public `/api/version`, so it shows even when auth is stale) — a column in the Fleet preflight table, like the fleet Hub update.
- **A studio returning 401 to the fleet token is now a *warning*, not a *block*.** Root cause: studios cache their fleet token at startup, so a studio that started before the token was set/synced rejects the Hub's token (HTTP 401 on `/api/catalog`) — showing "Blocked" even though the token is correct on that machine. But the **update runs via the machine's own Hub (not the studio's API) and restarts the studio, which reloads the token** — i.e. updating *fixes* the 401. Blocking the update on that check was backwards. Preflight now marks it `warn` (studio stays eligible) with a detail explaining a restart/update resolves it. Genuinely blocking problems (down, port conflict, unreachable, broken API contract) still fail. 136 tests.

## [1.27.0] — 2026-07-13

### Added — set one Hugging Face token across the fleet

- New **"Set Hugging Face token on all studios"** card (Models tab) and `POST /api/hub/broadcast/hf-token`. Paste a token once and it's pushed to every online studio's own `POST /api/settings` — for gated models and higher download rate limits. It's a **partial** settings update (only `hf_token` is sent, so each studio's other keys — e.g. cloud API credentials — are preserved), sent over Tailscale, and the token is **never stored in the Hub** (pass-through; the response never echoes it). Studios without a settings endpoint (Render) report a clean skip; offline studios pick it up on the next run. 135 tests.

## [1.26.0] — 2026-07-12

### Added — Distribute a model to the fleet (UI for broadcast download)

- The Hub already had `POST /api/hub/broadcast/download` (fans a model download out to studios, each pulling from Hugging Face itself), but there was no way to use it from the dashboard. Added a **"Distribute a model to the fleet"** card on the Models tab: pick a **studio type** (default chat) + a **Hugging Face repo** (autocompleted from the catalog) and every **online** studio of that type starts downloading it — no copying files between machines. Per-studio results are shown; offline studios are skipped (re-run when they're up). The fan-out hits each studio directly, so it doesn't need a peer Hub — just a reachable studio.

## [1.25.6] — 2026-07-12

### Added — Remove a single studio from a machine

- You could only remove a **whole machine**, so a studio type that isn't installed on a Mac (commonly music/video, pre-registered by "Add manually" which defaults to all modalities) was stuck showing "down" forever. Added a small **✕ on each studio pill** (Remote tab) that prunes just that studio: `DELETE /api/hub/registry/studios/{studio_id}` + `registry.remove_studio(id)`. Local (default) studios are protected (400). A pruned studio reappears only if it's actually running the next time you Refetch, or if you re-add it manually. 130 tests.

## [1.25.5] — 2026-07-12

### Changed — Clear message when a peer Hub is too old to self-update

- Remote Hub update requires the peer to already run ≥1.25.4 (the version that added the `self-update` endpoint) — the bootstrap of any self-update system. When the primary hits a peer that predates it, the fleet Hub update now reports a clear, actionable status (*"Hub vX is too old for remote update — update it once from the Pinokio sidebar on that Mac"*) instead of a raw 404.

## [1.25.4] — 2026-07-12

### Added — Remotely update the Studio Hub on agent Macs

- The fleet update system only covered **studios** (registry entries); the **Hub itself** had to be updated locally on each Mac. Added a **"Studio Hub updates (agent Macs)"** card on the Remote tab that updates the Hub across the fleet: the primary tells each reachable peer Hub to run its own `update.js` (git pull + restart), waits for it to come back on its startup service, and reports per-machine `from → to` version. Peers already on the latest version are **skipped** (no needless restart); unreachable Hubs are reported as such.
- New endpoints: `POST /api/hub/maintenance/self-update` (on every Hub — runs its own `update.js`; loopback or fleet-token authenticated), and `GET`/`POST /api/hub/maintenance/hub-updates` + `GET /api/hub/maintenance/hub-updates/{job_id}` (the primary's orchestrator). Peers are updated concurrently; the local Hub is excluded (update it from the Pinokio sidebar). 127 tests.

## [1.25.3] — 2026-07-12

### Added — Refetch a registered machine's studios

- Adding a machine (Discover & Add) only registered the studios that were **online at that moment**, with no way to re-detect studios that started later. Added a per-machine **Refetch** button and a **Refetch all** button on the Remote tab that re-probe the machine's host for the studio family ports and register any that have since come online. Reuses the existing `POST /api/hub/registry/discover` (which already adds only new host:port entries), so it's non-destructive. Frontend-only.

## [1.25.2] — 2026-07-12

### Changed — Adaptive, fair fleet waves

- Clarified that 10 scenes is the per-worker Chat pack size, not a batch ceiling. Seventy scenes can fan out to seven capable servers; 200 scenes with five capable servers continue automatically over four waves. Chat batches support up to 5,000 scenes.
- Chat and transcription queues now take fair round-robin turns across episodes. A large episode still fills otherwise-idle compatible workers, but it can no longer monopolize every subsequent wave while newer episodes wait.
- A queued episode render now reserves an eligible render Mac when it becomes free. Existing image, audio, transcription, or Chat work is never interrupted; priority applies only between jobs.
- Kept model-aware routing, cached-model checks, per-machine heavy-work leases, memory safeguards, retries, restart recovery, and natural work stealing across faster and slower Macs.

124 tests.

## [1.25.1] — 2026-07-12

### Changed — Jobs tab: order, sorting, pagination, sticky expand

- **Generation batches** (the Swarm submit form + list) moved to the **top** of the Jobs tab, above the Chat and Episode-transcription queues, so the completed jobs sit below the thing you actually submit.
- The **Generation** and **Episode-transcription** batch lists are now **sortable** (newest / oldest / status / longest processing) and **paginated** (12 per page with prev/next) — the transcription list had grown to 100+ finished batches with no way to page through it.
- **Fixed:** an expanded batch's detail (`<details>`) no longer collapses on every live refresh. Open state is preserved per batch id until you collapse it yourself.

Frontend-only.

## [1.25.0] — 2026-07-12

### Added — Saved Chat Studio prompt packs

- Added a restart-safe Chat queue where every worker leases one pack of up to 10 stable scene IDs. Ten model-capable Chat Studio servers can process up to 100 visual or motion prompts in one fleet wave.
- Added model-aware fleet dispatch, one active pack per physical machine, oldest-first scheduling, bounded retries, cancellation, active-batch idempotency, and restart recovery.
- Valid results from incomplete local-model responses are saved immediately. Automatic and manual retries request only missing scene IDs instead of discarding or regenerating successful prompts.
- Added authenticated submit/list/status/cancel/retry APIs and Jobs-tab visibility for project, episode, visual versus motion kind, batch/pack IDs, workers, scene progress, missing IDs, attempts, duration, and errors.
- Full prompt text is returned only by a specific batch status request; the frequently-polled batch list stays compact for long episodes.

121 tests.

## [1.24.1] — 2026-07-12

### Added — Restart-safe episode transcription queue

- Added a dedicated streaming multipart transcription batch API that persists chapter audio, distributes work oldest-first across every ready model-capable Voice Studio, and naturally gives faster Macs more chapters as they become free.
- Added physical-machine workload leases, one active transcription per Voice Studio, bounded transient retries, idempotent active-batch submission, restart recovery, safe cancellation, failed-item-only retry, and verified non-empty local SRT artifacts.
- Added episode transcription batches to Jobs with project/episode context, live chapter counts, worker and task IDs, processing time, errors, SRT downloads, retry/cancel controls, lifetime totals, and 1/3/7/15/30-day retention cleanup.
- Kept `POST /api/hub/transcribe` compatible by routing its one-file request through the same durable queue and returning the existing Voice Studio response shape.
- Hardened uploads with streamed 1 MiB chunks, per-file/batch size limits, a media extension allowlist, strict item/filename validation, generated storage names, and no client-controlled destination paths.

112 tests.

## [1.24.0] — 2026-07-12

### Added — Fleet-wide Whisper inventory

- Voice Studio transcription availability is now authenticated, aggregated across every online Mac, and included in the unified catalog as the `transcription` modality.
- Added `GET /api/hub/transcription` with recommended model, cached machines, ready machines, and fleet endpoint counts for Story Studio's Subtitles screen.
- Added an authenticated multipart transcription gateway that queues for a free compatible Voice Studio, so clients no longer upload audio to individual Macs.
- The Models tab now includes transcription models alongside image, voice, chat, and render inventory.
- Catalog failures serve the last good Whisper snapshot, matching the existing resilient model-catalog behavior.

## [1.23.4] — 2026-07-12

### Added — Asset quick sorting

- Added one-click Newest, Oldest, Name, Type, Studio, and Model sort buttons to Asset Ledger. Newest is the default and the selected choice is remembered.
- Sorting is performed by the ledger query, not by rearranging the newest 100 browser rows, so Oldest and categorical choices select the correct records from full history.
- Asset API sort values use a strict allowlist before reaching the database query.

93 tests.

## [1.23.3] — 2026-07-12

### Fixed — Resource table alignment

- Applied one fixed percentage-based column layout to every per-machine Resource table. Long Studio or machine names no longer shift Status, PID, Memory, or CPU between sections.
- Long values truncate inside their own cell instead of widening a table, while narrow screens retain internal table scrolling.

92 tests.

## [1.23.2] — 2026-07-12

### Added — Resource ordering

- Added a visible remote-machine order control with Online first, Name, and Available memory choices. Online is the default and the selected order is remembered.
- Resource Studio tables now also default to active/online workers first, then offline workers, while retaining Memory and Name choices.
- Machine-group sections prioritize machines with an online Studio and show clearer online, reachable-without-Hub, and offline states.

92 tests.

## [1.23.1] — 2026-07-12

### Fixed — Overview list alignment

- Gave every Overview list row the same stable column contract. Remote link-only rows now reserve the same action width as rows with Restart and Stop controls, keeping Machine, Status, Memory, and Version aligned with their headers.

92 tests.

## [1.23.0] — 2026-07-12

### Changed — modern fleet workspace

- Rebuilt the dashboard's visual hierarchy around a responsive fleet sidebar, live workspace masthead, elevated control surfaces, clearer status color, modern forms, and more readable tables.
- Added focused titles and guidance for every workspace while preserving all scheduling, lifecycle, security, maintenance, and reporting behavior.
- Added responsive navigation and dense-table handling for small screens, plus restrained transitions when changing workspaces.

92 tests.

## [1.22.1] — 2026-07-12

### Fixed — Render Studio dashboard visibility

- Added the `render` capability to Overview grouping, machine discovery, model filters, and fleet statistics. The Hub API already detected Render Studio, but the dashboard's older five-modality list hid it.
- Added a dashboard regression test so newly registered Render Studio workers remain visible.

92 tests.

## [1.22.0] — 2026-07-12

### Added — episode render workers

- Added Render Studio KH as a separate `render` capability on port 47874. It remains distinct from generative Video Studio.
- Added physical-machine, non-preemptive work leases so image and render jobs never overlap on the same Mac. Render batches are considered first when a machine becomes free; active work is never paused.
- Available render workers rank by their reported hardware score, preferring M4 16 GB machines while retaining older Macs as fallbacks.
- Added authenticated streaming storage for immutable render inputs, worker-artifact proxying, checksum metadata, and receipt acknowledgement so retention starts only after Story Studio verifies the returned file.

91 tests.

## [1.21.4] — 2026-07-12

### Added — explicit port and memory preflight

- Fleet preflight now detects duplicate host/port assignments and reports local or peer-Hub available/total memory with a low-memory warning threshold.
- Added a regression test proving duplicate ports block readiness before maintenance.

86 tests.

## [1.21.3] — 2026-07-12

### Fixed — Video Studio lifecycle path

- Corrected Video Studio's default Pinokio folder from the nonexistent `videostudio-mac.git` to `videostudio-mac`. The rolling update rejected the missing folder before launching anything, while Voice, Chat, and Music completed normally.
- Added a regression test requiring every default local Studio launcher folder to exist.

85 tests.

## [1.21.2] — 2026-07-12

### Fixed — Pinokio control from startup-service mode

- Lifecycle and rolling-update commands now invoke bundled `pterm` through Pinokio's bundled Node executable. The startup service intentionally has a minimal macOS `PATH`; relying on `#!/usr/bin/env node` caused detached maintenance commands to exit before reaching Pinokio.
- The rolling job remained safely drained on Image Studio and launched no update while this was diagnosed.

84 tests.

## [1.21.1] — 2026-07-12

### Fixed — rolling-update task launch

- The update route is now asynchronous, so its background rolling-update task is created on FastAPI's event loop instead of a worker thread. The first live 1.21.0 attempt failed safely before launching any Studio update and exposed this boundary.
- Added a route-level regression test that schedules an update through the real ASGI stack.

83 tests.

## [1.21.0] — 2026-07-12

### Added — secured Studio fleet, preflight, and rolling updates

- StudioHub now automatically maintains an owner-only fleet token and forwards it to every protected Studio catalog, generation, asset, recipe, broadcast, and gateway request. Remote Hub authentication also establishes an HttpOnly Studio session cookie.
- Added fleet preflight across health, authentication, capability schema, downloaded models, generation diagnostics, local update scripts, and free disk space.
- Added drained rolling updates: Studios stop receiving new Hub work, active work finishes, each Studio's own mode-aware `update.js` runs one at a time, and the Hub requires the new on-disk version to return healthy. A failed update triggers a normal recovery start and does not prevent later Studios from proceeding.
- Remote Studio updates delegate to the target machine's Hub, preserving machine-local Pinokio control.

### Verification

- 83 backend tests pass, including fleet-token forwarding, maintenance draining, sequential updates, failure containment, and route-level task scheduling. Frontend scripts and all Python modules parse cleanly.

## [1.20.1] — 2026-07-12

### Fixed — dashboard and local-control security

- Hub and fleet tokens are now always stored with owner-only (`0600`) permissions, including existing token files when they are loaded.
- Unsafe browser requests must come from the Hub's own origin. Local scripts remain frictionless, while unrelated websites can no longer use the loopback auth exemption to change Hub settings or control studios.
- Reference-image uploads now stream to disk, reject unsupported formats, enforce a 20 MB limit, and remove partial files after failed uploads.
- Dynamic studio, model, job, asset, alert, machine, and version text is escaped before dashboard rendering; external links are limited to HTTP(S) and open with `noopener`.

79 tests.

## [1.20.0] — 2026-07-11

### Added — per-item webhooks (stream results from a single batch)

- The job envelope now accepts **`itemWebhook`**: the Hub POSTs a small payload the moment **each item** reaches a terminal state, so a client can submit a whole multi-scene render as **one batch** and still receive each result as it finishes — no waiting for the entire batch, no polling. Payload carries `index`, `state`, `machine`/`studio`, `artifact_url`/`artifact_path`, `asset_id`, `duration_s`, `error`, plus a live `done`/`total` tally. Fires at most once per item; skipped for retry-requeued items. The existing whole-batch `webhook` still fires once on completion; they're independent.
- Docs: `STORYSTUDIO_INTEGRATION.md` §6b — the recommended "submit all scenes as one batch, stream results via `itemWebhook`" pattern, which is what makes the Jobs tab show a single **0/120** story-progress line (with per-scene machine tags + ETA) instead of a pile of 1-item jobs.

76 tests.

## [1.19.1] — 2026-07-11

### Fixed — Overview list columns + clearer memory

- The Overview **List** view had a single column that showed the studio's process RAM *or* the version — so on machines with live stats the **version disappeared** and columns looked skewed. **Memory and Version are now separate, always-present columns**, with a header row (Studio · Machine · Status · Memory · Version).
- The **Memory** column now shows the machine's **usable (free) and used** unified memory (e.g. `2.91 GB free · 5.2/17 GB used`) — the meaningful number — instead of a bare per-process figure like "0.04 GB". Each studio's own footprint is still available (tooltip in the list, a sub-line in card view). Machines without a Hub show "—" (no host stats available).

## [1.19.0] — 2026-07-11

### Added — restart, per-machine enable/disable, and alert management

- **Restart a studio** in one click (Overview → Restart, next to Stop). Locally it does a `pterm` stop followed by a delayed start (so the port frees first); remote studios are proxied to their own machine's Hub. New action on `POST /api/hub/studios/{id}/restart`.
- **Enable / Disable a machine in the fleet** (Remote tab toggle). A disabled machine stays registered and monitored, but the broker's dispatch (`_eligible_studios`) skips it, so it takes no new jobs — useful to quiesce a machine before updating or restarting it. Persisted in `machine_flags.json`; new endpoint `POST /api/hub/registry/machines/{machine}/enabled`; the machine's `enabled` flag is surfaced in `/api/hub/resources`.
- **Alert log management:** a **Clear log** button (`POST /api/hub/alerts/clear`) and **Show all / Show less** in the Alerts card, so the log no longer grows unbounded with no way to reset it. The header **🔔 bell is now a dropdown** of recent alerts (with Clear / View all), instead of just jumping to the Remote tab.

74 tests.

## [1.18.0] — 2026-07-11

### Added — job machine tags, live progress/ETA, all-tabs-live, and fleet diagnostics

- **Jobs tab now shows which machine is running each item**, with a per-item **progress %, elapsed time, and ETA**. The ETA is computed from the studio's live progress fraction, falling back to the batch's average completed-item time. The broker captures `progress` / `run_started` per item and `batch_summary` exposes `running_items` + `avg_s`.
- **Every tab updates live.** Overview, Resources, **Jobs**, and **Remote** now render straight from the Server-Sent Events summary (no more switching tabs to refresh). The heavier **Models / Assets / Stats** tabs auto-refresh on a 12s cadence while open (paused while you're typing in a control).
- **Fleet diagnostics on the Remote tab.** Each machine now shows *why* its specs are or aren't showing: **Hub ✓** (connected, with live RAM/CPU), **no Hub on :47873** (TCP refused — the Studio Hub isn't running there), **unreachable (firewall/asleep)** (packets dropped), or **token mismatch** (Hub reachable but rejected the fleet token). The peer refresh distinguishes these via the connection error type.
- **Fleet-token Save now confirms.** Saving shows an explicit "✓ Saved on this Hub" and re-checks the fleet, and a live summary reports how many peers are connected vs. no-Hub / unreachable / token-mismatch.

No API breakage; `GET /api/hub/jobs` and the stream summary simply gain `running_items` / `avg_s`, and `GET /api/hub/resources` machines gain a `status` field. 70 tests.

## [1.17.0] — 2026-07-10

### Added — live dashboard updates (SSE)

- The dashboard now updates **live over a Server-Sent Events stream** (`GET /api/hub/stream`) instead of polling every 5s — updates in ~2s and lighter on the Hub at fleet scale. If the stream drops it **falls back to polling automatically** and retries the stream.

### Fixed / hardened (all regression-tested)

- **Memory governor:** a too-small *local* machine no longer errors the whole batch when a bigger **remote** studio in the pool could run the model — it skips/waits instead.
- **Memory race:** two concurrent local jobs could read the same free-RAM snapshot and OOM together; added reservation accounting so the governor accounts for in-flight local dispatches.
- **Gateway connection leak:** streamed upstream responses are now closed after the proxy response finishes — previously they leaked and could exhaust the connection pool over a long-running service.
- **Peer refresh** is non-blocking, so a slow/offline fleet never stalls the local health poll.
- Test suite is now **68 tests** (adds gateway, peers, alerts, SSE) and runs in CI on every push.

## [1.16.1] — 2026-07-10

### Added — CHANGELOG + in-app "What's New"

- This `CHANGELOG.md`.
- A **What's New** panel: click the version badge in the header to see recent release highlights. A small dot appears next to the version after an update (until you open it once), so new features are discoverable without leaving the dashboard. Highlights live in the frontend and mirror this file.

## [1.16.0] — 2026-07-10

### Added — granular Stats: direct studio usage + per-operation filters

Previously the Stats tab only counted generations **dispatched through the Hub** (`source='job'`), so everything created directly inside a studio (indexed as a `scan` of its output folder) was excluded — the tab looked nearly empty despite thousands of real generations.

- **Counts now span every source by default** — Hub jobs + direct-in-studio scans + uploads — with a **Source toggle** (All / Hub / Direct) to separate them.
- **Operation type is derived from the studio** that produced each asset (machine suffix stripped, falling back to the media type), so **voice (TTS) and music are counted separately** even for scanned audio, where the raw modality is only the coarse `audio`.
- **New controls:** click any app tile to filter every table and the throughput chart to that operation; a **machine** dropdown; and **7-day / 30-day** windows alongside the existing All / 24h / 1h.
- **API** (`GET /api/hub/stats`) accepts `source`, `modality`, and `machine`; the response adds `by_source` and `available_modalities` / `available_machines` for the filter UI.
- **Fix:** the per-machine / per-app average speed now divides by the number of *timed* rows (scans carry no duration) instead of the total, so untimed scans no longer drag the averages down.

Verified against a copy of the live ledger DB (image / voice / music split correctly; timing unaffected by untimed scans) and the full test suite (64 tests, incl. new op-split + filter coverage).

## [1.15.0] — 2026-07-10

### Added — observability & alerting (Phase 3 hardening)

Studio-down / recovered / batch-failed alerts, surfaced through a header bell with a details view.

## [1.14.0–1.14.5] — 2026-07

### Added / Fixed — always-on service + hardening (Phases 1–2)

- Always-on **launchd startup service** so the Hub auto-starts at login and self-heals (matching the studios), with app-specific service scripts.
- **Test suite + CI** and a configurable `DATA_DIR`.
- Bug hunt: governor, a gateway leak, and peer-blocking fixes.
- Unified one-click **Update** with an auto-check banner; version badge moved to the top-right of the header.

## [1.13.0] — 2026-07

### Added — reference-image jobs

img2img / edit reference-image support for `POST /api/hub/jobs`.

## [1.12.0] — 2026-07

### Added — richer Stats

Throughput-over-time chart and per-model speed on the Stats tab.
