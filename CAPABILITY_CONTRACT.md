# GenStudio site capability contract

Endpoint: `GET /api/hub/capabilities`

Schema: `studiohub.site-capabilities`

Schema version: `1`

This is a private, read-only machine-to-machine contract from a Studio Hub
location controller to GenStudio KH. It is routing input only. It does not
transfer customer-job authority to Studio Hub.

For a ready-to-use GenStudio implementation brief, see
[`GENSTUDIO_CAPABILITY_HANDOFF.md`](GENSTUDIO_CAPABILITY_HANDOFF.md).

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
Additive optional fields may be introduced within schema version 1. Removing a
field, changing a field's type, or changing its meaning requires a new schema
version. The Studio Hub application version is reported separately as
`controller.studiohub_version`.

## Response shape

```json
{
  "schema": "studiohub.site-capabilities",
  "schema_version": 1,
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
  "workers": []
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

- `image.generation`
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
