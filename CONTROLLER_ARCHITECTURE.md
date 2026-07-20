# Studio Hub controller architecture

Studio Hub uses one codebase on every Mac. Runtime configuration selects whether
an installation accepts customer jobs as a site controller or serves only as the
local authority for its Studios.

## Roles

- `standalone` is the backward-compatible default. Local SQLite remains the job
  authority and all existing behavior continues.
- `controller` accepts customer jobs, coordinates its registered workers, and
  can publish control-plane state to PostgreSQL.
- `agent` manages and proxies its local Studios but refuses new customer job,
  Chat, transcription, recipe, and director submissions. Existing work is not
  cancelled when the role changes.

A site-controller Mac can also run Studios locally. It does not need a different
build or a second Hub process.

## Migration stages

### Stage 0 — local

`database_mode=off`. This is today's proven SQLite-backed scheduler. It is safe
for one controller, but separate Hubs do not share job ownership.

### Stage 1 — PostgreSQL shadow

`role=controller` and `database_mode=shadow`. PostgreSQL receives:

- Controller and site heartbeats.
- Registered machine and Studio status.
- Capacity snapshots.
- Current generation, Chat, and transcription job/item state.
- The schema required for future leases, fencing tokens, attempts, global
  operation leases, and audit events.

SQLite remains authoritative. PostgreSQL errors never fail a local job save, and
the shadow queue is memory-bounded during an outage. Global job claiming is
hard-disabled and reported as such by the API and dashboard.

### Stage 2 — lease and fencing qualification (next)

Before PostgreSQL becomes authoritative, the following must pass integration and
failure testing:

1. Atomic item claims with expiring leases.
2. Lease renewal while a controller owns dispatch.
3. Monotonic fencing tokens on commands.
4. Agent-side rejection of stale fencing tokens.
5. Worker reconciliation before a replacement controller retries work.
6. Idempotent completion and artifact registration.
7. Controller crash, PostgreSQL outage, and network-partition drills.

Only then will an explicit authoritative mode be added. There is intentionally no
configuration switch that can prematurely enable global claiming in Stage 1.

## Configuration

Use **Remote → Controller role & site**, or environment variables:

```text
STUDIOHUB_ROLE=controller
STUDIOHUB_SITE_ID=phnom-penh-1
STUDIOHUB_SITE_NAME=Phnom Penh · Site 1
STUDIOHUB_CONTROLLER_ID=controller-pp-a
STUDIOHUB_DATABASE_MODE=shadow
STUDIOHUB_DATABASE_URL=postgresql://...
```

The database URL is stored separately in an owner-only `0600` file when entered
through the dashboard. It is never returned by the API, written to the regular
settings JSON, or shown in logs. Environment configuration takes precedence.

Agent example:

```text
STUDIOHUB_ROLE=agent
STUDIOHUB_SITE_ID=phnom-penh-1
STUDIOHUB_CONTROLLER_ID=macmini-m1-001-hub
```

Agents do not need PostgreSQL credentials.

## Health and control API

- `GET /health/live` — process liveness.
- `GET /health/ready` — controller/database readiness; returns HTTP 503 when a
  configured shadow database is unavailable.
- `GET /health/capacity` — non-secret site capacity for GenStudio routing.
- `GET /api/hub/controller` — authenticated configuration and runtime status.
- `PUT /api/hub/controller` — save role/site/database-stage configuration.
- `POST /api/hub/controller/check` — initialize/verify the schema and publish an
  immediate heartbeat.

The existing `/api/health` and `/api/hub/summary` include control-plane state for
backward-compatible monitoring and the live dashboard.

### Configure through the API

JavaScript:

```javascript
await fetch(`${HUB}/api/hub/controller`, {
  method: "PUT",
  headers: {"Content-Type": "application/json", "X-Hub-Token": HUB_TOKEN},
  body: JSON.stringify({
    role: "controller",
    site_id: "phnom-penh-1",
    site_name: "Phnom Penh · Site 1",
    controller_id: "controller-pp-a",
    database_mode: "shadow",
    database_url: POSTGRESQL_URL
  })
});
```

Python:

```python
import requests

response = requests.put(
    f"{hub}/api/hub/controller",
    headers={"X-Hub-Token": hub_token},
    json={
        "role": "controller",
        "site_id": "phnom-penh-1",
        "site_name": "Phnom Penh · Site 1",
        "controller_id": "controller-pp-a",
        "database_mode": "shadow",
        "database_url": postgresql_url,
    },
    timeout=15,
)
response.raise_for_status()
```

curl:

```bash
curl -X PUT "$HUB/api/hub/controller" \
  -H "X-Hub-Token: $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "role":"controller",
    "site_id":"phnom-penh-1",
    "site_name":"Phnom Penh · Site 1",
    "controller_id":"controller-pp-a",
    "database_mode":"off"
  }'
```

## First-site operating procedure

1. Keep the current Hub in `standalone` until an off-site managed PostgreSQL URL
   is available.
2. Change this Hub to `controller`, choose a permanent site and controller ID,
   and leave the database stage at Local SQLite.
3. Enter the PostgreSQL URL, select Shadow migration, and click **Save &
   initialize**.
4. Require a green schema/heartbeat result. Customer jobs still use SQLite.
5. Configure new node Macs as `agent` under the same site ID when they arrive.
6. Do not attempt automatic cross-controller takeover until Stage 2 is shipped
   and qualified.
