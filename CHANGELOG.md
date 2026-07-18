# Changelog — Studio Hub KH

All notable changes to Studio Hub KH (the control plane for the KH Studio family) are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change to the Hub API, DB schema, or config. Re-install / migrate.
- **MINOR** (1.1.x → 1.2.x) — new feature, endpoint, or dashboard tab. **Update** from the Pinokio sidebar (restart the service if you run it as a startup service).
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak. **Just Update.**

## Unreleased

## [1.44.6] — 2026-07-18

### Fixed — legacy WAV metadata backfill

- Older completed voice jobs now backfill and persist their validated WAV
  checksum, byte size, duration, sample rate, and channel metadata on the first
  artifact read even when the worker already returned a valid `audio/wav`
  header. Repeated reads reuse the cached facts.

## [1.44.5] — 2026-07-18

### Fixed — validated voice artifact results

- Root cause: the artifact proxy always labelled every generated file
  `video/mp4`, while the worker's `duration_seconds` was generation runtime,
  not decoded audio duration. Voice jobs also did not reliably include bytes
  or a checksum in their terminal worker payload.
- Successful WAV voice jobs now validate the actual RIFF/WAVE bytes once at
  completion and persist `media_type`, `format`, bytes, SHA-256, decoded audio
  duration, sample rate, channels, and explicit `runtime_s`. `duration_s` is
  retained as a documented compatibility alias for runtime only.
- Artifact proxy responses preserve an allowed upstream image/video/audio MIME
  type, prefer cached byte-validated metadata, and no longer hard-code MP4.
  Public job results expose a stable Hub artifact URL and omit worker-local
  paths.
- Added fixtures covering the production-shaped WAV, runtime-versus-media
  duration, proxy MIME behavior, repeated reads, peer authentication, and
  missing/non-terminal artifacts. Non-WAV Voice Studio outputs are unchanged;
  they need worker-provided validated metadata before contract consumers should
  bill them.

## [1.44.4] — 2026-07-18

### Fixed — durable remote-render asset transfer

- Render inputs are now content-addressed by SHA-256 and retained for seven
  days. Story Studio can reconnect or retry a failed remote render without
  sending narration, scene media, overlays, titles, music, or subtitles again.
- Added checksum lookup before upload, immutable lease refresh on worker
  download, and protection against deleting active content-addressed assets.
- Concurrent uploads of the same media now safely converge on one verified
  retained object instead of consuming duplicate Hub storage.

## [1.44.3] — 2026-07-17

### Added — live job elapsed time and stall warning

- Every image, voice, render, transcription, and chat job now shows how long
  it has been processing (or waiting in queue) and when the last real activity
  occurred.
- Generation batches track actual worker progress changes and show a visible
  warning when a running batch has made no progress for at least 15 minutes,
  or five times its measured per-item average. The warning is advisory; it
  never cancels work automatically.

## [1.44.2] — 2026-07-17

### Changed — simple owner password

- Owner sign-in now accepts any non-empty password; there is no 12-character
  requirement. Password storage, remembered-device sessions, and Tailscale-only
  access remain protected exactly as before.

## [1.44.1] — 2026-07-17

### Hardened — Tailscale-only password sign-in

- Remembered-device password sessions are accepted only through the Hub's
  Tailscale address. The LAN address continues to support the recovery/API
  token, avoiding persistent browser credentials over ordinary HTTP LAN traffic.

## [1.44.0] — 2026-07-17

### Added — password sign-in for remote browsers

- Set one owner password locally in **Remote → Owner sign-in**, then sign in
  normally from any Tailscale device. A successful sign-in remembers that
  browser for 90 days, so everyday dashboard access no longer needs the raw
  Hub token.
- Passwords are salted and scrypt-hashed; the Hub stores only hashes of the
  opaque remembered-device sessions. Replacing the password signs out every
  remembered browser immediately. Login attempts are rate-limited.
- Hub and fleet tokens remain available for API clients, peer Hubs, and
  recovery, but are no longer the normal browser login flow.

## [1.43.2] — 2026-07-17

### Fixed — cache-proof release discovery

- Studio Hub now resolves each repository's current `main` commit through
  GitHub's Git transport endpoint, then reads `VERSION` from that immutable
  commit. This avoids stale raw branch URLs without consuming GitHub API quota.

## [1.43.1] — 2026-07-17

### Fixed — immediate GitHub release visibility

- Release checks now use GitHub's fully qualified `refs/heads/main` raw path.
  GitHub can briefly serve stale content from the shorthand `/main/VERSION`
  path after a push; the qualified ref exposes the new version immediately.

## [1.43.0] — 2026-07-17

### Improved — simple, consistent fleet updates

- Replaced Remote's fleet preflight checklist with the same focused controls as
  agent-Hub updates: app tabs, running/latest versions, reachability, Rescan,
  update-all-ready, and one-Studio Update buttons.
- Studio version scans now call only the public version/update endpoints. Health,
  model, engine, port, memory, and disk checks no longer gate or clutter manual
  update control; active work still drains and the restarted Studio is verified.

### Fixed — complete machine removal

- Removing a machine now immediately clears its Studios from live health and
  model/provider caches, peer resources, saved Studio/Hub version rows, labels,
  and enable/disable settings. Historical jobs and assets remain available.
- Saved update views automatically exclude machines no longer in the registry,
  and the Machines table now waits for fresh state before repainting.

## [1.42.3] — 2026-07-16

### Fixed — immediate authoritative release discovery

- Studio Hub now checks the canonical GitHub `VERSION` files for Hub, Voice,
  Chat, Image, Music, Video, and Render Studio every minute. Detection no
  longer waits for each Studio's daily/weekly updater cache to refresh.
- GitHub requests bypass stale CDN responses and preserve the last-known good
  version per app when one repository is temporarily unreachable.
- Both Updates and Remote use the Hub's canonical release value. Their visible
  views refresh automatically, so a newly pushed Voice Studio release appears
  without changing tabs or pressing Refresh.
- Starting an automatic fleet update now forces both the Hub's GitHub check and
  the target Studio's own safe update check before deciding that it is current.
  Completion also requires the restarted Studio to reach the published version.

## [1.42.2] — 2026-07-16

### Added — remote-only final rendering

- Added a dedicated Render view in Jobs with live worker name, progress, queue
  state, cancellation, and safe history cleanup for Story Studio final videos.
- Added the `routing: "remote"` batch route. It deliberately excludes the Hub
  Mac, so final-video work waits for an external Render Studio worker instead
  of silently consuming the control centre.

## [1.42.1] — 2026-07-16

### Fixed — authoritative fleet versions

- Remote maintenance now compares every worker against one published version per
  Studio app. A stale worker cache can no longer make Voice Studio v1.20.2 look
  current after v1.20.3 is published.
- The Updates dashboard now reconciles an app's saved updater history with its
  published-version contract, preventing false downgrade displays such as
  `1.20.3 → 1.20.2`.

### Improved — focused remote Studio updates

- Added app tabs to Remote maintenance. Choose All apps or a single app to
  filter both preflight checks and the rolling-update list.
- The existing bulk action now updates all eligible Studios in the selected app
  tab; All apps remains the fleet-wide action, and per-machine Update buttons
  remain available.

## [1.42.0] — 2026-07-16

### Fixed — durable production-job details

- Expanded image/voice item status now survives live queue refreshes instead of
  closing after a few seconds. The Hub preserves the open panel and reuses its
  loaded item detail while fresh queue summaries continue arriving.

### Added — unified production-job control and safe cleanup

- Jobs is now organized into Image, Voice, Transcription, and Chat tabs. Each
  has independent sort controls, ten jobs per page, pagination, and matching
  terminal-job clear controls.
- Clearing a transcription job permanently removes its Hub-local uploaded
  source and subtitle files. Clearing an image/voice job removes its Hub asset
  ledger entries and only unlinks a file when it is owned by this Hub; worker
  output and shared voice references are never deleted.
- Added an optional, off-by-default local job-storage cap. Set a 1–50 GB limit
  and the Hub will remove oldest completed transcription jobs only when its
  own job files exceed that limit. A "Check now" control makes the result
  visible without waiting for the hourly sweep.

## [1.41.1] — 2026-07-16

### Fixed — shared cloned voices dispatch only where synchronized

- Voice jobs using a canonical Hub voice ID now wait for a Voice Studio that
  has successfully synchronized that exact voice instead of dispatching to an
  arbitrary model-compatible worker.
- Direct-only Voice Studio voice IDs retain their existing behavior, while an
  unsynchronized shared voice remains safely queued for the background sync
  process to heal.
- The queue status explains when it is waiting for a compatible shared-voice
  worker, preventing silent wrong-voice requests and avoidable worker errors.

## [1.41.0] — 2026-07-16

### Added — one local ElevenLabs gateway for the fleet

- ElevenLabs cloud voice batches now always wait for the Voice Studio running
  on the main Studio Hub Mac. Remote Voice Studios remain eligible for local
  TTS models but no longer need duplicate cloud credentials or account pools.
- The scheduler reports that it is waiting for the local ElevenLabs gateway
  when that Voice Studio is offline, busy, disabled, or under maintenance,
  instead of silently spilling a paid cloud request onto another Mac.
- Central routing keeps account selection, quota state, per-account voice IDs,
  and connection-drop recovery in one place. Stable per-item request IDs make a
  lost Hub-to-Voice submit response idempotent, and uncertain paid outcomes are
  never requeued. Added broker tests proving that
  ElevenLabs uses only the local gateway while ordinary local TTS still uses
  every eligible Voice Studio.

## [1.40.0] — 2026-07-16

### Added — one shared, transcribed voice library for the fleet

- Added a dedicated **Voices** workspace. Select a reference recording,
  transcribe it through the existing fleet Whisper queue inside Studio Hub,
  review or correct the text, confirm permission, then save and synchronize it
  to every registered Voice Studio Mac.
- Studio Hub is the source of truth for the reference audio, metadata, and
  transcript. Every worker receives the same stable 12-character voice ID and
  verified SHA-256, so Hub-dispatched cloning jobs resolve consistently.
- Added per-machine synchronized, pending/offline, unsupported-version,
  conflict, and failed states with manual **Sync again** control and authenticated
  reference-audio playback.

### Automatic recovery and safety

- Connection drops and offline machines stay pending and retry every 30 seconds.
  Updating or reconnecting an older Voice Studio is enough for it to catch up;
  no voice re-upload is needed.
- Shared sync routes remote traffic through each machine's authenticated peer
  Hub. Audio hashes and returned IDs are verified before a target is marked
  synchronized.
- Existing local voices are never merged, overwritten, or deleted. Provider
  mappings and generated model embeddings remain machine-local.
- Added isolated tests for canonical storage, validation, authentication,
  transcription-to-editable-text, peer routing, old-worker reporting, a forced
  connection drop, and successful automatic retry. No new dependency or model
  installation is required.

## [1.39.0] — 2026-07-15

### Added — safe optional updates for the Hub and the whole fleet

- Added a dedicated **Updates** workspace with Off, Notify only, and automatic
  install modes. The default remains Off. Daily or weekly schedules, the local
  maintenance hour, idle-only protection, installed/latest versions, last and
  next checks, release notes, live progress, defer reasons, Retry, Check now,
  Update now, and Update after current work are visible in one place.
- The fleet table discovers the safe updater on Hub, Voice, Chat, Image, Music,
  and Video, with independent per-app modes and settings links. **Check all**
  asks every reachable app to refresh; **Update idle apps** runs eligible sibling
  Studios one at a time and verifies health before proceeding to the next.
- Fleet updates tolerate the expected connection drop during an app restart,
  reconnect to the same update, and do not start a duplicate. Active work is
  reported as deferred instead of cancelled. Existing manual preflight and
  rolling maintenance remain available separately in Remote.

### Safety and recovery

- Hub installation is blocked by active generation, Chat, transcription,
  leases, or fleet maintenance. Every update requires the fixed GitHub origin,
  `main`, a clean fast-forward, free disk, successful dependency/import checks,
  a healthy restart, and the exact expected running version.
- The short-lived LaunchAgent works without an open browser, uses one lock and
  rotating redacted logs, and is removed immediately when set to Off. Reset
  unloads it before removing the environment. Failed installs make one bounded,
  clean-worktree rollback attempt and never discard local changes.
- Added focused regression coverage for schedule lifecycle, Git safety,
  deferral, rollback, per-app mode preservation, sequential health gates, and
  automatic reconnection after restart.

## [1.38.0] — 2026-07-15

### Fixed — self-healing fleet generation without authentication races

- Every remote Studio request now uses that machine's peer Hub immediately, even before the short peer-status cache has populated. This closes the startup/discovery race that sent newly connected workers a stale direct credential and caused hundreds of image items to fail with HTTP 401.
- Generation transport, timeout, throttling, and 5xx failures retry up to three times after visible 3-second and 10-second delays. Authentication and other permanent 4xx failures stop immediately with the original reason instead of burning three attempts.
- Jobs distinguishes **retrying** from queued/running work, shows the next attempt countdown, and preserves the exact worker failure. Batch submissions are capped at 1,000 items and 25 MB.
- Remote Hub work no longer falls back around the agent Hub, keeping one authority for credentials, machine identity, and lifecycle control.

### Security and reliability

- Removed query-string token authentication. The remote dashboard authenticates with a header once, then uses an HttpOnly same-site session for SSE so credentials do not enter browser history or access logs.
- Live updates now show live-versus-polling state and reconnect with bounded exponential backoff without stacking reconnect timers.
- New registry writes reject URL-shaped hosts and unsafe machine IDs, and duplicate host/port worker registrations are refused.
- Startup-service repair refuses to terminate an unrelated process on port 47873. Install, update, and restart report success only after Hub health is verified; update additionally verifies that the running version matches `VERSION`.
- Added a reproducible runtime dependency lock used by Install and Update. `pip-audit` found no known vulnerabilities; Bandit found no high-severity issues.

### Documentation

- Expanded the Remote tab and README with the fleet trust model, source-of-truth credential, validation, rotation, revocation, and one-time repair flow.
- Added regression coverage for peer routing before cache warmup, permanent-versus-transient retry decisions, retry visibility, token transport, request bounds, registry validation, and duplicate endpoint prevention.

## [1.37.1] — 2026-07-15

### Fixed — generation uses connected peer Hubs for remote workers

- Image, voice, video, render, transcription, gateway, recipe, cancellation, polling, acknowledgement, and artifact requests now travel through each remote machine's connected Studio Hub instead of bypassing it for a direct Studio call.
- A remote Studio that cached an older fleet credential can no longer consume and fail most of a batch with repeated HTTP 401 errors; its local peer Hub securely reaches the worker over loopback with the current machine credential.
- Local workers remain direct loopback, and a remote worker still falls back to its direct authenticated address only when that machine's peer Hub is unavailable.
- Added broker regression coverage proving remote generation submission and polling use the peer-Hub URL and Hub credential.

168 tests.

### Fixed — Studio update status reflects running versus published versions

- Local and remote Studio rows now compare the running version with each Studio's published latest version and explicitly report **Current**, **Update available**, or **Not verified**.
- Completed update history no longer leaves a misleading **Updated** badge and active Update button behind. The Hub rescans every Studio before marking a rolling-update job complete, and only confirmed outdated Studios are included in bulk updates.
- Version truth remains visible when a separate preflight check blocks updating; the blocking check and its detail are shown separately.

## [1.37.0] — 2026-07-15

### Added — acknowledged fleet cancellation and safe image-job cleanup

- Cancelling a Story Studio generation batch now makes Studio Hub immediately signal every known running Studio worker job, while queued items are cancelled before they can dispatch.
- Cancellation responses report queued cancellations, running stop signals, and any worker signals still pending, so clients no longer silently claim that fleet work stopped.
- Added **Cancel image queue**, **Clear finished image jobs**, and per-batch **Clear** controls to Jobs. Active work must be cancelled before it can be cleared.
- Clearing removes Hub job history only. Generated assets, ledger records, and output files are always preserved.
- Added bulk cancellation and terminal-history cleanup endpoints with modality scoping, plus regression coverage for worker signalling, queue isolation, active-job protection, and asset preservation.

165 tests.

## [1.36.1] — 2026-07-14

### Fixed — Video Studio image-to-video dispatch

- Video jobs with a reference image now use Video Studio's multipart `video2video` endpoint in explicit `img2video` mode instead of being sent to the text-to-video endpoint.
- The Hub validates the selected model's `img2video` capability before dispatch, forwards the exact uploaded source image, and keeps cloud-provider credentials inside Video Studio.
- Text-to-video remains available to other Hub clients, while Story Studio can enforce its stricter image-to-video-only product boundary.

162 tests.

## [1.36.0] — 2026-07-14

### Added — fleet-wide cloud audio provider readiness

- Voice Studio cards now show whether cloud audio providers are ready, configured but unavailable, missing credentials, or not yet supported by an older Voice Studio release. The compact status works in both card and list views and remains readable on mobile.
- Added `GET /api/hub/providers` and a `cloud_providers` summary payload with ready, configured, available, machine, model-count, and stale-state information. Provider health federates through the existing peer resource snapshots as each agent Hub updates.
- Provider polling uses a short timeout and cache, keeps the last known state during transient failures, and retains only a strict public-field allowlist. API keys and other provider response data never enter Hub state or fleet snapshots.
- Mixed-version fleets remain compatible: older Voice Studios that return 404 are marked unsupported instead of breaking the resource poll or dashboard.

161 tests. Responsive dashboard verified at 1440 px and 390 px without horizontal overflow.

## [1.35.1] — 2026-07-14

### Fixed — render is a local lane, not cloud

- The `render` episode-assembly step no longer appears in the Cloud lane (or counts as a cloud generation). Render Studio flags its catalog entry `is_cloud=true` only to bypass the broker's download/memory gates — that's a dispatch hint, not a hosting statement. The Hub now classifies lanes with `monitor.is_cloud_lane(is_cloud, modality)`, which treats `render` (and any future assembly-type modality in `LOCAL_ONLY_MODALITIES`) as local while leaving the broker's raw dispatch path untouched. Applied in both the Models tab (`models_by_repo`) and the ledger `is_cloud` the broker records at dispatch.

157 tests.

## [1.35.0] — 2026-07-14

### Added — local vs cloud model lanes across the dashboard

- The **Models** tab now splits the catalog into a **Local** lane and a **Cloud** lane (grouped by provider) instead of mixing them, with a **Local / Cloud / All** filter. Cloud rows show a provider badge (e.g. `fal`), a **new** badge, a **deprecated** badge (from the studio's `status`), and a price pill when a `price` object is present. The existing modality ordering is kept within each lane.
- Model counts are reported as **distinct lanes, never one merged number**: `/api/hub/models` and `/api/hub/catalog` now return a `lanes: {local, cloud}` summary (computed before any `cloud=` filter is applied), and `/api/hub/models` adds a `providers: {name: count}` breakdown for the cloud lane.
- The **Stats** tab gains a **Local / Cloud / All** lane facet next to the existing Source filter. `/api/hub/stats` accepts `lane=local|cloud` and always returns `by_lane: {local, cloud}` for the current window, so the split is visible even while viewing one lane.
- Cloud generations are now tagged in the ledger: the `assets` table gains an `is_cloud` column (auto-migrated on existing DBs), the broker records it from the studio's own catalog entry, and direct in-studio scans (which can't know a model's provider) count as **local**. Generation/broker routing is unchanged — cloud models still flow through the existing `video` → `/api/generate/txt2video` contract.
- This is generic across studios: any studio whose `/api/catalog` marks entries `is_cloud=true` with a `provider` (Video, and next Voice) is grouped and counted the same way; existing Image and Chat local catalogs are unaffected and simply stay in the Local lane.

156 tests.

## [1.34.4] — 2026-07-13

### Changed — visible, consistent Studio and Hub updates

- Studio and agent-Hub updates now show a live progress card, completed count, current machine or Studio, per-row state, and failure details while the job is running.
- Added a Studio version rescan, individual agent-Hub update actions, and bulk Hub updates that target only reachable machines with an update ready.
- Studio and Hub update actions now share the same polished primary-button design and clear disabled/updating labels.
- Remote Studio update polling now reconnects to the same update job after a temporary connection drop instead of reporting a false failure or starting the update twice.

154 tests.

## [1.34.3] — 2026-07-13

### Changed — separate Studio update controls from preflight diagnostics

- Fleet preflight now focuses on health, capability, model, engine, memory, and other safety checks.
- Added a dedicated Studio updates table with version, last checked, status, sorting, bulk update, and one-click per-Studio Update actions.
- Existing rolling drain, verification, and update eligibility rules are unchanged.

## [1.34.2] — 2026-07-13

### Fixed — self-healing generation status after connection drops

- When a worker accepts an image/audio/video generation but the Hub loses the status response, the Hub now keeps the original lease and reconciles that same worker job for up to 120 seconds before retrying — long enough for a slow M1 generation to finish.
- A completed worker job is adopted into the Hub ledger instead of being duplicated or reported as a false failure. Empty transport errors now include their exception type for diagnosis.

## [1.34.1] — 2026-07-13

### Added — per-image generation status

- The Jobs tab now has an expandable per-image view for generation batches, showing each prompt's state, retry attempts, worker/Mac, duration, and final failure reason.
- The existing batch summary, queue behavior, automatic retries, and Assets/Stats views were left unchanged.

> Entries before 1.16.0 are condensed summaries reconstructed from git history — this changelog began at 1.16.0.

## [1.34.0] — 2026-07-13

### Added — adaptive cloud scene-prompt packs

- Chat batches now declare their model cost tier. Local and free-cloud workers remain hard-limited to 10 scenes per request, while paid-cloud workers accept up to 30; Story Studio defaults paid cloud to 20.
- The tier participates in idempotency and is returned in batch status, so retries and saved queue history retain the exact batching policy.
- Existing clients that omit the tier remain safely classified as local and keep the original 10-scene limit.

152 tests.

---

## [1.33.4] — 2026-07-13

### Fixed — canonical Studio credential stays synchronized

- Every Hub fleet save now updates both its private `.fleet_token` and the owner-only API-root `.kh_studio_token` consumed by all sibling Studios. This is independent of whether the Hub folder is named `studiohub-mac` or `studiohub-mac.git`.
- Updated Studios reload that canonical file on every protected request, so synchronization repairs authentication immediately without another Studio restart.

152 tests.

## [1.33.3] — 2026-07-13

### Fixed — mixed Pinokio folder names and truthful update completion

- Studio lifecycle/update control now resolves both the configured folder and its exact `.git` suffix counterpart, covering machines installed as either `imagestudio-mac` or `imagestudio-mac.git` (and the inverse Chat variant).
- Update verification now follows the post-pull `VERSION` file and requires the running process to load that version after a restart or version advance. A peer can no longer report an old release as a successful update before the pull finishes.

152 tests.

## [1.33.2] — 2026-07-13

### Fixed — rolling updates drain every queue type

- Fleet maintenance now blocks new Chat packs and transcription chapters as well as generation jobs, then waits for active leases from all three queue systems before restarting a Studio. Rolling authentication upgrades can no longer interrupt an in-flight LLM pack or Whisper chapter.

150 tests.

## [1.33.1] — 2026-07-13

### Fixed — verification feedback distinguishes offline from mismatched

- **Save & verify** now separates a real credential mismatch (one-time local Save required) from an offline or unreachable peer (retry when it is online). Network exception names are shown when the underlying message is empty.

148 tests plus an isolated browser save flow and a live 13-peer verification (6 verified, 7 correctly identified as unreachable).

## [1.33.0] — 2026-07-13

### Added — save, synchronize, and verify one fleet credential

- **Save & verify** now saves on the primary Hub, securely synchronizes every registered peer Hub using the previously trusted credential, and verifies each peer with the new value before claiming success.
- The Remote tab has an explicit busy state and a persistent accessible result panel with per-machine success or failure details. Live fleet refreshes no longer erase the Save response.
- Mismatched peers are identified as needing a one-time local Save; already connected peers no longer require repetitive pasting on every Mac.
- Fleet credentials must be 12–512 characters, preventing accidental empty or ambiguous short saves. Tokens remain owner-only and are never returned in synchronization results.

148 tests.

## [1.32.4] — 2026-07-13

### Fixed — model warm-up no longer exhausts retries immediately

- A transient Chat worker failure now waits 5 seconds before attempt two and 15 seconds before attempt three. Newly activated workers have time to load a cached model into memory or recover from a brief restart instead of consuming all attempts in a tight loop.
- Jobs shows the automatic retry countdown even while other workers remain active. Manual **Retry missing** still resets exhausted packs without discarding completed scenes.

146 tests.

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
