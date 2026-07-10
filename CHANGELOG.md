# Changelog — Studio Hub KH

All notable changes to Studio Hub KH (the control plane for the KH Studio family) are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change to the Hub API, DB schema, or config. Re-install / migrate.
- **MINOR** (1.1.x → 1.2.x) — new feature, endpoint, or dashboard tab. **Update** from the Pinokio sidebar (restart the service if you run it as a startup service).
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak. **Just Update.**

> Entries before 1.16.0 are condensed summaries reconstructed from git history — this changelog began at 1.16.0.

---

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
