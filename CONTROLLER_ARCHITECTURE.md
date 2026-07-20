# Studio Hub site-controller architecture

The permanent application boundary follows GenStudio ADR-0007:

```text
Customer / first-party client
  -> GenStudio KH
  -> Studio Hub location controller
  -> site-local Studio workers
```

GenStudio is the only global customer-facing and business authority. Studio
Hub is a private site execution service. The same Studio Hub build can run as a
site controller or as the local agent on a worker Mac, but no Hub instance may
claim or transfer a global customer job.

## Ownership boundary

GenStudio owns:

- Customer accounts, API keys, pricing, promotions, credits, charges, refunds,
  customer-visible status, and retention.
- Customer job IDs, global idempotency, execution attempts, cross-location
  routing, retries, reconciliation, leases, and fencing-token issuance.
- Final customer assets and signed/downloadable customer grants.

Studio Hub owns only its site-local execution:

- Machine and Studio health, capabilities, model availability, and capacity.
- Memory admission, worker eligibility and selection, local queues, local safe
  retries, and physical-machine concurrency protection.
- Worker progress, artifact retrieval, immutable revision/checksum evidence,
  and returning verified execution results to GenStudio.
- Continuing an already accepted local execution through a short GenStudio
  connection interruption. GenStudio later reconciles the same Hub batch.

Local SQLite is permanently authoritative for Studio Hub scheduling. There is
no PostgreSQL-authoritative or cross-controller-claiming stage in Studio Hub.

## Roles

- `standalone` preserves the established direct/local behavior.
- `controller` accepts work explicitly routed to this site and coordinates its
  registered local/remote workers.
- `agent` manages and proxies only its own local Studios. It refuses new
  customer-style queue, Chat, transcription, recipe, and director submissions.

Agent Hubs do not use or retain PostgreSQL credentials. A site controller may
also run Studios locally; it does not require another build or Hub process.

## Optional PostgreSQL shadow

`database_mode=shadow` is available only to a controller and is completely
optional. It publishes non-authoritative operational records:

- Controller/site heartbeats.
- Machine and Studio inventory.
- Capacity and operational telemetry.
- Site-local execution progress and result evidence.
- Audit/diagnostic information.

SQLite writes happen first. A PostgreSQL outage cannot reject, replace, claim,
retry, refund, transfer, or otherwise alter a local job. `global_job_claiming`
is permanently `false`.

Migration `001_controller_foundation.sql` shipped ownership-shaped columns and
tables before the GenStudio boundary was finalized. They remain solely for
backward schema compatibility. Migration `002_execution_evidence_boundary.sql`
marks those lease, claim, attempt, and fencing fields as legacy/reserved and
adds explicit GenStudio execution-evidence columns. Studio Hub contains no code
that acquires those leases or increments their fencing fields.

## GenStudio-assigned execution identity

GenStudio may submit a site execution with these top-level fields (or the same
fields inside `genstudio_execution`):

```json
{
  "genstudio_job_id": "job_01...",
  "genstudio_attempt_id": "attempt_01...",
  "idempotency_key": "non-secret-stable-attempt-key",
  "fencing_token": 42,
  "site_id": "phnom-penh-1",
  "operation": "tts",
  "model_revision": "optional-expected-revision",
  "voice_revision": "optional-expected-revision"
}
```

The IDs and token are issued by GenStudio. Studio Hub never invents or advances
a global token. The controller stores only a SHA-256 hash of the idempotency
key, preserves the supplied evidence with its local batch, and applies two
local admission rules before worker dispatch:

1. After observing token `N` for a GenStudio job, a request with a token below
   `N` is rejected.
2. Replaying the same job, attempt, idempotency identity, and payload is safe;
   the same idempotency identity with a different payload is rejected.

These checks prevent a stale GenStudio assignment from starting new local work.
They do not make Studio Hub the owner of the global attempt. GenStudio remains
responsible for deciding whether and where another attempt is created.

Existing direct Story Studio/Studio Hub requests and existing GenStudio adapter
requests without the new metadata retain their current behavior.

## Health and control API

- `GET /health/live` — process liveness.
- `GET /health/ready` — site-execution readiness. An optional telemetry outage
  is reported as a warning and never removes a healthy local scheduler.
- `GET /health/capacity` — non-secret site capacity for GenStudio routing.
- `GET /api/hub/capabilities` — private, schema-versioned routing capability
  snapshot. It requires a Hub/fleet token header even on loopback and reports
  only allowlisted operational facts; no customer content or ownership IDs.
- `GET /api/hub/controller` — authenticated role/site/shadow status.
- `PUT /api/hub/controller` — save role, site, and optional shadow settings.
- `POST /api/hub/controller/check` — verify the optional shadow schema and send
  an immediate heartbeat/evidence flush.

The existing `/api/health` and `/api/hub/summary` include the same boundary
status for backward-compatible monitoring.

The capability snapshot reports a model's immutable runtime revision only when
the worker catalog supplies a full immutable hash. A missing or mutable
revision is returned as `null` and marked unqualified for revision pinning.
Studio Hub does not synthesize revisions, GenStudio IDs, idempotency keys, or
fencing tokens.

## Configuration

Use **Remote -> Controller role & site**, or controller environment variables:

```text
STUDIOHUB_ROLE=controller
STUDIOHUB_SITE_ID=phnom-penh-1
STUDIOHUB_SITE_NAME=Phnom Penh · Site 1
STUDIOHUB_CONTROLLER_ID=controller-pp-a
STUDIOHUB_DATABASE_MODE=off
```

If an optional observability database is introduced later, a controller may use
`STUDIOHUB_DATABASE_MODE=shadow` and `STUDIOHUB_DATABASE_URL=postgresql://...`.
The URL is stored separately in an owner-only file when entered in the UI and
is never returned by the API or written to logs. Agents must not receive it.

## Operating rules

1. Keep PostgreSQL off unless non-authoritative fleet observability is needed.
2. Configure every location with a stable site ID and one GenStudio-routable
   controller endpoint.
3. Configure worker Macs as agents under their location's site ID.
4. GenStudio selects a healthy site using the health/capacity contract and
   sends one explicitly owned attempt to that controller.
5. Studio Hub performs only local dispatch/retry and returns execution evidence.
6. GenStudio alone decides cross-location retry, failover, billing, refunds,
   final state, and asset retention.

Source of truth: [GenStudio ADR-0007](https://github.com/theng12/genstudiokh/blob/main/docs/decisions/ADR-0007-genstudio-api-boundary.md).
