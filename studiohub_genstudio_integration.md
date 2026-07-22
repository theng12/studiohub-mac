# GenStudio KH handoff: consume Studio Hub site capabilities

Use this file as the implementation brief for a GenStudio KH coding session.
The Studio Hub capability contract is available in Studio Hub KH `v1.56.0`
and later. Version `v1.57.0` adds effective site-local RAM-admission facts;
version `v1.61.0` adds the site-local Whisper Tiny baseline contract.

The canonical response contract is documented in
[`CAPABILITY_CONTRACT.md`](CAPABILITY_CONTRACT.md). If this handoff and that
contract ever disagree, the contract is authoritative.

## Objective

Add a private GenStudio client that reads a location controller's current
capability snapshot and uses it as one routing input. Do not expose this
snapshot directly to customers.

The permanent ownership boundary is:

```text
Customer/API -> GenStudio KH -> Studio Hub location controller -> Studio workers
```

GenStudio remains the sole global authority for customer jobs, billing,
idempotency, attempts, fencing-token issuance, retries, reconciliation,
cross-location routing, customer-visible status, and customer assets. Studio
Hub reports site-local facts and may accept or reject an explicitly assigned
attempt; it never selects, claims, or reclaims a global job.

## Studio Hub request

```http
GET {studio_hub_base_url}/api/hub/capabilities
Authorization: Bearer {studio_hub_or_fleet_token}
Accept: application/json
```

`X-Hub-Token` is also accepted, but GenStudio should standardize on the Bearer
header. The token is required even when Hub and GenStudio run on the same Mac.
Do not use a browser session, password, cookie, URL parameter, or query-string
token.

Expected identity:

```json
{
  "schema": "studiohub.site-capabilities",
  "schema_version": 1
}
```

GenStudio must reject an unknown schema name or unsupported major schema
version and should ignore unknown additive fields within version 1.

## Multi-controller maintenance composition

GenStudio may provide one global operator button, but each selected Studio Hub
continues to own and persist only its site-local maintenance jobs. Use each
site's stored authenticated transport and compose these existing contracts:

1. Confirm the site has no queued/running Hub work from `GET /api/hub/jobs`,
   `GET /api/hub/chat/jobs`, and `GET /api/hub/transcription/jobs`.
2. Audit or repair startup services with `GET /api/hub/startup-services` and
   `POST /api/hub/startup-services/{machine}/{modality}/install`.
3. Rescan and start drained Studio updates with
   `POST /api/hub/maintenance/studio-versions` and
   `POST /api/hub/maintenance/updates`; poll the returned site-owned job.
4. Update agent Hubs with `POST /api/hub/maintenance/hub-updates` and poll the
   returned site-owned job.
5. Reconcile the lightweight transcription baseline with
   `POST /api/hub/model-baselines/reconcile`.
6. Update the location controller itself last through
   `POST /api/hub/maintenance/self-update`, then verify its version and
   capability identity after it returns.

Run locations with bounded concurrency and retain per-site results. A dropped
connection is not proof of failure: reconnect to the site-owned update job or
rescan versions before retrying. Never start a second update merely because a
poll response was lost. These are operator maintenance operations, not customer
jobs; they must not mint GenStudio job IDs, attempts, idempotency keys, leases,
or fencing tokens.

The Whisper baseline endpoints contain no customer material:

```http
GET  /api/hub/model-baselines
POST /api/hub/model-baselines              {"enabled": true}
POST /api/hub/model-baselines/reconcile
```

They apply only to registered Voice Studio transcription workers. Offline
targets remain retryable and baseline failures never replace or block the
site-local SQLite scheduler.

## Configuration and secret handling

Store these values per location:

- Studio Hub base URL.
- Expected `site_id`.
- Expected `controller_id`, if the deployment pins one controller identity.
- Hub or fleet token in GenStudio's existing secret store.
- Whether immutable model revision pinning is required for that location.

Never write the token to application logs, database telemetry, exception text,
URLs, browser storage, or customer-visible responses. A sanitized connection
status may record HTTP status, latency, observation time, site/controller IDs,
and schema version.

## Polling and freshness

Implement this as inbound polling from GenStudio. Do not add a persistent
outbound connector to Studio Hub yet.

Recommended initial behavior:

- Poll each configured site every 15 seconds with small random jitter.
- Allow only one in-flight capability request per site.
- Use a bounded request timeout of about 30 seconds because Hub may refresh
  read-only Studio catalogs while composing the response.
- Preserve the last valid snapshot for diagnostics, but do not route new work
  from a stale snapshot.
- Treat a snapshot as stale when `observed_at` is older than 60 seconds, its
  identity does not match the configured site, authentication fails, the
  response is malformed, or the controller is unreachable.
- Use exponential backoff after transport failures while continuing to probe.
  A failed capability poll must not change customer billing or an already-owned
  attempt by itself.

## Routing interpretation

A site is eligible for a new assignment only when all of these are true:

1. The snapshot is fresh and identity-validated.
2. `controller.online` and `controller.ready` are true.
3. `controller.drained` is false.
4. `authority.global == "genstudio"`.
5. `authority.site_local_scheduler == "sqlite"`.
6. `authority.global_job_claiming` is false.
7. At least one worker/model pair for the requested operation reports
   `availability.available_now == true`.
8. The model's controls and input/output limits accept the request.
9. If GenStudio requires revision pinning,
   `availability.revision_pinning_ready == true` and `runtime_revision` matches
   the selected immutable revision.

Use `internal_model_id` when addressing the selected Studio runtime. Stable
operation names currently include:

- `image.generation`
- `voice.tts`
- `audio.transcription`
- `chat.completion`
- `music.generation`
- `video.generation`
- `video.render`

For voice work, also match a supported `controls.voice_modes` value. Do not
infer voice-cloning support from the model name.

Capacity is shared by physical Mac. `eligible_worker_services` counts routing
choices, not independent concurrent slots. Use
`capacity.available_physical_machine_slots` and each machine's
`available_capacity.worker_slots` when estimating site concurrency. Do not add
the Image, Voice, Chat, and other service slots from the same physical machine
as if they could all perform heavy work simultaneously.

`availability.available_now` is an observation, not a reservation. GenStudio
must still handle a safe assignment rejection because capacity can change
between observation and dispatch.

For local models, `memory_admission` explains the catalog requirement, Hub
default, effective total/free-memory floors, policy source, and observed machine
memory. GenStudio should treat `availability.available_now` as the routing
decision and retain `memory_admission` as sanitized diagnostics. The location
operator may adjust these floors from Studio Hub; GenStudio must not overwrite
them or turn them into global ownership state.

## Availability and revision rules

- A non-null `runtime_revision` is a worker-reported immutable full hash.
- `runtime_revision: null` is valid and means the Studio did not report a
  qualified immutable revision.
- Never replace a null revision with a branch, tag, model name, timestamp, or a
  GenStudio-generated fingerprint.
- `availability.reason` is diagnostic and should not be translated into a
  customer promise without GenStudio policy.
- Cloud models are available only when Hub has verified their provider state.
- Maintenance, drains, quarantines, worker busy state, and shared-machine busy
  state are already reflected in `available_now`.

## Failure behavior

- `401`: configuration/credential error; mark the site unavailable and alert an
  operator without logging the token.
- Unsupported schema or identity mismatch: quarantine the snapshot and mark the
  site unavailable for new routing.
- Timeout, connection failure, or stale observation: mark the site unavailable
  for new routing and let GenStudio's global router consider another location.
- Zero capacity or no available compatible model: do not submit; select another
  eligible site or keep the GenStudio job pending according to GenStudio policy.
- An assignment rejected after a successful snapshot remains a GenStudio-owned
  routing decision. Studio Hub must not be asked to claim another global job.

Do not cancel or reassign an accepted attempt solely because a later capability
poll fails. Attempt leases, fencing, reconciliation, and cross-location retry
remain GenStudio responsibilities.

## Privacy constraints

The capability endpoint intentionally contains no customer prompts, input text,
generated content, artifacts, credentials, GenStudio job IDs, attempt IDs,
idempotency keys, or fencing tokens. GenStudio must not add customer content to
its capability-poll telemetry or logs.

## Suggested GenStudio interfaces

Keep transport and routing policy separate. Equivalent names are acceptable:

```text
StudioHubCapabilityClient.fetch(site) -> CapabilitySnapshot
CapabilitySnapshot.validate_identity(site_config)
CapabilitySnapshot.is_fresh(now)
CapabilityRouter.eligible_sites(operation, model, controls, limits)
CapabilityRouter.select_site(...)      # GenStudio-owned policy
```

Persisting a last-known sanitized snapshot for operations is acceptable, but it
must remain routing telemetry rather than customer-job authority.

## Required GenStudio tests

Add tests proving:

1. Bearer authentication is sent and never logged.
2. Schema name/version and site/controller identity are validated.
3. Unknown additive v1 fields are ignored.
4. Stale, malformed, unauthorized, and unreachable sites are ineligible.
5. Drained or unready controllers are ineligible.
6. Busy, drained, maintained, quarantined, offline, or incompatible workers are
   not selected.
7. Operation, internal model, controls, limits, voice mode, and immutable
   revision requirements are matched correctly.
8. Physical-machine capacity is not double-counted across sibling Studios.
9. Null immutable revisions are preserved and can be rejected by policy without
   inventing a replacement.
10. A capability failure cannot charge/refund, claim/retry a customer job,
    issue a fencing token, or change an accepted attempt.
11. PostgreSQL and Studio Hub never become global routing authorities.

Use a mocked Studio Hub response for contract tests. A guarded local smoke test
may call a running Hub with a configured secret, but it must perform only the
GET above and must not submit, drain, restart, or alter workers or jobs.

## Definition of done

- GenStudio can configure and authenticate one or more Studio Hub sites.
- It periodically obtains and validates schema v1 snapshots.
- Its router can filter sites by freshness, controller state, physical
  capacity, operation, model, revision, voice mode, limits, and controls.
- Site unavailability reduces routing capacity without changing global job
  authority or corrupting accepted attempts.
- Tokens and customer content do not enter capability telemetry.
- GenStudio's existing billing, idempotency, attempt, fencing, retry, and asset
  behavior remains authoritative and unchanged except where it consumes these
  read-only routing facts.
