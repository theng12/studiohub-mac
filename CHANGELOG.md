# Changelog — Studio Hub KH

All notable changes to Studio Hub KH (the control plane for the KH Studio family) are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change to the Hub API, DB schema, or config. Re-install / migrate.
- **MINOR** (1.1.x → 1.2.x) — new feature, endpoint, or dashboard tab. **Update** from the Pinokio sidebar (restart the service if you run it as a startup service).
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak. **Just Update.**

> Entries before 1.16.0 are condensed summaries reconstructed from git history — this changelog began at 1.16.0.

---

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
