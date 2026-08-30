# Live Fleet Activity and Performance Design

> Follow-up: owner-approved on-demand prompts, transcripts, origin details,
> reference media, and output previews are specified in
> `2026-08-31-on-demand-fleet-job-details-design.md`. Ordinary fleet polling
> remains privacy-safe; sensitive content is retrieved only after the owner
> opens one exact job.

## Job and audience

The Controller Studio Hub's Stats page serves the fleet owner during daily operations. Its first job is to answer, without interpretation: which Macs are working, which just finished, which are ready, which have been idle for a long time, and which need attention. Historical generation analytics remain a secondary layer for comparing output and performance.

## Outcomes and proof

The primary view must show every registered machine with a plain-language operational state and its duration. For active work it shows Studio, model, progress, job identity, and elapsed time. For inactive machines it shows the last observed completion or failure and how long ago it occurred. The view includes both Hub-dispatched jobs and jobs started directly inside Image Studio or Voice Studio.

Proof comes from observed Studio job state and Hub broker records, not process presence or CPU heuristics. Older Studio versions that lack the activity contract are labelled `Direct activity unavailable`; the Hub does not invent a state for them.

## Selected architecture

### Reporter contract

Image Studio and Voice Studio expose one authenticated, read-only `/api/fleet/activity` endpoint. It reuses the fleet credential and the existing health-poll cadence, so it adds no callback, daemon, port, or credential. Keeping detailed activity out of the public `/api/health` response preserves that endpoint's deliberate rule against exposing job identities.

The versioned payload is:

```json
{
  "schema": "kh-studio.activity.v1",
  "observed_at": 1788120012.0,
  "studio": "image",
  "active": {
    "id": "job-id",
    "state": "running",
    "model": "owner/repository",
    "progress": 0.42,
    "created_at": 1788120000.0,
    "started_at": 1788120002.0,
    "updated_at": 1788120012.0,
    "source": "direct"
  },
  "latest": {
    "id": "job-id",
    "state": "done",
    "model": "owner/repository",
    "progress": 1.0,
    "created_at": 1788119900.0,
    "started_at": 1788119902.0,
    "finished_at": 1788119918.0,
    "runtime_s": 16.0,
    "source": "direct",
    "error": null
  }
}
```

Only `queued`, `running`, `done`, `error`, and `cancelled` are valid states. Progress is normalized to `0..1`. Text is bounded and safe for display. The reporter exposes at most the active job and latest terminal job; it does not return paths, prompts, credentials, or full history.

### Hub observation and retention

Studio Hub already polls every registered Studio every five seconds. After a successful health probe it fetches the optional authenticated activity snapshot, validates it, and records meaningful state transitions in a small `machine_activity` table in the existing ledger. A 404 means the Studio is an older compatible version. Observations are idempotent by machine, Studio, job ID, and state. Rows older than 30 days are pruned opportunistically.

Hub-dispatched broker evidence remains authoritative when the same job is visible through both paths. Reporter observations fill the direct-generation gap and provide recent local state. The Hub never double-counts a job solely because it was observed repeatedly.

### Machine state model

The Stats endpoint derives one state per registered machine using this precedence:

1. `needs_attention`: a reachable Studio has a recent terminal error or the machine has a current health problem.
2. `working`: at least one Studio reports queued/running work or the broker owns an active lease.
3. `offline`: the machine or every tracked Studio is unreachable.
4. `just_finished`: the latest successful completion is less than 15 minutes old.
5. `long_idle`: the latest observed activity is at least 2 hours old.
6. `ready`: reachable, compatible, and inactive for less than 2 hours.
7. `unknown`: insufficient evidence, used only for mixed-version or first-observation states.

State precedence is deterministic. A terminal failure remains visible under `needs_attention` until later successful activity proves recovery; it does not permanently poison the machine.

## Stats experience

The existing Stats tab gains an operational section above all historical charts.

- **Fleet pulse** shows counts for Working, Just finished, Ready, Long idle, Offline, and Needs attention.
- **Machine activity** lists every registered machine, sorted by attention first, then working, just finished, ready, long idle, and offline.
- Each row shows state and duration, current or latest Studio/model, progress or last result, last activity time, completed and failed work in the selected window, median comparable runtime, and useful utilization.
- Expanding a row shows its recent activity timeline and evidence limitations.
- Existing window, source, machine filters, throughput chart, per-machine table, matrix, and per-model analysis remain below as **Historical performance**.

The machine board updates through the dashboard's existing summary/refresh mechanisms. Loading uses the incumbent skeleton/empty-state vocabulary; errors state what evidence is unavailable without hiding the rest of the fleet.

## Performance semantics

- Use median runtime, not average, for performance comparisons.
- A comparison is valid only within the same Studio and model and after at least three successful timed jobs per machine in the selected window.
- Show relative language such as `18% faster than fleet median` only when at least two machines have sufficient comparable evidence.
- Utilization means observed running time divided by observed reachable time in the selected window. If direct activity reporting was unavailable for part of the window, label it `partial` rather than presenting a precise percentage.
- Direct output scans without activity events still contribute to the existing historical output counts but not to utilization or exact runtime.

## Compatibility and safety

- The activity endpoint is optional, so all existing Hub and Studio versions continue to interoperate; a 404 becomes an explicit compatibility limitation rather than a health failure.
- No new network port, daemon, dependency, credential, or write endpoint is introduced.
- The polling payload excludes prompts, filesystem paths, reference audio,
  generated assets, and tokens. The approved follow-up design may retrieve
  those job details on demand from the originating Studio without adding them
  to polling, the activity ledger, or central retention.
- GenStudio APIs, routing, lease behavior, updates, enrollment, and memory controls are unchanged.
- The operational page never blocks the existing Stats response when one Studio or machine is unavailable.

## Testing and verification

- Image and Voice unit tests prove health activity normalization for queued, running, completed, failed, missing, and malformed job state.
- Hub tests prove contract validation, idempotent persistence, 30-day pruning, state precedence, mixed-version compatibility, de-duplication with broker evidence, and like-for-like median comparisons.
- Frontend tests prove the operational section renders every state, clear duration and limitation labels, keyboard-accessible row expansion, and preserves the historical controls.
- Full test suites run independently in all three repositories, followed by desktop and narrow-width browser inspection of the Stats page.

## Explicit anti-goals

- No generic telemetry platform, external analytics database, or new package.
- No opaque single-number machine score.
- No performance comparison across different models or workloads.
- No attempt to reconstruct exact past utilization before activity reporting existed.
- No change to fleet scheduling or customer-visible GenStudio behavior.
