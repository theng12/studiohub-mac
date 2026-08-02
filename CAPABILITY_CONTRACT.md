# GenStudio site capability contract

Endpoint: `GET /api/hub/capabilities`

Schema: `studiohub.site-capabilities`

Schema version: `2`

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
Additive optional fields may be introduced within schema version 2. Removing a
field, changing a field's type, or changing its meaning requires a new schema
version. The Studio Hub application version is reported separately as
`controller.studiohub_version`.

## Response shape

```json
{
  "schema": "studiohub.site-capabilities",
  "schema_version": 2,
  "observed_at": "2026-07-20T15:30:00Z",
  "site_id": "phnom-penh-1",
  "controller": {
    "controller_id": "controller-pp-a",
    "role": "controller",
    "studiohub_version": "1.56.0",
    "online": true,
    "ready": true,
    "drained": false
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
    "eligible_worker_services": 3,
    "shared_physical_machine_slots": true,
    "by_operation": {}
  },
  "machines": [],
  "workers": [],
  "model_supply": []
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

Operations use stable names such as:

- `image.text_to_image`
- `image.image_to_image`
- `voice.tts`
- `audio.transcription`
- `chat.completion`
- `music.generation`
- `video.generation`
- `video.render`

Each model capability reports the worker's internal model ID, execution lane,
provider when relevant, input/output limits, allowlisted controls, and current
availability. Voice models may report `preset_voice`,
`reference_audio_clone`, `voice_design`, or `provider_voice_id` modes when the
worker catalog provides enough evidence.

Locally brokered Image, Voice, Music, and Video generation models also report
`memory_admission`: catalog, Hub-default, and effective minimum total/free RAM,
policy source, current observed memory, and whether the machine is eligible
now. Operator overrides are site-local scheduling policy; they do not modify
the worker catalog or transfer global authority from GenStudio.
`availability.available_now` reflects the effective policy.

## Audited candidate and exposure gate

Schema version 2 advertises only models that pass both independent gates:

1. The sibling Studio publishes a valid `studio.model-audit` version 1
   `genstudio_candidate` for an exact internal model ID, immutable runtime
   revision, contract hash, and operation. The sibling's audit must be passed
   and `candidate_for_genstudio` must be true.
2. The location owner approves that exact candidate in Studio Hub's Models
   workspace. Approval is pinned to model ID + operation + revision + contract
   hash. A changed runtime or contract returns to review automatically.

A sibling never publishes `approved_for_genstudio`; final exposure authority
belongs to Studio Hub. Removing sibling approval or revoking Hub approval stops
new capability publication without deleting historical evidence. An outage
retains the last-good inventory but makes stale supply unavailable.

The sibling audit candidate may report `adapter`, `controls`, `input_limits`,
`output_limits`, `capacity`, and `hardware`. Studio Hub bounds and sanitizes
these fields before publication. Each published model includes its audit and
exact exposure evidence, and `availability.approved_for_genstudio` is true.

`model_supply` groups the detailed worker evidence by exact model ID,
operation, immutable revision, and contract hash. It reports installed,
online, ready, busy, offline, and quarantined machine counts; available
physical slots; machine IDs; hardware and memory evidence; per-machine
availability reasons; last catalogue refresh; and stale state. The aggregate
is derived from `workers[].models[]` and is never a second authority.

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
with reason `catalog_stale`.

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
- A local model is installed, or a cloud provider is currently verified ready.
- The runtime and subsystem report compatibility/readiness.
- The audited catalogue observation is not stale.
- The exact model contract remains approved by Studio Hub.

An unavailable model includes a stable reason such as `worker_offline`,
`physical_machine_busy`, `worker_maintenance`, `model_not_installed`, or
`provider_unavailable_or_unverified`.

## Privacy and authority boundary

The response is built from allowlisted health, inventory, registry, hardware,
and scheduler facts. It never includes:

- Customer prompts, text, or generated content.
- Artifact or cache paths.
- API keys, passwords, tokens, or provider credentials.
- GenStudio customer job IDs, attempt IDs, idempotency keys, or fencing tokens.

GenStudio remains responsible for global jobs, routing, billing, retries,
leases, fencing, customer status, and assets. Studio Hub may report local
capacity or reject an assigned attempt, but it does not select, claim, reclaim,
or transfer global work. SQLite remains authoritative for site-local dispatch;
PostgreSQL remains optional non-authoritative evidence only.
