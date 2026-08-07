# Changelog — Studio Hub KH

All notable changes to Studio Hub KH (the control plane for the KH Studio family) are documented here.

Versioning follows [Semantic Versioning](https://semver.org/) with this project-specific interpretation:

- **MAJOR** (1.x.x → 2.x.x) — breaking change to the Hub API, DB schema, or config. Re-install / migrate.
- **MINOR** (1.1.x → 1.2.x) — new feature, endpoint, or dashboard tab. **Update** from the Pinokio sidebar (restart the service if you run it as a startup service).
- **PATCH** (1.2.0 → 1.2.1) — bugfix / UI tweak. **Just Update.**

## Unreleased

## [1.78.2] — 2026-08-07

### Fixed — `--machine` no longer weakens broken-cache detection

- Narrowing a repair run to specific machines also narrowed the population that
  broken-cache detection compares against, so a run targeting two damaged
  caches drew its reference from exactly those damaged caches. Observed live: a
  VibeVoice peer median of 105% across the fleet collapsed to 14% when the run
  was filtered to three machines.
- The whole fleet is now always surveyed to build the comparison; `--machine`
  and `--studio` filter only what is reported and acted on. A filtered run and
  a full run now reach identical conclusions about the same machine.

## [1.78.1] — 2026-08-07

### Fixed — broken-cache detection now compares machines, not catalog sizes

- `tools/fleet_repair.py` judged a cache broken by comparing its bytes against
  the catalog's `size_gb`, which is a hand-maintained approximation and is
  routinely off. `chatterbox-turbo-4bit` measures 69% of its claimed size on
  all six machines that hold it — the catalog is stale and every one of those
  caches is healthy, but all six were reported broken and queued for a pointless
  re-download.
- Completeness is now judged against the rest of the fleet: healthy copies of a
  repo measure the same to within rounding, so a machine well below its peers
  has genuinely lost data whatever the catalog claims. Repos held by fewer than
  three machines fall back to a deliberately forgiving absolute check.
- This removed all six false positives and kept every real one, each of which
  now reports its own evidence — a Kokoro cache holding 10% where the fleet
  holds 115%, a VibeVoice cache at 1%, and a MOSS cache 20% under its peers
  that the previous tolerance had let through.

## [1.78.0] — 2026-08-07

### Added — fleet cache survey and repair

- `tools/fleet_repair.py` walks every machine in the fleet, compares each
  studio's on-disk model cache against what that machine's RAM qualifies it
  for, and reports broken caches, stranded models, and uncovered capabilities.
  It covers Voice Studio and Image Studio through the endpoints both already
  expose, so no studio-side change was needed.
- Interrupted downloads (`partial`) and caches that report `cached` while
  holding well under their expected bytes (`phantom`) are both detected and
  can be repaired in place with `--apply`. The first run over the fleet found
  35 partial and 10 phantom caches; a phantom Kokoro cache on one worker had
  been failing every job routed to it at load time.
- `--fill-gaps` additionally installs one model for any essential capability a
  machine cannot currently serve — a worker with no clone-capable voice model
  is invisible dead weight in the routing pool rather than an obvious failure.
- Nothing is ever deleted. Repair only starts downloads, so a wrong call costs
  bandwidth rather than a cache. Models cached above a machine's memory floor
  are reported as `stranded` for an operator to reclaim deliberately.

### Fixed

- Provisioning decisions use each model's static memory floor rather than Voice
  Studio's `memory_eligible`. That field is `total >= floor and available >=
  required`, and its live free-memory term made a busy machine look permanently
  unqualified for models it runs perfectly well once other apps quit.

## [1.77.0] — 2026-08-04

### Added — controlled Fish Audio S2 Pro memory qualification

- The controller-owned Voice qualification harness now admits only the exact
  `mlx-community/fish-audio-s2-pro-8bit` checkpoint and checksum-bound Aiden
  reference/transcript for cloning at the 30-second, 5-minute, and 15-minute
  evidence targets.
- Fish attempts record their intended audio duration and automatically request
  cancellation against the exact durable worker job after the 10× realtime
  speed-stop threshold is exceeded.
- A Fish pass now requires output within 15% of its requested duration, exact
  checkpoint and reference evidence, a complete artifact contract, long-form
  chunk evidence, and type-checked resource telemetry. Successful evidence
  includes measured slowdown ratio and inverse realtime throughput.
- Each physical worker tier must durably pass 30 seconds before 5 minutes and
  both earlier gates before 15 minutes on the same immutable checkpoint.

### Safety

- The disputed Fish catalog RAM floor is retained as reported evidence but does
  not control admission. Fish permits only controlled 16 GB and 24 GB tiers and
  independently requires at least 8 GB live free memory, fresh authenticated
  telemetry, an exact immutable revision, idle state, and Voice Studio 1.27.15.
- Response-lost submissions reconcile through Voice Studio's stable request ID
  without reposting. Uncertain jobs fence their physical machine until their
  exact worker job reaches a final state; an authenticated empty reconciliation
  releases the fence as a failed attempt after a three-minute safety grace.
- The 10× watchdog uses worker execution time and falls back conservatively to
  Hub acceptance time when a running worker omits its start timestamp.
- Fish remains internal research only. This release does not approve, expose,
  route, price, publish, or add it to GenStudio's desired-state catalog.

### Verification

- Fake-worker coverage proves 16 GB and 24 GB tier selection, checksum-bound
  reference transport, variable-length 5-minute and 15-minute cases, exact-job
  10× cancellation, durable replay behavior, sanitized telemetry, and artifact
  access without exposing worker locations.

## [1.76.2] — 2026-08-03

### Changed — OmniVoice qualification is cloning-only

- OmniVoice qualification now accepts only reference voice cloning. Auto voice,
  voice design, and combined design controls remain internal sibling research
  capabilities and cannot enter the controller's approval or publication flow.
- The verified short-form clone evidence now unlocks the controlled 40,000
  character adapter-managed long-form clone test without approving or exposing
  the model.

### Verification

- Focused qualification tests prove that a checksum-bound, transcript-bearing
  reference is still required for an OmniVoice long-form attempt and that the
  attempt remains an internal evidence record rather than a customer job.

## [1.76.1] — 2026-08-03

### Fixed — approved catalog no longer blocks controller updates

- The owner-controlled approved-model catalogue is now treated as mutable
  per-controller runtime state, alongside the other machine-local Hub state
  files. Creating or changing an approval no longer makes the Git checkout
  appear dirty to the safe updater.
- Existing `model_exposures.json` files remain in place and are preserved
  during updates; no approval, revocation, audit evidence, or desired-state
  revision is deleted or rewritten by this patch.

### Verification

- A focused regression test requires the exposure state file to remain
  ignored by Git so future catalogue changes cannot regress fleet updates.

## [1.76.0] — 2026-08-03

### Added — Group B Wave 2 qualification controls

- The controller-owned voice qualification harness now admits the exact
  VoxCPM2 4-bit, VibeVoice Realtime 0.5B 4-bit, and OmniVoice bfloat16
  checkpoints without granting approval, routing, or publication authority.
- Qualification requests record the exact operation under test and enforce
  model-specific reference, transcript, voice-design, preset-roster, immutable
  revision, idle-worker, and live-memory requirements before remote execution.
- OmniVoice long-form remains deliberately disabled until its short-form
  runtime and memory evidence establishes a safe private section budget.
- Completed qualification audio can be downloaded through the controller,
  allowing every listening decision to retain its exact generated artifact
  without revealing a worker address.

### Safety

- Wave 2 requires Voice Studio 1.27.9. Unknown, busy, stale, mismatched,
  uncached, or memory-ineligible workers remain blocked before submission.
- Lost submit responses remain uncertain and are never automatically retried.

## [1.75.0] — 2026-08-03

### Added — approved-model placement matrix

- The Models page now shows approved models as rows and registered sibling
  machines as columns, so the owner can see fleet placement at a glance.
- Every machine/model cell translates the controller's existing desired-state
  evidence into a plain status: Ready, Downloading, Not installed, Offline,
  Not suitable, RAM unknown, Contract mismatch, or Needs attention.
- Each cell shows the audited RAM floor, observed machine RAM, and the exact
  controller reason. The detailed target table remains available in a compact
  disclosure for troubleshooting.

### Safety

- This is a read-only presentation of the existing GenStudio-approved catalog
  and Studio Hub target evidence. It does not approve models, change desired
  state, start downloads, or infer suitability without machine RAM evidence.

### Verification

- Focused baseline, frontend typography, and release metadata tests cover the
  matrix contract and its suitability/error labels.

## [1.74.0] — 2026-08-03

### Added — resource-aware fleet scheduling

- Local inference now uses best-fit placement: an eligible 8 GB worker is
  selected before a 16 GB or 24 GB worker for flexible models such as Kokoro,
  preserving scarce higher-memory capacity without hardcoding model names.
- Queued models with a higher audited or operator-controlled total-memory floor
  receive the next compatible high-memory worker before flexible queued work.
  A newly queued 16 GB Qwen3-TTS job therefore takes a 16/24 GB worker after
  its current Kokoro item finishes, while 8 GB workers continue the Kokoro
  queue.
- Queue ranking reads only Studio Hub's durable last-good sibling catalog. It
  never blocks dispatch on a live catalog request, and the existing exact
  revision, cache, hardware, live-memory, and worker admission gates remain
  authoritative.

### Safety

- Running jobs are never preempted. Larger machines remain work-conserving and
  resume flexible work whenever no compatible constrained job is waiting.
- Unknown-memory workers remain eligible as last-choice fallbacks, and Render
  Studio keeps its existing hardware-score priority.

### Verification

- Broker tests cover 8 → 16 → 24 GB best-fit ordering, a 15-item constrained
  Qwen queue overtaking an older flexible Kokoro backlog at the next dispatch
  boundary, unchanged fair-turn behavior for unknown/cloud work, and preserved
  render priority.

## [1.73.2] — 2026-08-03

### Changed — safe Qwen3-TTS cloning admission

- Qwen3-TTS 0.6B Base voice-cloning work now requires a 16 GB Apple Silicon
  machine. The completed 8 GB run remains valid negative evidence, but urgent
  memory pressure and swap make that tier ineligible for customer work.
- The 24 GB tier remains preferred; unrelated Qwen3-TTS variants keep their
  separate, unapproved qualification state and are not promoted by this rule.

### Verification

- Focused memory-admission and broker tests prove the exact Base checkpoint is
  rejected on 8 GB and remains eligible on 16 GB when live free-memory policy
  is satisfied.

## [1.73.1] — 2026-08-03

### Fixed

- Authenticated Hub and fleet machine clients may now operate the isolated
  Wave 1 qualification evidence API. Model exposure approval and revocation
  remain restricted to the controller-local or remembered owner browser, so
  fleet automation cannot approve, publish, price, or route a candidate.

### Verification

- Added an authorization boundary test proving the machine token can read the
  qualification ledger while the same client remains forbidden from model
  approval.

## [1.73.0] — 2026-08-03

### Added — controlled Wave 1 voice qualification

- Added a controller-owner API for durable qualification attempts
  for Qwen3 TTS CustomVoice 0.6B 8-bit, Qwen3 TTS Base 0.6B 8-bit cloning,
  and Chatterbox 4-bit. It supports short, exact 40,000-character long-form,
  and cancellation cases without creating a customer job or changing model
  approval, catalog, price, or publication state.
- Every attempt persists its stable client request ID and exact physical
  site/machine/RAM/model/revision evidence before submission. Preflight
  requires an idle Voice Studio at v1.27.0 or newer, a fully cached and
  runtime-ready model, and a fresh authenticated memory snapshot for its 8,
  16, or 24 GB tier. Controller-local work is default-denied, requires an
  explicit opt-in, resolves to the controller's physical machine ID, and can
  be fenced with persisted excluded machine IDs.
- Qwen CustomVoice requires the worker's exact nine-speaker availability
  roster; Chatterbox requires its exact 23-language runtime-enforced catalog
  roster. Clone reference assets are resolved and checksum-read during
  preflight, before any attempt record or worker submission.
- Submissions, polls, and cancellation signals travel through the registered
  remote machine's Studio Hub. Lost responses or worker identity changes become
  review-required `uncertain` attempts and are never auto-resubmitted.
- Terminal records retain only the Voice Studio v1.1 versioned integration
  envelope, allowlisted artifact facts, and Voice Studio
  resource telemetry; worker endpoints, paths, credentials, prompts, and raw
  worker errors are excluded from the admin response.

### Verification

- Added fake-worker coverage for persistence and idempotency, controller-local
  opt-in and physical exclusion, Qwen/Chatterbox control rosters, reference
  asset preflight failure, uncertain transport handling, Voice Studio v1.1
  terminal evidence sanitization, cancellation, and no model-approval mutation.

## [1.72.3] — 2026-08-03

### Fixed

- Published the already-sanitized audited candidate hardware requirement in
  every approved worker model capability, so private GenStudio clients receive
  the contract field promised by capability schema v2 without exposing tokens
  or local paths.

## [1.72.2] — 2026-08-03

### Fixed

- Auto-update validation now accepts legitimate linked Git worktrees while
  continuing to reject symlinked checkout roots, malformed gitfiles, and
  missing Git metadata targets before any update operation runs.

## [1.72.1] — 2026-08-02

### Added — Claude Desktop work queue

- Added a repository-local `.claude-work` foundation for controller-reviewed
  batches, focused tasks, isolated implementation worktrees, versioned shared
  contracts, per-task reports, and final batch summaries.
- Every task records disjoint path boundaries, dependencies, verification
  commands, commit and push permissions, provider-spending policy, and
  production, credential, migration, restart, deployment, and live-fleet
  authority independently.
- The worker prompt preserves Studio Hub's controller boundary, keeps sibling
  repositories isolated, and requires controller review before merge, push,
  deployment, service restart, or live configuration changes.

### Changed

- Commits confined entirely to `.claude-work/**` are now treated as internal
  orchestration state and do not create an application release. Any commit
  touching Studio Hub code, tests, launchers, configuration, contracts, or
  runtime documentation still follows the full release discipline.

### Verification

- Validated every required template, queue-state directory, permission field,
  architectural safeguard, and report path. No application code, migration,
  credential, service, deployment, or live fleet state was changed.

## [1.72.0] — 2026-08-02

### Added — GenStudio-owned approved fleet model catalog

- Added an authenticated controller desired-state endpoint for one global,
  exact approved-model catalog managed in GenStudio.
- Controllers persist the last-good catalog and automatically ask each audited
  hardware-eligible sibling Studio to cache its approved models.
- The Models workspace now explains the global authority and reports approved,
  eligible, ineligible, pending, cached, and failed targets.

### Changed

- Removed the hardcoded seven-model Voice Studio baseline. New machines no
  longer download research models merely because they joined a controller.
- Site-local exposure controls become read-only after the first GenStudio
  catalog arrives, preventing different locations from drifting into separate
  sellable catalogs.
- Revocation stops new downloads and routing without deleting cached files,
  resumable partial downloads, jobs, or historical evidence.

### Verification

- Focused tests cover authenticated desired-state delivery, exact-contract
  validation, RAM eligibility, partial-download reuse, last-good persistence,
  global authority, revocation preservation, and cache-only capabilities.

## [1.71.0] — 2026-08-02

### Added — worker-owned generation resource evidence

- Studio Hub now retains Voice Studio's versioned per-job unified-memory,
  pressure, swap, process-tree, and MLX telemetry throughout polling and in the
  durable terminal job record.
- The Jobs detail table shows compact resource evidence next to the exact worker
  attempt, including peak worker RSS, lowest free memory, peak pressure, swap,
  and MLX peak allocation. Active rows continue refreshing instead of freezing
  behind the terminal-result cache.
- Only an explicit whitelist from the
  `voicestudio.resource-telemetry` v1 contract may cross the worker boundary;
  unknown fields and unknown schemas are discarded.

### Verification

- Focused tests cover terminal retention, public-result propagation, unknown
  contract rejection, private-field filtering, and dashboard rendering.

## [1.70.1] — 2026-08-02

### Fixed — catalogue evidence no longer blocks updates

- Marked the per-machine `catalog_observations.json` last-good catalogue cache
  as runtime state. The cache remains preserved across restarts and updates,
  but Git no longer treats it as an operator code change that blocks the safe
  updater.
- This completes the same runtime-state protection already applied to Hugging
  Face credential delivery metadata. No model approval, routing, or customer
  work is changed by this patch.

## [1.70.0] — 2026-08-02

### Added — private voice-reference transport

- Added an authenticated, controller-only execution-asset endpoint for staging
  a customer's voice reference by durable GenStudio asset identity and exact
  checksum. Audio and metadata are stored privately with a bounded expiry and
  can be explicitly deleted after the assigned job.
- Voice jobs can now reference the staged execution asset. Studio Hub verifies
  it, selects one eligible Voice Studio worker, and sends the audio through the
  worker's private multipart generation endpoint without exposing local paths,
  arbitrary URLs, or base64 audio in the normal job contract.
- Structured missing, expired, inaccessible, checksum, size, and format errors
  let GenStudio safely re-stage the original asset without silently falling
  back to a different voice.

### Changed — transport and scheduling remain separate from audio processing

- Studio Hub now relays Voice Studio's model-specific reference checksums,
  preprocessing revision, prepared duration, long-form strategy, and chunk
  progress while leaving segment selection, normalization, chunking, stitching,
  and final speed control inside Voice Studio.
- Capability and architecture documentation now define GenStudio as the owner
  of the original customer upload and consent record, Studio Hub as short-lived
  secure transport, and Voice Studio as the model-aware execution authority.

### Verification

- Focused tests cover private and idempotent staging, checksum binding, expiry,
  authentication, safe API responses, multipart worker forwarding, structured
  failure behavior, progress relay, and terminal audio evidence.
- The complete Studio Hub suite passes without contacting a sibling Studio or
  paid provider.

## [1.69.1] — 2026-08-02

### Fixed — portable release verification

- Studio Hub's updater unit tests now use an explicit stopped-service fixture
  instead of probing macOS `launchctl` on Linux GitHub runners.
- The Pinokio sibling-folder integration assertion now runs when the complete
  local Studio family is present and skips honestly in a standalone repository
  checkout. Runtime registry behavior remains fully tested on every platform.

### Verification

- The complete 445-test Mac suite passes locally.
- The GitHub workflow can now distinguish portable unit coverage from the two
  Mac/Pinokio installation checks instead of reporting false release failures.

## [1.69.0] — 2026-08-02

### Added — deliberate audited-model discovery for GenStudio

- Added a durable, independent sibling catalogue refresher with non-overlapping
  cycles, concurrent bounded worker requests, persisted last-good observations,
  and explicit catalogue age, stale state, and failure evidence.
- Added an owner-controlled exposure registry pinned to the exact internal model
  ID, operation, immutable runtime revision, and audited contract hash. Sibling
  Studios can submit candidates, but cannot make themselves sellable.
- Added the guided **Models offered to GenStudio** workflow for refreshing audit
  evidence, reviewing exact contracts and fleet supply, approving a candidate,
  and revoking new routing without losing history.
- Added cache-only exposure and refresh APIs plus capability schema v2. GenStudio
  now sees only owner-approved audited models and a per-model supply view derived
  from detailed machine evidence.

### Changed

- Ordinary Models-page reads no longer make live sibling catalogue requests.
  Offline workers remain visible from last-good inventory without blocking the
  operator experience.
- A changed revision or contract never inherits an older approval. Stale
  catalogue evidence remains inspectable but is unavailable for new routing.

### Verification

- Focused tests cover deliberate approval, revocation after candidate removal,
  contract-change suspension, automatic candidate discovery, distinct fleet
  states, low-memory reasons, stale last-good retention, persisted restart
  recovery, cache-only capability reads, and the guided owner controls.

## [1.68.5] — 2026-08-02

### Fixed — capability reads stay independent of worker latency

- The private GenStudio capability snapshot now assembles model and
  transcription evidence strictly from Studio Hub's last-observed caches.
- Expired cache entries remain visible as last-known capability evidence while
  worker health and readiness continue to report their current state separately.
- Capability reads never refresh a worker catalog or transcription endpoint, so
  a slow or unreachable Studio cannot delay the control-plane contract.

### Verification

- Focused capability and monitor tests prove cached image, voice, and
  transcription models survive stale caches without any worker network call.

## [1.68.4] — 2026-08-01

### Added — durable Hugging Face credential delivery

- Added a Hub-side Hugging Face credential workflow that validates a replacement
  token, stores it in the macOS Keychain, and never writes the secret to Hub
  state JSON, browser storage, or delivery responses.
- Added durable per-Studio delivery metadata with retryable states, so offline
  or temporarily unreachable Studios can be caught up from one **Retry pending**
  action instead of being updated manually.
- Updated the Models tab to show whether the credential is saved and how many
  target deliveries remain pending. The existing broadcast endpoint remains a
  compatibility alias for the durable flow.

### Verification

- Full Studio Hub test suite passes.
- Focused credential and broadcast tests verify that secrets never appear in
  persisted metadata or API responses.

## [1.68.3] — 2026-08-01

### Fixed — stale checkouts are still updated

- A no-op maintenance skip is now allowed only when the running release, the
  checked-out release, and the published family release all match.
- Machines whose checkout is behind the published release continue through
  the normal update flow instead of being incorrectly marked current.

## [1.68.2] — 2026-08-01

### Fixed — current workers are not re-downloaded

- Skip a maintenance update when the running Studio already matches the
  checked-out release, preventing stale coordinator snapshots from starting
  a no-op restart and reporting a false timeout.
- Keep resumable model download state untouched while the maintenance check
  reconciles the worker's actual version.

## [1.68.1] — 2026-08-01

### Fixed — slow Studio updates no longer become false failures

- Extended the Studio restart grace period to ten minutes so dependency refreshes
  on cold MLX machines can finish before the Hub records a failure.
- Kept the existing update job and remote job IDs intact while a restart is in
  progress, preventing duplicate updates and stale partial download records.

## [1.68.0] — 2026-08-01

### Added — complete Voice Studio test baseline

- Added Whisper Large v3 Turbo alongside Whisper Tiny, plus the requested
  Kokoro, VibeVoice Realtime, Fish Audio S2 Pro 8-bit, Chatterbox 4-bit, and
  supported OmniVoice bf16 checkpoints to the site-local Voice Studio stock
  list.
- Each worker/model target now records its memory requirement and eligibility.
  Known workers below a model's unified-memory floor are shown as
  **ineligible** instead of being placed into an endless download queue; all
  eligible targets continue to retry automatically.
- Live worker memory telemetry takes precedence over a stale configured
  hardware profile, with the profile retained as the offline fallback.

### Notes

- OmniVoice uses the supported `OmniVoice-bfloat16` checkpoint. The published
  compact row-wise 4-bit/8-bit conversions still require upstream MLX-Audio
  loader support; adding a Python package alone would not make them runnable.
- Existing Hugging Face partials remain untouched and are reused by Voice
  Studio 1.23.3 when a target is retried.

## [1.67.1] — 2026-07-30

### Fixed — truthful completed model-baseline state

- When Voice Studio safely repairs a stale Hugging Face partial and reports
  `already_cached`, the Hub now records that worker/model pair as **ready**
  immediately. It no longer presents a fake queued/running download or creates
  another terminal history row during the next baseline reconciliation.
- Active partial downloads remain resumable and untouched; this only consumes a
  completed-cache response from Voice Studio.

## [1.67.0] — 2026-07-30

### Added

- Added a durable **Reinstall generation everywhere** Hub action for the five
  sibling Studios that provide generation installers.
- Remote Hubs now execute their own trusted installer and return a job that the
  requesting Hub can monitor; package installation never runs on the wrong Mac
  or through GenStudio.

### Safety

- Installs drain active Hub work first, run serially per Mac, run in parallel
  across independent Macs, verify the installer’s `GEN_VERIFY_OK` result, and
  report unreachable or failed targets instead of claiming success.
- Normal Studio and Hub updates remain unchanged; generation reinstall is an
  explicit action because it may download large dependencies and restart a
  Studio.

## [1.66.0] — 2026-07-30

### Added — durable Fish Audio S2 Pro fleet distribution

- The site-local Voice Studio model baseline now includes the 8-bit Fish Audio
  S2 Pro model (`mlx-community/fish-audio-s2-pro-8bit`).
- GenStudio's existing per-location maintenance path now causes each Hub to
  reconcile Fish on every registered Voice Studio: reachable workers start a
  download immediately, while offline workers remain recorded for automatic
  retry after reconnecting.
- Fish is kept out of Image, Chat, Music, Video, and Render workers.

### Safety

- This change distributes and queues the model only; it does not change
  GenStudio routing, billing, customer accounts, job ownership, or Studio Hub
  authority.
- Fish Audio S2 Pro 8-bit is a large model with a 24 GB runtime-memory floor;
  machines below that floor may cache the files but remain ineligible for
  generation.

### Verification

- Extended baseline endpoint, missing-model, cached-model, and offline-retry
  coverage to four required models.

## [1.65.0] — 2026-07-27

### Added — persistent required Voice Studio models

- The site-local model baseline now keeps Whisper Tiny, Kokoro v1.0 82M, and
  VibeVoice Realtime 0.5B 4-bit cached on every registered Voice Studio.
- Each machine/model pair has an independent ready, pending, offline, or error
  state. Offline Macs remain pending and automatically download missing models
  after reconnecting instead of requiring another manual fleet broadcast.

### Changed

- The Models dashboard now presents the three required voice models together
  and reports the combined fleet-ready count without sending them to unrelated
  Image, Chat, Music, Video, or Render workers.
- Existing Whisper-only baseline state migrates automatically and remains
  compatible with older API clients that read the original top-level repo.

### Safety

- Model reconciliation remains site-local, authenticated, non-blocking, and
  independent from GenStudio routing, customer jobs, billing, and ownership.

### Verification

- Added coverage for all three model downloads, cached-model idempotency, and
  persistent offline retry state.

## [1.64.3] — 2026-07-27

### Fixed — long-form voice artifacts have time to verify

- Studio Hub now allows up to ten minutes to download and independently verify
  a completed Voice Studio WAV. A 40,000-character narration can produce a
  100–160 MB PCM file, which could exceed the former 60-second transfer window
  on a slower fleet link even though generation had completed successfully.

### Safety

- Studio Hub still receives one text item and one final artifact. VoiceStudio
  alone owns private model sections; GenStudio retains job, customer, billing,
  and publication authority.
- The existing renewable execution lease still fences stale work. Routing,
  worker selection, concurrency, checksums, media validation, and final
  artifact URLs are unchanged.

### Verification

- Added authenticated peer-transfer coverage for the long-form verification
  window and reran the complete Studio Hub suite.

## [1.64.2] — 2026-07-24

### Fixed — GenStudio Controller credential is visible again

- Controller settings now show the permanent **Hub token** plainly beside the
  registration code and fleet token, with a dedicated **GenStudio connection**
  explanation and Copy control.
- The registration code remains exclusively for enrolling Agent Hubs. GenStudio
  continues to use the Hub token for authenticated controller verification,
  capability reads, and job routing.

### Safety

- No token is rotated and no Agent, Controller, job, routing, or local dispatch
  behavior changes. Existing GenStudio site credentials remain valid.

## [1.64.1] — 2026-07-24

### Fixed

- Manual-mode updates now pass the canonical absolute `start.js` path to
  Pinokio's `script.stop` API. Pinokio 8.0.40 no longer receives the invalid
  relative `start.js` URI that could trigger an unhandled-rejection crash.

### Safety

- Startup-service updates retain their existing service-aware path. Update
  order, dependency installation, idle checks, worker dispatch, and live jobs
  are unchanged.

### Verification

- Added a launcher regression assertion that rejects bare relative stop URIs
  and requires the runtime-resolved app-local path.

## [1.64.0] — 2026-07-24

### Added — explicit machine modes

- Remote now begins with one clear **Machine mode** setting: **Standalone**
  (orange), **Agent** (green), or **Controller** (red). The selected role and
  its color are shown in both the page header and the settings card.
- Every Hub can now have a friendly display-only machine name. Renaming it
  never changes the stable Site ID, Hub ID, enrollment identity, or routing.
- Saving Controller mode automatically ensures a reusable permanent
  registration code and fleet token exist. Both are shown plainly with copy
  controls beside the Controller name, Site ID, Hub ID, and owner password.
- Agent mode now presents the operator's actual enrollment inputs together:
  Agent name, local hardware, Controller address, and registration code. A
  successful join reports that fleet settings were received and the Agent is
  ready for jobs.

### Upgrade behavior

- Existing Controller and Agent identities, fleet credentials, and joined
  machines are retained. Reachable Agent Hubs can use the normal remote update;
  offline or older Macs need a local update before they receive this screen.
- Switching away from Controller mode asks for confirmation and never silently
  deletes existing Agent configuration.

### Verification

- Added API and enrollment coverage for automatic Controller credentials,
  display-only machine names, and Agent registration. Backend, control-plane,
  and dashboard regression tests pass.

## [1.63.3] — 2026-07-24

### Fixed

- A completed GenStudio batch no longer becomes cancelled in Hub history merely
  because its execution lease expires after every item is terminal. Renewable
  leases still fence queued or running work exactly as before.

### Added

- Added edge-triggered operator alerts for genuine unfinished GenStudio lease
  expirations, repeated worker restarts reported by Studio health, and peer
  Agent Hubs that remain unreachable for three consecutive resource checks.
  Recovery alerts are emitted once when restart health or peer connectivity
  returns to normal.

### Safety

- Alerts are observability only. They never claim global work, restart a
  process, alter GenStudio ownership, or change SQLite site-local dispatch.
- No live worker, queue, or PostgreSQL setting is changed by this update.

### Verification

- Added regression coverage for completed-result preservation, one-shot lease
  alerts, repeated-restart alert debouncing, and Agent Hub outage/recovery
  detection.

## [1.63.2] — 2026-07-23

### Fixed

- Shared-voice previews now use a Play/Stop toggle. Starting another preview
  stops the first one, and refreshes also release any active browser audio.

## [1.63.1] — 2026-07-23

### Fixed

- Shared-voice transcription now accepts ordinary audio filenames containing
  punctuation such as commas and parentheses. Hub creates a safe internal job
  identifier without changing the visible filename.
- The Voices workspace now shows a prominent in-place transcription state for
  sending, success, and failure, rather than leaving a small status message
  beside the save controls.

## [1.63.0] — 2026-07-23

### Added — fenced GenStudio site failover

- Added renewable GenStudio execution leases for generation, transcription,
  and Chat batches, including an authenticated lease-renewal endpoint.
- Expired work is cancelled before dispatch, after worker completion, and
  during Hub restart recovery. An expired or superseded fencing token cannot
  be revived, so a restored power-cut location cannot publish a stale result.
- Transcription uploads now carry the same GenStudio job, attempt, fencing,
  idempotency, site, revision, and lease evidence as other fleet jobs.

### Fixed

- Fixed GenStudio release qualification repeatedly fencing voice and image
  canaries even after Studio Hub completed them. The authenticated renewal
  contract now keeps active attempts pollable until GenStudio receives and
  verifies the terminal result.

### Safety

- Expired or superseded attempts remain permanently fenced. Updating restores
  live result polling but never adopts outputs from an earlier failed run.

### Verification

- Added authenticated API coverage for the exact submit-and-renew contract,
  including rejection without a Hub token and durable batch evidence updates.

## [1.62.0] — 2026-07-23

### Added — authenticated remote Hub restart

- Added a modern **Restart Hub** control to the Hub update card and a
  **Restart** action whenever the fleet row reports `restart_required`.
- Added an authenticated `POST /api/hub/maintenance/restart` endpoint backed by
  the installed `com.kh.studiohub.server` LaunchAgent. It returns before the
  delayed restart, then the dashboard reconnects and reloads only after the
  expected version answers healthy.
- Restart safety refuses missing startup-service installations, dirty or
  divergent Git checkouts, and active Hub work by default. A caller must send
  an explicit `{"force": true}` request to override only the active-work guard;
  repository safety cannot be bypassed.

### Verification

- Added API, authentication, service-spawn, restart-safety, and dashboard
  regression coverage. No live workers or jobs are restarted by the tests.

## [1.61.5] — 2026-07-23

### Changed — automatic 30-day fleet backup retention

- Raised the generated-output backup-retention default from 3 days to 30 days
  while preserving the existing combined 80 GB hard cap per physical Mac.
- Existing saved 3-day Hub, transcription, and Studio policies migrate
  automatically once during update. Agent Hubs enforce the migration locally,
  so offline workers catch up when they update and reconnect without per-Mac
  setup.
- Explicit retention choices saved after migration remain respected. Active
  jobs, source uploads, shared voices, models, credentials, chat history, and
  pinned outputs remain protected.

### Verification

- Added regression coverage for Hub fleet-policy, Hub transcription-policy,
  and worker migration behavior, including explicit post-migration overrides.

## [1.61.4] — 2026-07-23

### Fixed — transient health misses no longer restart the controller

- The startup watchdog now requires three consecutive failed health probes
  before restarting Studio Hub. A successful probe immediately clears the
  streak, so a brief model load, update handoff, or network pause cannot create
  a restart loop.
- Added isolated watchdog tests proving the first two failures are tolerated,
  the third triggers one restart request, and recovery resets the counter.

### Verification

- Passed the complete Studio Hub test suite, watchdog shell syntax, launcher
  JavaScript syntax, release metadata coverage, and whitespace validation.

## [1.61.3] — 2026-07-23

### Fixed — model baseline state cannot block Hub updates

- Classified `model_baselines.json` as per-controller runtime state. Whisper
  Tiny reconciliation can now persist its enabled flag and target evidence
  without making the Git worktree dirty or blocking automatic updates and
  service restarts.
- Added regression coverage for the runtime-state ignore rule. No dispatch,
  model admission, customer jobs, or global GenStudio authority changed.

### Verification

- Passed the complete Studio Hub test suite, JavaScript syntax validation,
  release metadata validation, and whitespace validation.

## [1.61.2] — 2026-07-23

### Fixed — stale update history cannot offer a downgrade

- Automatic-update availability now compares semantic versions instead of
  treating any difference as newer. A stale cached `latest_version` below the
  running Hub can no longer display an update or trigger a downgrade attempt.

## [1.61.1] — 2026-07-23

### Fixed — update status verifies the running Hub

- Studio Hub no longer reports a pulled version as installed/current while the
  live service is still answering with older code. Both the Hub update card and
  fleet update inventory now show the loaded version, retain the on-disk
  version separately, and display **Restart required** until they match.
- This prevents a cross-controller maintenance run from accepting a successful
  Git pull as proof that a controller actually restarted and loaded the release.

## [1.61.0] — 2026-07-23

### Added — self-healing Whisper Tiny fleet baseline

- Studio Hub can now keep `mlx-community/whisper-tiny` (approximately 70 MB)
  cached on every registered Voice Studio transcription worker at its site.
  The Models tab shows exact per-machine ready, pending, offline, and failure
  state and provides **Check & install now** plus an automatic-policy toggle.
- Added authenticated `GET` / `POST /api/hub/model-baselines` and
  `POST /api/hub/model-baselines/reconcile` contracts for site-local operators
  and GenStudio's multi-controller maintenance view.

### Reliability and safety

- Offline Voice Studios are retained as retryable targets and reconciled when
  they return. A failed baseline download is observable but can never block the
  SQLite scheduler or customer work.
- The policy is intentionally limited to transcription-capable Voice Studios.
  Image, Chat, Render, and other workers do not waste storage on a model they
  cannot execute.
- Model-baseline telemetry contains only Studio IDs, machine IDs, reachability,
  and download state. It carries no prompts, audio, generated assets, customer
  IDs, billing data, or global job ownership.

## [1.60.3] — 2026-07-22

### Fixed — one guided worker-enrollment flow

- Replaced the role-first setup chooser with one three-step **Add this Mac to
  your fleet** flow. A normal worker now needs only the main controller address,
  permanent enrollment code, and its hardware profile; Studio Hub selects Agent
  mode and transfers the fleet credential automatically.
- Added a read-only **Check connection** action that verifies the selected Hub,
  location, version, controller role, and enrollment-code readiness without
  sending the code or changing either Mac.
- Controller addresses now accept common Tailscale forms: an IP or MagicDNS
  hostname with no scheme, direct HTTP with an omitted port, and explicit HTTPS
  Tailscale Serve URLs. Direct HTTP defaults safely to Studio Hub port 47873.
- Join failures now distinguish invalid addresses, DNS failures, timeouts,
  unreachable Hubs, outdated Hubs, the wrong location Hub, missing enrollment
  setup, and using a Hub/fleet token instead of the permanent enrollment code.

### Changed

- Existing location managers and joined workers show their configured state
  instead of the setup form. Starting a brand-new location is a secondary,
  clearly labelled path, and raw role/PostgreSQL controls remain collapsed
  under **Technical recovery settings · rarely needed**.
- An owner-authenticated Tailscale browser may configure the Hub it is signed
  into, matching local setup convenience. Anonymous and fleet-token-only remote
  requests remain unable to change a Hub's location.

### Safety

- Enrollment discovery exposes only non-secret site identity, Hub version,
  role, and code-ready state over private LAN/Tailscale sources. It never
  exposes the enrollment code, Hub token, or fleet token.
- Connection checks are read-only. Existing rollback, private-network,
  controller-role, permanent-code, local SQLite, and agent submission
  protections remain unchanged.

## [1.60.2] — 2026-07-22

### Fixed — generic GenStudio completion responses

- Studio Hub now accepts GenStudio's explicit `completion` chat-batch kind and
  preserves the model response as one opaque result, including valid JSON
  objects whose keys are ordinary response content rather than scene IDs.
- Completion batches require exactly one result ID, keeping the handoff
  unambiguous for GenStudio's durable LLM job and settlement record.

### Safety

- Existing Story Studio visual and motion batches keep their current parsing
  contract. The new behavior applies only to requests explicitly marked as a
  generic completion and never triggers another model invocation.

## [1.60.1] — 2026-07-22

### Fixed — exact local ChatStudio selection

- GenStudio-assigned local LLM batches now remain pinned inside Studio Hub to
  a ChatStudio worker that reports the exact immutable model revision, verified
  native token usage, and enough output-token capacity for the request.
- Older workers that only report the same mutable model name are skipped. They
  remain available for compatible legacy direct work and can rejoin GenStudio
  routing after updating and reporting the full capability contract.

### Safety

- Existing queued and running jobs are not moved. The stricter filter applies
  only when selecting a worker for newly dispatchable GenStudio chat work.

## [1.60.0] — 2026-07-22

### Added — verified local LLM execution handoff

- Chat capability snapshots now carry immutable model revisions, output-token
  limits, and ChatStudio's explicit verified-token-usage capability to
  GenStudio's site assignment engine.
- GenStudio chat batches now use the same restart-safe execution identity,
  idempotency, and fencing guard as other local modalities. Exact replays return
  the original local batch without creating duplicate work.
- Completed chat batches preserve tokenizer-native usage and model-revision
  evidence from ChatStudio for GenStudio settlement and audit.

### Safety

- GenStudio-assigned chat work rejects missing or inconsistent token evidence
  and any runtime revision mismatch. Legacy direct Story Studio chat batches
  remain compatible when they do not carry GenStudio execution identity.
- Existing queued and running work is not moved, cancelled, or rewritten by
  this release.

## [1.59.0] — 2026-07-22

### Added — permanent enrollment and fleet startup control

- Replaced expiring, single-use agent enrollment codes with one permanent,
  reusable site enrollment credential. Owners can reveal, copy, rotate, or
  revoke it from **Remote**; already enrolled Macs remain connected after a
  rotation or revocation.
- Added **Automatic startup across the fleet** to audit Image, Voice, Chat,
  Music, Video, and Render Studio startup services on this Mac and every
  reachable peer Hub. The table distinguishes installed, missing, incomplete,
  unsupported, app-missing, outdated-Hub, and unreachable states.
- Added per-Studio **Install/Repair** controls and a sequential **Install
  missing** action. Each target machine runs and verifies its own sibling
  Studio installer through its authenticated local Hub.

### Changed

- Permanent enrollment claims remain limited to loopback, private LAN, and
  Tailscale sources. The claim database stores only the SHA-256 digest and use
  metadata; a separate mode-0600 controller file keeps the owner-visible value
  available after restarts.
- Previously issued expiring codes retain their original expiry and single-use
  behavior after the backward-compatible SQLite schema migration. Creating the
  new permanent credential rotates only permanent credentials.

### Safety

- Startup auditing is read-only and never installs anything automatically.
  Installation accepts only a regular `install_service.sh` inside a known
  sibling app, enters maintenance mode, refuses Hub-tracked active work, runs
  one target at a time, and verifies both launchd server and watchdog labels.
- Remote startup operations use the existing fleet credential and always run
  on the target machine's own Hub. No live workers or jobs are changed merely
  by updating Studio Hub or opening the audit.

## [1.58.3] — 2026-07-22

### Fixed — visible one-time enrollment codes

- Added explicit **Reveal/Hide** and **Copy code** controls to newly created
  10-minute agent enrollment codes. The value remains masked by default.
- Reveal state is cleared whenever a code is replaced or expires, at which point
  both Reveal and Copy become unavailable.
- Code copying now uses the same safe private-HTTP fallback as the Hub and fleet
  credentials, so it works from local and Tailscale dashboards without storing
  the code in browser storage.

### Safety

- Enrollment codes remain memory-only, single-use, and limited to ten minutes;
  the controller still persists only their SHA-256 hashes and expiry metadata.

## [1.58.2] — 2026-07-21

### Fixed — readable dashboard pickers and typography

- Increased every native select menu to a 15 px control face with 16 px picker
  options, fixing the tiny model and hardware picker text on macOS.
- Replaced the dashboard's scattered 8–11.5 px text with a shared readability
  scale across Models, Remote, Jobs, Updates, Resources, Memory, Voices, tables,
  badges, inline code, and mobile navigation. Compact readable text now has a
  true rendered 12 px floor.
- Added a frontend regression guard that rejects future literal font sizes below
  12 px and verifies the shared control and native-picker sizing contract.

### Safety

- This is a frontend-only release. It does not restart workers, alter active
  jobs, or change SQLite dispatch, GenStudio routing, or PostgreSQL settings.

## [1.58.1] — 2026-07-20

### Fixed — owner token visibility and copying

- Added explicit **Reveal/Hide** and **Copy** controls for both the Hub token
  and fleet token. Credentials remain masked by default and are never saved in
  browser storage or written to logs.
- Token copying now includes a safe fallback for private HTTP/Tailscale
  dashboards where the browser Clipboard API is unavailable.
- Remembered owner sessions may reveal these credentials over Tailscale, while
  requests authenticated only with a Hub or fleet machine token still receive
  status without either secret.

## [1.58.0] — 2026-07-20

### Added — simple private controller and agent enrollment

- Added a prominent **Remote → Set up this Mac** wizard. A new controller now
  needs only its location name, stable site ID, and local hardware profile; an
  agent needs only the private controller URL, one-time code, and its hardware.
- Controllers can create high-entropy enrollment codes that expire after ten
  minutes and work once. Only SHA-256 hashes and expiry/use metadata are stored
  in an owner-only SQLite database.
- Agent claims are restricted to private LAN/Tailscale links. The local join
  validates the complete response before saving role, site identity, parent
  controller, site fleet credential, and local hardware assignment.

### Changed

- The established role, PostgreSQL-shadow, fleet-token, and recovery controls
  remain available behind **Advanced settings and manual recovery**.
- Hub, fleet, and enrollment secrets are masked by default in the dashboard;
  setup codes and tokens are never placed in URLs or localStorage.

### Safety

- New controllers and agents always use `database_mode=off`. Agents clear any
  local PostgreSQL credential and continue refusing customer submissions.
  SQLite remains the site-local scheduler, while GenStudio remains the sole
  global job, routing, retry, fencing, billing, and asset authority.
- Network and validation failures make no local changes. A local setup commit
  restores prior identity, credential, and hardware files if a later write
  fails, as far as the filesystem permits.

## [1.57.1] — 2026-07-20

### Fixed — exact model revision is enforced before worker assignment

- GenStudio attempts with a pinned model revision now dispatch only to a worker
  whose authenticated catalog advertises that exact immutable cached snapshot.
  Missing, mutable, or different revisions leave the job safely queued for a
  compatible worker instead of wasting compute and failing after generation.
- The guard applies consistently to every Studio Hub local modality while
  preserving the existing route for Story Studio and other legacy callers that
  do not yet supply GenStudio execution identity.

### Safety

- Studio Hub still cannot invent or advance global ownership or revision
  evidence. GenStudio remains the job authority, SQLite remains the site-local
  scheduler, and charging remains outside Studio Hub.

## [1.57.0] — 2026-07-20

### Added — adjustable per-model RAM admission

- Added a transparent **Models -> RAM admission guard** editor showing the
  Studio catalog requirement, Hub default, effective total-RAM floor, current
  free-RAM floor, and whether the value comes from the catalog, a fleet default,
  or an operator override.
- Owners can save or reset a persistent override for each locally brokered
  Image, Voice, Music, or Video model. Changes
  immediately wake the SQLite dispatcher so queued work is re-evaluated; no
  worker restart or active-job interruption is required.
- Added authenticated RAM-admission read/update/reset APIs and included the
  effective policy in the Models API and private GenStudio capability snapshot.

### Fixed

- FLUX.2 Klein 4B MLX 4-bit now uses the measured fleet default of 8 GB total
  RAM with 2 GB currently free. ImageStudio's newly conservative 16 GB catalog
  value remains visible for provenance but no longer excludes proven 8 GB Macs.
- Download size remains excluded from runtime RAM calculations. Too-small Macs
  are skipped, temporarily pressured Macs wait, and worker memory refusals still
  requeue safely instead of consuming a generation attempt.

### Safety

- Overrides are site-local operator policy, not GenStudio global job authority.
  Cloud models remain outside local RAM admission, and physical-machine leases
  still prevent sibling Studios from starting overlapping heavy workloads.

## [1.56.1] — 2026-07-20

### Added — GenStudio integration handoff

- Added a self-contained GenStudio capability-integration handoff covering
  authentication, freshness, routing semantics, shared physical-Mac capacity,
  failure handling, privacy constraints, and required client tests.
- Added a mandatory repository release rule: every committed change, including
  documentation and integration contracts, must increment `VERSION` and add
  matching changelog and dashboard **What's New** entries.

## [1.56.0] — 2026-07-20

### Added — private GenStudio site-capability contract

- Added authenticated `GET /api/hub/capabilities`, schema
  `studiohub.site-capabilities` version 1, for GenStudio's private site router.
  It composes the existing health, capacity, registry, hardware-profile,
  catalog, maintenance, and scheduler state without changing worker dispatch.
- The snapshot reports controller readiness/drain state, physical machines,
  registered hardware profiles, worker versions and availability, shared local
  capacity, supported operations, sanitized model controls/limits, voice modes,
  and immutable runtime revisions when workers actually report one.
- Missing or mutable model revisions remain explicitly unqualified instead of
  receiving a Hub-generated replacement. Current execution availability and
  revision-pinning readiness are reported independently.
- The contract requires a Hub or fleet token in an `Authorization: Bearer` or
  `X-Hub-Token` header even on loopback. Browser sessions, cookies, and URL
  tokens do not authenticate this machine-to-machine endpoint.

### Safety

- Capability output uses an explicit field allowlist. It cannot include cache
  paths, credentials, customer prompts/text, generated content, artifacts,
  GenStudio job/attempt IDs, idempotency keys, or fencing tokens.
- GenStudio remains the global routing/job/billing/retry authority; SQLite
  remains the site-local scheduler; PostgreSQL remains optional shadow evidence
  only. No outbound connector, global claiming, or worker mutation was added.

## [1.55.0] — 2026-07-20

### Changed — permanent GenStudio architecture boundary

- Established GenStudio KH as the sole global authority for customer jobs,
  accounts, billing, idempotency, attempts, fencing, retries, cross-location
  routing, final customer state, and assets. Studio Hub remains only the
  site-local execution authority with SQLite as its permanent scheduler.
- Added validation and durable local evidence for externally supplied GenStudio
  job IDs, attempt IDs, idempotency identity, fencing token, site, operation,
  model revision, and voice revision. Exact replay returns the same local batch;
  conflicting replay and stale job/attempt fences are rejected before dispatch.
- Added a backward-safe schema clarification that marks migration 001's
  ownership-shaped leases, attempts, and fencing fields legacy/reserved. The
  PostgreSQL runtime writes non-authoritative execution evidence only and has no
  global claim, retry, refund, transfer, or token-generation path.
- Agent mode now removes saved PostgreSQL credentials and ignores inherited
  database credentials. PostgreSQL remains optional, off by default, and
  shadow-only for site heartbeat, inventory, capacity, and execution evidence.
- Optional telemetry failure no longer makes the healthy SQLite scheduler
  unready. Existing local Story Studio/GenStudio requests, worker dispatch,
  hardware registration, and health/capacity contracts remain compatible.

### Safety

- No database was provisioned or activated, and no live worker, queue, or job
  was restarted, drained, disabled, or changed for this release.

## [1.54.0] — 2026-07-20

### Added — reusable machine hardware registration

- Added the approved Mac mini, MacBook, and iMac hardware catalog to the Remote
  registration flow, including planned-unit counts and stable suggested machine
  IDs that match GenStudio's operating-cost profile IDs.
- Hardware profile assignments persist independently of Studio records, can be
  changed for an existing machine, and are removed when that machine is
  unregistered. Operators can add future hardware classes from the dashboard.
- Studio and resource APIs now publish each machine's selected hardware profile,
  giving GenStudio an explicit identity instead of relying on name inference.
- The dashboard requires a hardware selection when registering a new Mac and
  shows an editable profile control on every registered-machine row.

## [1.53.0] — 2026-07-20

### Added — controller backend and PostgreSQL migration foundation

- Added explicit Standalone, Controller, and Agent roles using the same Studio
  Hub build. Agent Hubs continue local monitoring, lifecycle, gateway, memory,
  storage, and update duties while refusing new customer-owned queue submissions.
- Added durable site/controller identity and a modern setup panel under Remote.
  PostgreSQL credentials are kept in a separate owner-only file and are never
  returned by the API or included in normal settings.
- Added `/health/live`, `/health/ready`, and `/health/capacity` for future
  GenStudio site routing, plus authenticated controller setup/check endpoints.
- Added the first PostgreSQL schema for sites, controllers, machines, Studios,
  jobs, items, attempts, leases, fencing tokens, and audit events.
- Added a safe shadow migration runtime that publishes ten-second heartbeats,
  fleet inventory/capacity, and generation, Chat, and transcription job state.
  SQLite remains authoritative; PostgreSQL outages cannot fail local job saves.
- The ownership-shaped fields in this initial schema are legacy/reserved.
  GenStudio permanently owns global claims, attempts, fencing, and failover;
  Studio Hub writes PostgreSQL execution evidence only.

## [1.52.0] — 2026-07-20

### Added — per-Studio fleet scheduling controls

- Added an independent pause/resume control for every registered Image, Voice,
  Chat, Music, Video, and Render Studio in the Remote machine table. Operators
  can dedicate one Mac to selected job types without disabling the whole node.
- App-specific choices persist across Hub restarts. The existing machine toggle
  remains the master switch without erasing each app's saved choice.
- Pausing is drain-safe: running work finishes normally, while the Studio stays
  online for health monitoring, lifecycle control, and remote updates.
- Enforced the control across the main generation/render broker and the separate
  Chat and Transcription queues, including explicitly routed jobs.

## [1.51.0] — 2026-07-20

### Added — fleet model-memory controls

- Added a dedicated Memory workspace for every registered Image, Chat, Video,
  Music, and Voice Studio on local or remote Macs. Select any combination of
  workers, apply one shared policy, and see each result independently.
- Performance remains the explicit default and preserves loaded models for the
  fastest next generation. Balanced releases after 10 idle minutes, Memory
  Saver after 2 minutes, and Immediate releases when current work finishes.
- Release selected now asks each Studio to unload its model and clear available
  MLX, Metal, or PyTorch allocator caches. Busy Studios refuse safely without
  interrupting generation, while successful Studios stay successful if another
  worker is offline, old, or busy.
- Studio Hub uses the friendly Activity Monitor title `Studio Hub Mac`; each row
  also reports the sibling Studio's friendly process title.

### Fixed — What's New stays current

- The version badge now loads release details directly from `CHANGELOG.md`
  through `GET /api/releases`, with the embedded highlights retained only as an
  offline fallback. Newly shipped Hub versions can no longer be omitted from
  What's New because a second handwritten list was not updated.

### Verification

- Added direct/peer routing, authentication, partial-success, busy/offline,
  validation, Activity Monitor title, release-detail, and dashboard contract
  regression coverage. Existing launcher and fixed-port behavior are unchanged.

## [1.50.0] — 2026-07-19

### Added — hardware-aware agent machine sorting

- The Remote agent-Hub table now shows each Mac's Apple chip and unified RAM
  and can be sorted by availability, name, chip generation, or RAM.
- Added a one-click direction toggle with natural defaults: A–Z for names and
  highest/newest first for RAM and chips. The browser remembers both choices.
- Hardware details come from each Mac's own Hub and remain last-known when a
  Mac goes offline. Older agents temporarily fall back to structured machine
  names until they receive this update.

### Verification

- Added backend hardware-snapshot and WebUI contract coverage. Launcher,
  update, scheduling, and generation behavior remain unchanged.

## [1.49.4] — 2026-07-19

### Fixed — storage status distinguishes optional apps from offline Macs

- Fleet storage cards no longer display an unreachable Mac as safely using
  zero bytes. Offline Hubs are visibly marked and explain that local hourly
  enforcement resumes automatically after reconnection.
- Optional Studio stores that are not installed on a worker are reported as
  inactive instead of being presented as alarming Studio failures.

## [1.49.3] — 2026-07-19

### Fixed — rolling updates remain bounded when a Mac disappears

- Remote Studio updates still reconnect after ordinary restart-related
  connection drops, but a Mac that stays completely unreachable for three
  minutes no longer blocks every Studio queued behind it for up to 20 minutes.
  The result explicitly asks for a version rescan before retrying because the
  remote update may have completed independently.
- Updating an offline local Studio no longer crashes when its last monitor
  record contains a null health payload.

## [1.49.2] — 2026-07-19

### Fixed — stale untracked Hub processes become recoverable services

- Update now recognizes an owned Hub listener on port 47873 even when both its
  launchd service marker and Pinokio's running-script state are missing. The
  safe service installer can take over that stale process, restart the newly
  pulled version, and restore future remote-update control without touching an
  unrelated port owner.

## [1.49.1] — 2026-07-19

### Fixed — remote Hub updates recover a missing service marker

- One-click and fleet updates now detect the loaded launchd Hub service as the
  source of truth and restore `service/.installed` before choosing the restart
  path. A Mac can no longer pull the new code yet keep serving an old process
  merely because that local marker was missing.

## [1.49.0] — 2026-07-19

### Added — self-healing fleet storage protection

- Jobs now includes a modern fleet storage controller with a default three-day
  retention window and one combined 80 GB limit per physical Mac. Policy saves,
  immediate cleanup progress, per-Mac usage, app contributions, and unreachable
  nodes are visible from the main Hub.
- Every Hub enforces its own Mac hourly and the primary Hub propagates policy and
  manual cleanup to reachable peer Hubs. Image, voice, music, video, render, and
  Hub transcription stores participate through their protected cleanup APIs;
  Chat reports that it has no disposable media.
- When a Mac crosses its combined limit, the largest eligible store is reduced
  first and cleanup continues oldest-first. Active work, shared voice masters,
  source uploads, models, chat history, credentials, pinned renders, and
  unreturned results remain protected even if the Mac stays over the limit.

### Changed — Hub transcription backup defaults

- Completed transcription files now default to three-day retention with
  automatic cleanup enabled. Capacity cleanup removes only local input/SRT
  files while preserving terminal job metadata in the dashboard.

## [1.48.1] — 2026-07-19

### Fixed — GenStudio jobs avoid stale Voice Studio workers

- GenStudio-labelled voice batches now wait for Voice Studio 1.20.13 or newer,
  which reports the immutable model and voice revisions required before
  GenStudio can publish an asset or capture credits.
- Older workers remain available to existing direct Story Studio jobs while a
  safe rolling fleet update drains and upgrades them one at a time.

## [1.48.0] — 2026-07-19

### Added — exactly-once client batch submission

- `POST /api/hub/jobs` now accepts a stable `clientRequestId`. Replaying the
  same request returns its existing batch, including after a Hub restart,
  while reusing the ID for different work is rejected.
- GenStudio can safely recover when a local network interruption hides the
  original submit response without generating the customer's audio twice.

## [1.47.8] — 2026-07-19

### Fixed — terminal voice results publish atomically

- A voice item now becomes `done` only after WAV metadata, revision evidence,
  and its stable Hub asset identity are finalized. Fast pollers can no longer
  observe a partial terminal result and reject an otherwise valid WAV.

## [1.47.7] — 2026-07-19

### Added — immutable Qwen generation evidence for GenStudio

- Voice terminal results now carry the exact model snapshot revision and
  preset or cloned-reference voice revision reported by Voice Studio.
- Clone results also retain the stable Hub `voice_library_id`; built-in voices
  retain the exact `preset_speaker`, allowing GenStudio to audit dispatch
  without exposing worker-local paths or addresses.
- Missing revision evidence remains non-billable: GenStudio rejects it before
  credit capture while the Hub preserves the completed artifact for diagnosis.

### Fixed — image download size no longer strands generation queues

- The memory governor no longer mistakes a Studio catalog's `size_gb`
  download/disk figure for runtime unified-memory usage. This latent mismatch
  became visible when Image Studio corrected FLUX.2 Klein 4-bit from a 2.3 GB
  estimate to its real 4.6 GB repository size, which made the Hub incorrectly
  demand 5.6 GB free before dispatch.
- Local workloads retain a 2 GB live-memory pressure floor, total-machine RAM
  compatibility checks, physical-machine leases, worker MemoryGuard handling,
  capacity rerouting, and automatic retries. Studios can now publish an
  explicit `min_free_memory_gb` runtime floor, and Hub production policy can
  raise it for workloads such as Qwen3-TTS.

### Fixed — Whisper recovery routes around unstable workers

- A failed transcription request now marks its Voice Studio as temporarily
  avoided and invalidates its cached readiness before the item is retried.
  Subsequent retries prefer another eligible model-ready worker instead of
  repeatedly returning the same connection failure.
- Terminal transcription failures now include the workers attempted, making a
  real Voice Studio, peer-Hub, or network outage actionable without discarding
  successful chapter outputs.

### Fixed — Qwen 0.6B voice uses 8 GB M1 capacity safely

- Qwen3-TTS 0.6B Base and CustomVoice are supported on 8 GB Apple-silicon
  workers. The Hub now requires an 8 GB machine with at least 3.2 GB live free
  memory for a safe cold load, rather than incorrectly excluding 8 GB workers.
  When that memory is unavailable, the item waits or runs on another eligible
  worker; 8 GB Macs remain available for image work throughout.

## [1.47.2] — 2026-07-19

### Fixed — runtime preferences cannot block future updates

- Alert delivery preferences and job-storage cleanup settings are now ignored
  as per-machine runtime data. Saving either setting no longer makes the Hub
  repository appear dirty or blocks its safe updater.

## [1.47.1] — 2026-07-19

### Fixed — updates no longer look busy for 20 minutes when nothing restarted

- Studio and agent-Hub update verification now fails with an actionable updater
  status/log message when the target never begins restarting within three
  minutes. The full 20-minute recovery window remains available after an
  actual restart begins.
- A peer Hub's HTTP 409 explanation is preserved in the primary dashboard, so
  an already-running remote operation is shown directly instead of a generic
  HTTP/MDN error.

## [1.47.0] — 2026-07-19

### Added — unattended fleet reliability protection

- The memory governor now uses each connected peer Hub's live host-memory
  telemetry before remote dispatch. A worker MemoryGuard refusal is treated as
  capacity pressure: the item waits, tries another Mac, and does not consume a
  generation attempt.
- Connection drops, timeouts, and gateway 502/503/504 responses now receive a
  bounded 30-minute self-healing window with up to eight attempts and
  progressive backoff. The original worker job is reconciled before retrying,
  preserving duplicate-generation protection.
- Repeated connection failures temporarily quarantine the physical Mac, and an
  item avoids its recently failed machine so another compatible worker can
  steal it. Successful work automatically closes the circuit.
- Health probes require three consecutive failures before declaring a Studio
  down and two successful probes before returning it to the scheduler. Active
  leases continue to suppress expected inference-time health timeouts.
- Resources now reports Pinokio Caddy memory and file-descriptor use for local
  and connected peer Macs. Abnormal Caddy growth raises a one-time alert with a
  recovery notification, making HTTPS port conflicts visible before they
  consume generation memory.

## [1.46.1] — 2026-07-19

### Fixed — Render Studio automatic-update parity

- Added the canonical local Render Studio row to the Hub's automatic-update
  inventory. Render now participates in per-app Off/Notify/Auto controls,
  **Check all**, individual updates, and staggered **Update idle apps** runs.
- The Render settings shortcut opens its own automatic-update controller, while
  rolling operations continue to authenticate, reconnect after restart, and
  require published-version health before advancing.

## [1.46.0] — 2026-07-19

### Added — fleet-wide shared voice rename and removal

- Every shared voice card now has **Rename** and **Delete** controls. Renaming
  preserves the stable voice ID, canonical audio hash, local provider mappings,
  embedding caches, and existing project references while synchronizing the new
  metadata to every Voice Studio Mac.
- A rename requested during an active sync automatically queues a fresh metadata
  pass, preventing the earlier in-flight snapshot from winning.
- Deleting removes the Hub's canonical audio immediately and sends a
  hash-verified managed-delete request to each reachable Voice Studio. Workers
  refuse to remove unrelated local voices or a stable-ID collision with a
  different audio hash.
- The Hub retains only a tiny deletion tombstone after removing the media.
  Offline, restarting, newly registered, and later-returning Macs are reconciled
  automatically until every managed copy is gone. Pending removal cards show
  per-machine progress and expose **Retry removal**.

### API

- `GET /api/hub/shared-voices` now includes active deletion operations.
- Added `DELETE /api/hub/shared-voices/{id}` and
  `POST /api/hub/shared-voices/{id}/delete-sync`.

## [1.45.0] — 2026-07-18

### Added — durable, self-healing fleet updates

- Automatic rolling-update jobs and their progress now survive a Studio Hub
  restart. On startup, the Hub safely reconnects to or resumes unfinished app
  updates instead of losing the operation from the dashboard.
- Busy sibling Studios now receive a durable update-after-current-work request
  on their own scheduler. The dashboard distinguishes that safe queued state
  from a failure and can retry only failed apps with one click.
- Transient Studio connection failures use bounded retries and keep a visible
  reconnect count. Slow agent Hubs receive four connection attempts before the
  remote update reports them unreachable.
- Remote Studio and agent-Hub update history is persisted. If the primary Hub
  itself restarts mid-operation, the interrupted row remains visible and
  actionable instead of disappearing or staying falsely active.

### Fixed — Hub runtime data blocked its own updater

- `.hub_password.json`, `.hub_sessions.json`, and `render_uploads/` are now
  correctly classified as private runtime data. They no longer make every
  automatic update fail the clean-worktree safety check, while real source
  edits remain protected.
- A genuine dirty-worktree refusal now lists the exact blocking paths in the
  update result so it can be resolved remotely without guessing.

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
