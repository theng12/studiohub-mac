# On-Demand Fleet Job Details Design

## Status and authority

This design was approved by the owner on 2026-08-31. It extends the live fleet activity design in `2026-08-30-live-fleet-activity-design.md` without weakening that design's privacy-safe polling contract. This document is the durable source of truth for implementation across Studio Hub KH, Image Studio KH, and Voice Studio KH.

## Owner outcome

From Studio Hub's Stats page, the owner can open a current or recent Image/Voice job and understand both what it did and where it came from. The detail view can show the Image prompt, Voice text and reference transcript, available reference media, generated outputs, and useful job parameters. Images and audio can be previewed, opened, or downloaded from the controller.

Sensitive content is fetched only after the owner selects **View details**. Studio Hub does not add prompts, transcripts, file contents, reference media, or full request parameters to its fleet activity ledger, alerts, logs, browser storage, or a new central history store.

## Scope

This phase covers jobs already represented by the live fleet activity contract: Image generation and Voice speech-generation jobs with an exact originating Studio and job ID. For Voice, `transcript` means the speech text and any reference transcript attached to that TTS job. This phase does not independently add subtitle/transcription workloads that are not yet represented in live fleet activity; those require their own activity contract before they can have a reliable details action.

The four stable origin labels are:

1. `Studio Hub` — dispatched by a controller; include the controller/location name when known.
2. `Local Studio UI` — started from the same Mac's official Studio interface; include the worker machine name.
3. `API/automation` — started through an API or automation; include an authenticated or server-derived device/client name when available.
4. `Unknown/legacy` — old jobs or jobs whose origin cannot be proven.

The label is always visible in the machine activity row and in the details drawer. Optional device/client text is supplementary and never replaces the stable label.

## Selected architecture

### Keep polling privacy-safe

`GET /api/fleet/activity` remains the small authenticated telemetry endpoint used by the five-second monitor poll. It continues to omit prompts, transcripts, paths, file handles, reference media, assets, and full parameters.

Because the current activity contract has not yet shipped from its open release branches, its `kh-studio.activity.v1` job object may gain these optional bounded scalar fields before release:

```json
{
  "origin": "hub",
  "origin_device": "Studio Hub KH · PPS"
}
```

`origin` accepts only `hub`, `local_ui`, `api`, or `unknown`. `origin_device` is optional, bounded display text derived from trusted server-side evidence. Missing fields are interpreted as `unknown`, so older reporters remain compatible. No origin field is accepted from an arbitrary request body and presented as verified.

Hub broker ownership remains authoritative for `hub`. The controller attaches its known controller/location label to its own broker evidence. The official Studio frontend may mark a request as `local_ui` only on a loopback request. Remote API/device naming is used only when an authenticated Hub/peer identity or another server-controlled identity is available; otherwise the row says `API/automation` without a device name. User-supplied headers and parameters are not trusted as device identity.

### Fetch one exact job on demand

Image and Voice each add an authenticated read-only endpoint:

```text
GET /api/fleet/jobs/{job_id}/details
```

The endpoint resolves `job_id` through the Studio's existing generation manager and durable local job history. It returns `404 job_not_found` when the job no longer exists. It never accepts a filesystem path or a free-form media URL.

The versioned response is `kh-studio.job-details.v1`:

```json
{
  "schema": "kh-studio.job-details.v1",
  "studio": "image",
  "job": {
    "id": "job-id",
    "state": "done",
    "model": "owner/repository",
    "operation": "txt2img",
    "created_at": 1788120000.0,
    "started_at": 1788120002.0,
    "finished_at": 1788120018.0,
    "runtime_s": 16.0,
    "origin": "hub",
    "origin_device": "Studio Hub KH · PPS"
  },
  "inputs": {
    "prompt": "...",
    "negative_prompt": "...",
    "text": null,
    "reference_transcript": null,
    "parameters": {}
  },
  "references": [],
  "outputs": []
}
```

The projection is operation-specific and allowlisted. It includes useful user-facing generation settings but excludes internal paths, credentials, Hugging Face tokens, environment values, private checksums not needed for display, and implementation-only fields prefixed with `_`. Image may return prompt, negative prompt, dimensions, aspect ratio, steps, guidance, seed, strength, quantization, and public LoRA names. Voice may return speech text, reference transcript, selected voice name/ID, language, delivery controls, chunk/section information, and safe output metadata.

The detail response receives `Cache-Control: no-store, private, max-age=0`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`. Sensitive values are not included in exception messages or request logs.

### Reuse the existing Hub gateway

The controller browser requests the detail through Studio Hub's existing authenticated Studio gateway:

```text
/studio/{studio_id}/api/fleet/jobs/{job_id}/details
```

For a remote worker, `peers.studio_request()` continues routing through that machine's Agent Hub and replaces browser-facing credentials with the saved Hub/Studio fleet credential. The browser never needs an individual Studio token, a new sign-in, or a direct connection to the remote Studio.

The existing streaming gateway remains responsible for backpressure and closing upstream responses on completion or disconnect. No new port, daemon, callback, permanent credential, external store, or product dependency is introduced.

## Media access

### Opaque short-lived handles

References and outputs contain metadata plus a short-lived opaque handle, never a filesystem path:

```json
{
  "kind": "output",
  "name": "image.png",
  "media_type": "image/png",
  "size_bytes": 123456,
  "duration_s": null,
  "handle": "opaque-value",
  "expires_at": 1788120312.0
}
```

Each Studio issues handles valid for five minutes. A handle is bound to the exact job, media kind, item index, and expiry. It is authenticated with the existing fleet secret using Python standard-library HMAC-SHA256; it contains no raw path and introduces no new credential or persistent token table. The Studio re-resolves the recorded job media at access time, so a signed handle cannot outlive a deleted or pruned file.

Media is fetched from:

```text
GET /api/fleet/jobs/{job_id}/media/{handle}
```

The Studio verifies fleet authentication, signature, expiry, job binding, file existence, canonical allowed root, MIME allowlist, and the media reference recorded on that job. It refuses symlinks or resolved files outside approved Studio-owned roots. Query parameters cannot override the resolved file.

The opaque handle may appear as a URL segment in local HTTP access logs. It contains no content or path, is unusable without the existing authenticated Hub/Studio session, and expires within five minutes. Application error and audit messages do not repeat it.

Allowed preview types are the Image formats already accepted/produced by Image Studio and the audio formats already accepted/produced by Voice Studio. The implementation reuses each Studio's existing upload/reference limits and output policy rather than inventing a second set of limits. The HTTP response streams the file, supports the incumbent `FileResponse`/range behavior, and sets `no-store`, `nosniff`, a safe filename, and attachment disposition only for explicit download.

Closing the drawer aborts active browser fetches and media streams. Issued handles are not persisted and expire after five minutes even if the close signal is lost.

## Stats interface

Every current job and recent activity item with a Studio/job ID receives a **View details** action. It opens an accessible drawer without navigating away from Stats.

The drawer contains:

- status, machine, Studio, operation, model, job ID, timing, duration, and progress;
- the stable origin label and optional verified device/client name;
- Image prompt/negative prompt or Voice speech text/reference transcript, with Copy controls;
- useful generation settings presented as labelled values rather than raw JSON;
- inline reference-image/reference-audio previews when the original file remains available;
- generated image gallery or audio player;
- explicit **Open** and **Download** controls.

No sensitive detail is prefetched with Stats. While the drawer is open for an active job, metadata may refresh through the same on-demand endpoint at a modest interval; it stops on close. Closing the drawer removes fetched text and media URLs from the DOM and aborts outstanding requests. The frontend stores none of it in local storage, session storage, IndexedDB, query strings, analytics, or the service worker/browser cache.

The drawer preserves keyboard focus, traps focus only while open, closes with Escape, restores focus to the invoking button, exposes loading and error status to assistive technology without repeated announcements, and remains usable at narrow mobile widths.

## Error and compatibility behavior

The interface distinguishes:

- `unsupported_version` — update the originating Studio to enable details;
- `machine_offline` — retry when the Agent Hub is reachable;
- `job_not_found` — local history was cleared or the job expired;
- `media_removed` — metadata remains but the original file was pruned;
- `handle_expired` — silently request fresh details once, then show Retry;
- `permission_denied` — enrollment/authentication needs attention;
- `temporarily_unavailable` — bounded network or Studio error.

Failure to load details never changes Studio health, current activity state, job success, scheduling, or historical Stats. Old Image/Voice versions continue supplying activity when supported and show details as unavailable rather than failing the fleet row.

## Retention and privacy boundaries

- Studio Hub adds no sensitive columns or payload JSON to `activity_events`, `machine_state_transitions`, alerts, logs, or control-plane shadow records.
- The details response is constructed from the originating Studio's existing in-memory/durable job record and existing local media. It does not extend that Studio's retention policy.
- Existing Hub broker/recovery records required for Hub-dispatched work remain unchanged; this feature does not create another copy of their content.
- The controller keeps only non-sensitive activity scalars and origin labels in its established 30-day operational history.
- Job cleanup and output pruning remain authoritative. Once the Studio removes a job or file, the controller reports it unavailable and does not resurrect it from another cache.

## Implementation boundaries

The work remains inside the three existing open release branches and releases:

- Studio Hub KH `2.14.0`
- Image Studio KH `1.31.0`
- Voice Studio KH `2.5.0`

Image and Voice own detail projection, media validation, and handle verification. Studio Hub owns broker-backed origin correction and the Stats drawer. Shared behavior is expressed through matching small contracts and contract tests; no cross-repository package or new framework is introduced.

GenStudio APIs, routing, billing, scheduling, lease behavior, enrollment, fleet updates, model installation, memory controls, and customer-visible products are unchanged. No fleet machine is updated automatically as part of implementation.

## Verification requirements

### Image and Voice

- Authentication is required remotely for details and media; loopback behavior remains consistent with existing Studio policy.
- Detail projections include the approved user-facing fields for every supported operation and exclude paths, secrets, internal `_` fields, and unrelated request parameters.
- Origin classification covers Hub, official loopback UI, authenticated API/automation, and unknown/legacy without trusting caller-supplied device names.
- Handles are opaque, job-bound, media-bound, expire after five minutes, reject tampering, and cannot traverse allowed roots.
- Reference media and outputs stream only when still attached to the exact job; deleted/pruned files return the approved error.
- Response and error logs contain no prompt, transcript, path, or credential;
  application messages do not repeat opaque media handles.

### Studio Hub

- Broker evidence overrides conflicting worker origin claims for Hub-dispatched jobs.
- Optional origin fields remain compatible with reporters that omit them.
- Details are never fetched by the ordinary Stats request or background poll.
- The gateway substitutes credentials correctly across controller, Agent Hub, and Studio hops and streams disconnects without leaking connections.
- The drawer renders every content/error state, preserves focus and selected Stats window, aborts on close, and leaves no browser-storage state.
- Source labels and verified optional device names appear in both activity rows and details.

### Release verification

- Run focused contract/security/UI tests first, then the complete Hub, Image, and Voice suites.
- Run release-metadata, compilation, diff, and existing dependency checks in all three repositories.
- Inspect the drawer at desktop and narrow widths with image, audio, loading, offline, removed-media, and unsupported-version fixtures.
- Obtain an independent final cross-repository security/privacy review before pushing amendments to the existing pull requests.

## Explicit anti-goals

- No central content archive or 30-day prompt/transcript/media retention.
- No eager detail prefetch or sensitive fields in activity polling.
- No arbitrary remote file browser or path-based download endpoint.
- No new user-entered token, per-machine sign-in, or direct browser-to-worker requirement.
- No spoofable free-form device identity.
- No new service, port, database, dependency, or telemetry platform.
- No independent expansion of the fleet activity board to subtitle/transcription jobs in this phase.
