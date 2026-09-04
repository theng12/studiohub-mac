# GenStudio site capability contract

Endpoint: `GET /api/hub/capabilities`

Schema: `studiohub.site-capabilities`

Schema version: `3`

This is a private, read-only machine-to-machine contract from a Studio Hub
location controller to GenStudio KH. It is routing input only. It does not
transfer customer-job authority to Studio Hub.

For a ready-to-use GenStudio implementation brief, see
[`studiohub_genstudio_integration.md`](studiohub_genstudio_integration.md).

## Authentication

Every request requires one of these headers, including requests from loopback:

```http
Authorization: Bearer <hub-or-fleet-token>
```

or:

```http
X-Hub-Token: <hub-or-fleet-token>
```

Browser sessions, cookies, query-string tokens, and owner passwords do not
authenticate this endpoint.

## Versioning

Clients must check both `schema` and `schema_version` and ignore unknown fields.
Additive optional fields may be introduced within schema version 3. Removing a
field, changing a field's type, or changing its meaning requires a new schema
version. The Studio Hub application version is reported separately as
`controller.studiohub_version`.

## Agent maintenance capability

`GET /api/version` advertises the additive integer field
`studio_update_repair_schema`. Exact value `1` means the Agent accepts the
authenticated, fixed `studio-update-repairs` maintenance operation for its own
local Voice/Image siblings. Controllers must require `type(value) is int` and
value `1`; booleans, strings, missing fields, and unknown versions are not
capability evidence.

The operation never transfers filesystem paths or `ENVIRONMENT` contents to the
controller. An old, offline, or unsupported Agent remains pending/retryable and
must receive the Hub update (or local SSD Stage 5 fallback) before remote repair.
This maintenance capability is independent of GenStudio model routing and does
not change the site-capabilities schema version.

## Response shape

```json
{
  "schema": "studiohub.site-capabilities",
  "schema_version": 3,
  "observed_at": "2026-07-20T15:30:00Z",
  "site_id": "phnom-penh-1",
  "controller": {
    "controller_id": "controller-pp-a",
    "role": "controller",
    "studiohub_version": "1.56.0",
    "online": true,
    "ready": true,
    "drained": false,
    "managed_release": null
  },
  "authority": {
    "global": "genstudio",
    "site_local_scheduler": "sqlite",
    "global_job_claiming": false,
    "postgresql": "optional_shadow_evidence_only"
  },
  "capacity": {
    "queue_depth": 0,
    "available_physical_machine_slots": 1,
    "eligible_physical_machine_slots_total": 1,
    "eligible_worker_service_slots_total": 3,
    "eligible_worker_services": 3,
    "shared_physical_machine_slots": true,
    "by_operation": {
      "image.text_to_image": {
        "workers_total": 1,
        "workers_online": 1,
        "workers_ready": 1,
        "available_worker_slots": 1,
        "eligible_physical_machine_slots_total": 1,
        "eligible_worker_service_slots_total": 1,
        "available_physical_machine_slots": 1
      }
    }
  },
  "machines": [],
  "workers": [],
  "model_supply": [],
  "managed_release": null
}
```

Each machine reports its physical machine ID, registered hardware profile,
online/enabled/drained/maintenance state, available unified memory, and one
shared heavy-work slot. Worker-service counts are eligibility choices, not
additive concurrency: Image, Voice, and Chat on one Mac still share that Mac's
single physical-machine slot.

Each worker reports:

- Studio type and running Studio version.
- Stable service ID and physical machine ID.
- Registered hardware profile.
- Online, ready, busy, drained, maintenance, and machine-quarantine state.
- Current available slot count.
- Supported operations and per-worker model capabilities.

The additive capacity totals separate compatible capacity from current free
capacity. `capacity.eligible_physical_machine_slots_total` is the number of
unique physical machine IDs with at least one exact, approved model
observation whose `availability.capacity_eligible` is true. It includes a
machine while that worker or another service on the machine is busy.
`capacity.eligible_worker_service_slots_total` counts the corresponding unique
worker services. The same two totals appear under each operation in
`capacity.by_operation`; `available_physical_machine_slots` and
`available_worker_slots` remain current observations. The legacy
`eligible_worker_services` field also remains a current available-service
count for older consumers.

Operations use stable names such as:

- `image.text_to_image`
- `image.image_to_image`
- `voice.tts`
- `audio.transcription`
- `chat.completion`
- `music.generation`
- `video.generation`
- `video.render`

Each model capability reports the worker's internal model ID, local execution
lane, input/output limits, allowlisted controls, and current availability.
Voice models may report `preset_voice`, `reference_audio_clone`, or
`voice_design` modes when the worker catalog provides enough evidence.

For `reference_audio_clone`, the audit-bound `input_limits` may additionally
report `text_max_characters`, `long_form_strategy`,
`private_section_max_characters`, and a `reference_audio` object containing the
model's minimum/recommended/maximum usable duration, sample rate, channel
count, transcript requirement, and accepted formats. These are worker-owned
execution facts. Studio Hub sanitizes and relays them; it does not invent or
normalize model limits.

Locally brokered Image, Voice, Transcription, Music, and Video models also report
`memory_admission`: catalog, Hub-default, and effective minimum total/free RAM,
policy source, current observed memory, and whether the machine is eligible
now. Operator overrides are site-local scheduling policy; they do not modify
the worker catalog or transfer global authority from GenStudio.
`availability.available_now` reflects the effective policy.
`availability.capacity_eligible` is the separate static capacity fact: it
requires an online, ready, enabled, non-maintenance, non-quarantined worker
with a fresh, error-free catalog, an installed model, compatible
runtime/subsystem, matching execution gates, and a passing total-memory floor
when applicable. A reachable remote machine without host-memory evidence is
not counted when its model has a RAM floor; its current worker-reported slot
and existing availability reason remain separate observations.
It deliberately does not require a free slot, and therefore remains true for a
compatible busy worker. A low current free-memory reading can make
`available_now` false without removing the machine from total compatible
capacity.

## Audited candidate and exposure gate

Schema version 3 advertises only models that pass both independent gates:

1. The sibling Studio publishes a valid `studio.model-audit` version 1
   `genstudio_candidate` for an exact internal model ID, immutable runtime
   revision, contract hash, and operation. The sibling's audit must be passed
   and `candidate_for_genstudio` must be true.
2. The owner approves that exact candidate once in GenStudio's global Approved
   Fleet Model Catalog. Approval is pinned to model ID + operation + revision +
   contract hash. GenStudio pushes the versioned desired state to every
   controller, which persists its last-good copy and enforces it locally.

A sibling never publishes `approved_for_genstudio`; final exposure authority
belongs to GenStudio. Studio Hub validates and consumes that authority; it does
not create an independent site approval. Removing sibling candidacy or global
approval stops new capability publication and automatic caching without
deleting cached files, partial downloads, or historical evidence. An outage
retains the last-good inventory but makes stale supply unavailable.

The sibling audit candidate may report `adapter`, `controls`, `input_limits`,
`output_limits`, `capacity`, and `hardware`. Studio Hub bounds and sanitizes
these fields before publication. Each published model includes its audit and
exact exposure evidence, and `availability.approved_for_genstudio` is true.

`model_supply` groups the detailed worker evidence by exact model ID,
operation, immutable revision, and contract hash. It reports installed,
online, ready, busy, offline, and quarantined machine counts; current
available physical slots; total eligible physical slots in
`eligible_physical_slots_total`; machine IDs; hardware and memory evidence;
per-machine availability reasons; last catalogue refresh; and stale state.
Each machine observation includes additive boolean `capacity_eligible`, so a
consumer can deduplicate a physical machine across operation aliases without
parsing diagnostic reason strings. The aggregate is derived from
`workers[].models[]` and is never a second authority.

## Catalogue freshness

Studio Hub refreshes every registered sibling catalogue in a dedicated
background loop, independently of the quick health loop. Refreshes are
non-overlapping, concurrent across workers, and individually bounded. The
last-good inventory is stored with owner-only file permissions and survives a
Hub restart. Worker failure marks its observation stale/unavailable without
erasing its last-known inventory.

`GET /api/hub/capabilities` is strictly cache-only: it never performs a worker
request. Each model includes `catalog_observation` with observation time, age,
and stale state. A stale catalogue forces `availability.available_now=false`
with reason `catalog_stale`; a recorded refresh error fails closed with reason
`catalog_error`. Last-good rows remain available for diagnostics only.

## Runtime revisions

`runtime_revision` is populated only from a worker-reported immutable full hash
(40–64 hexadecimal characters, optionally prefixed by `sha256:`). Studio Hub
does not turn a branch, tag, model name, current time, or catalog fingerprint
into a runtime revision.

When no immutable revision is available:

```json
{
  "runtime_revision": null,
  "revision_source": null,
  "revision_status": "not_reported",
  "availability": {
    "revision_pinning_ready": false
  }
}
```

Execution availability and revision-pinning qualification are separate facts.
GenStudio decides whether its routing policy requires an immutable revision.

## Availability semantics

`availability.available_now=true` requires all relevant local facts to pass:

- The worker is online and ready.
- The worker and machine are not drained, in maintenance, quarantined, or busy.
- The local model is installed.
- The runtime and subsystem report compatibility/readiness.
- When a sibling publishes them, `qualified_revision_match` and
  `execution_ready` are not false. These additive booleans are also relayed in
  `availability` for diagnostics. A revision mismatch reports
  `runtime_revision_mismatch`; other explicit worker execution unavailability
  reports `worker_execution_unready`.
- The audited catalogue observation is not stale and has no recorded refresh
  error.
- The exact model contract remains present in the last-good GenStudio fleet
  catalog accepted by this controller.
- When the worker catalog reports candidate `capacity.available_slots`, it
  must report at least one current slot. Worker health busy evidence is also
  treated as unavailable now, including the nested generation busy signal
  used by Image Studio.

`availability.capacity_eligible=true` is the total-capacity counterpart. It
requires the worker and model compatibility, freshness, installation,
execution, and total-memory gates above, but intentionally ignores worker
busy, shared-machine busy, and current free-slot/free-memory observations.
Consequently a busy-but-compatible machine contributes to
`eligible_physical_slots_total` while contributing zero to
`available_physical_slots`. Offline, drained, maintenance, quarantined,
stale, uninstalled, incompatible, revision-mismatched, execution-unready,
and total-memory-ineligible observations are not capacity eligible.

An unavailable model includes a stable reason such as `worker_offline`,
`physical_machine_busy`, `worker_maintenance`, or `model_not_installed`.
Older sibling versions that omit `qualified_revision_match` and
`execution_ready` publish both as `null`; omission alone never makes a
previously compatible model unavailable.

## Managed release evidence and admission

Schema version 3 adds sanitized `managed_release` evidence at the response,
controller, machine, and worker levels. With no desired release it is `null`
and the existing availability reasons keep their previous precedence. With an
active intent it reports the desired release ID, expected and observed SemVer
and 40-hex commit, component/site state, convergence, next retry, canary, and
catalog-request timestamps. It never includes credentials, checkout paths,
commands, or customer data.

Site states are `pending`, `queued`, `running`, `waiting_busy`, `degraded`,
`blocked_release`, and `complete`. Component states are `not_installed`,
`pending_offline`, `pending_busy`, `checking`, `updating`, `restarting`,
`verifying`, `current`, `retryable_failure`, `auth_blocked`, and
`release_blocked`. A component that was already queued when its authoritative
whole-machine switch turned off is durably `excluded_disabled`; the machine is
not contacted, does not hold the site open, and becomes pending again when the
machine is re-enabled. `complete` and `blocked_release` are terminal;
`degraded` remains retryable and survives controller restart.

Managed-release lag remains visible but does not by itself make an otherwise
healthy model unavailable. Pending, offline, busy, retryable, authentication,
and version-mismatch states describe rollout progress; GenStudio's exact
per-model revision, contract, approval, freshness, and execution gates remain
the routing authority while those machines converge. Only a `blocked*` release
or component state, including the legacy `release_blocked` spelling, forces
`available_now=false` with `managed_release_blocked` because it identifies an
immutable manifest contradiction rather than ordinary lag.

Release evidence is additional observability alongside the existing audit,
approval, revision, cache, memory, busy, maintenance, and health gates. A
catalog acknowledgement records only that approved-model reconciliation was
requested; it is not proof that model downloads completed.

## Privacy and authority boundary

The response is built from allowlisted health, inventory, registry, hardware,
and scheduler facts. It never includes:

- Customer prompts, text, or generated content.
- Artifact or cache paths.
- API keys, passwords, or tokens.
- GenStudio customer job IDs, attempt IDs, idempotency keys, or fencing tokens.

GenStudio remains responsible for global jobs, routing, billing, retries,
leases, fencing, customer status, and assets. Studio Hub may report local
capacity or reject an assigned attempt, but it does not select, claim, reclaim,
or transfer global work. SQLite remains authoritative for site-local dispatch;
PostgreSQL remains optional non-authoritative evidence only.

## Private voice-reference execution

Customer voice uploads remain GenStudio assets. They are not added to Studio
Hub's operator-owned Shared Voices library and are never broadcast across the
fleet. For one assigned site attempt, GenStudio stages the exact bytes through
`POST /api/hub/execution-assets/voice-references` using authenticated multipart
fields `audio`, `source_asset_id`, `source_sha256`, and optional `transcript`,
`transcript_segments_json`, `language`, and `ttl_seconds`.

The response contains a short-lived `asset.id`, checksum, byte count, media
type, and expiry. It excludes the source asset ID, transcript, storage path,
and customer metadata. GenStudio then submits the ordinary `/api/hub/jobs`
voice envelope with `sharedParams.voice_reference_asset_id` and a
`voice_modes`-compatible audited model. The Hub forwards the bytes only to the
selected Voice Studio using its authenticated multipart reference endpoint.

Voice Studio owns decoding, silence-aware selection, resampling, loudness
normalization, transcript slicing, private text sections, stitching, and one
final speed adjustment. Studio Hub treats the request as one execution attempt,
pins it to one physical worker, relays `chunk_index` / `chunk_total` progress,
and forwards cancellation to that worker. It never schedules private chunks as
independent jobs.

Stable staging failures include `VOICE_REFERENCE_ASSET_MISSING`,
`VOICE_REFERENCE_ASSET_EXPIRED`, and `VOICE_REFERENCE_ASSET_INACCESSIBLE`.
Preparation errors reported by Voice Studio remain machine-readable and are
not retried as infrastructure failures. Successful terminal evidence may
include `reference_source_sha256`, `reference_audio_sha256`,
`reference_preparation_revision`, `reference_duration_s`,
`long_form_strategy`, and `chunk_total`.
