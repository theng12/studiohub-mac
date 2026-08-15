# Durable Release Intent and Automatic Rejoin Design

**Status:** implemented for Studio Hub 2.8.0; final release review, commit,
controlled canary, and GenStudio activation remain gated by the rollout plan
**Owner:** StudioFleet / Studio Hub
**Release target:** Studio Hub 2.8.0

## Outcome

GenStudio owns one immutable global release intent and one-site-at-a-time activation. Each Studio Hub controller validates, stores, executes, resumes, and reports the exact release inside its site. A down, busy, underspec, auth-rejected, or failed machine is retained as pending evidence and never blocks a healthy machine or later site.

This is the approved umbrella design for Studio Hub, Image Studio, and Voice
Studio. Their shared updater implementation is deliberately released first;
the controller reconciler is never allowed to treat it as an assumed future
capability. GenStudio's counterpart is
`/Users/thengmacmini/Developer/_handoffs/2026-08-15_to-claude-genstudio_from-gpt-studiofleet_durable-release-intent-and-auto-rejoin.md`.

## Scope

| Managed component | Required repository | Rule |
| --- | --- | --- |
| Hub | `theng12/studiohub-mac` | all registered Hubs |
| Image | `theng12/imagestudio-mac` | installed only |
| Voice | `theng12/voicestudio-mac` | installed only |

Chat, Music, Video, and Render remain untouched. Hardware/model eligibility is separate from software release state. The implementation changes neither normal `Off` / `Notify` / `Auto` settings nor their scheduling semantics, starts no latest-GitHub polling, adds no dependency or launcher change, and never performs generation work on the development Mac.

## Ownership and order

```text
owner action in GenStudio
  -> immutable release manifest, one active site globally
  -> controller PUT validates + persists; POST creates/adopts one local job
  -> first reachable remote stable machine: Hub, Image, Voice (canary)
  -> remaining remote machines in stable-ID order, same serial bundle
  -> controller Image, then Voice, then controller Hub
  -> controller requests approved model catalog reconciliation
  -> GenStudio restores only fresh software + model-contract supply
```

No two components or machine bundles overlap. The controller drains only the component it is about to change and never cancels work. Offline, busy, disk, auth, or one-machine health failures are target-local. A malformed manifest, expected-SHA/version mismatch, or the same clean-checkout health failure on two distinct stable machines is `blocked_release` and stops subsequent fanout.

## Immutable manifest and API

```json
{
  "schema": "genstudio.studio-fleet-release-intent",
  "schema_version": 1,
  "release_id": "sha256:<64-lowercase-hex>",
  "sequence": 12,
  "created_at": "2026-08-15T00:00:00Z",
  "components": {
    "hub": {"repository": "theng12/studiohub-mac", "version": "2.8.0", "commit": "<40-lowercase-hex>"},
    "image": {"repository": "theng12/imagestudio-mac", "version": "<image-release-semver>", "commit": "<40-lowercase-hex>", "installed_only": true},
    "voice": {"repository": "theng12/voicestudio-mac", "version": "<voice-release-semver>", "commit": "<40-lowercase-hex>", "installed_only": true}
  }
}
```

`release_id` is `sha256:` plus SHA-256 of UTF-8 canonical JSON with `release_id` omitted: sorted keys, `,`/`:` separators, no whitespace. Commits are exactly lowercase 40-hex, versions are SemVer triples, and repository names must match the table exactly. The Hub verifies the canonical hash, schema, timestamp, sequence, component set, and all component fields before state mutation. It rejects changed content under an existing ID and a lower sequence.

| Method | Endpoint | Behaviour |
| --- | --- | --- |
| `PUT` | `/api/hub/maintenance/release-intent` | controller-only persisted desired intent; exact duplicate is idempotent |
| `GET` | `/api/hub/maintenance/release-intent` | sanitized intent, site/controller identity, and job summary |
| `POST` | `/api/hub/maintenance/release-intent/{release_id}/activate` | returns `202` + a durable job; replay adopts it |
| `GET` | `/api/hub/maintenance/release-jobs/{job_id}` | sanitized per-machine/component/catalog/retry evidence |
| `POST` | `/api/hub/maintenance/managed-update` | agent-only serial local bundle, authenticated by `X-Hub-Token`; identical replay adopts the existing child job |
| `GET` | `/api/hub/maintenance/managed-update/{job_id}` | agent job adoption/polling result |

The controller derives a deterministic child `operation_id` from canonical `release_id`, stable machine ID, and literal `managed-update`; it does not include a secret. The agent persists this operation identity and child job atomically before returning. If the initial POST response is lost, the controller repeats the identical POST body and the agent returns the original `job_id` with `adopted: true`; the replay is an allowed duplicate HTTP request, never duplicate execution. GenStudio uses its existing machine credential, never an owner browser cookie. Controller responses include configured `site_id` and `controller_id`; GenStudio matches them to its record. Tokens, checkout paths, command lines, and customer data never enter intent/job JSON, logs, query strings, or UI.

## Exact update primitive

The existing `update.js` executes `git pull`, so it is forbidden from managed release execution. Existing `AutoUpdater` is the reusable safety primitive: it already has fixed origin/main validation, clean checkout, flock locking, dependency/import checks, restart, rollback, and durable busy retry. Extend it in Hub, Image, and Voice with an optional all-or-nothing managed tuple:

```json
{"after_current": true, "target_commit": "<40-hex>", "target_version": "<component-release-semver>", "operation_id": "<bounded-opaque-id>"}
```

Normal requests that omit all three managed fields are unchanged. A managed request leaves saved mode/frequency/hour/idle settings untouched. It atomically stores the requested tuple before spawning work; same operation/target adopts state, a different active target returns 409.

Before mutation, AutoUpdater must retain current preflight and additionally prove: requested SHA resolves after fetch; target is an ancestor of `origin/main`; local `HEAD` is an ancestor of target (no downgrade/divergence); `git show <target>:VERSION` equals target version; and no branch rewrite removed it. It merges only `git merge --ff-only <target_commit>`, never `origin/main`. Its status persists requested/started/completed/rollback commits and the health check requires loaded version **and** loaded app commit equal the requested target.

Hub `/api/health` and `/api/version` gain `app_commit`, captured from safe local Git metadata at process start. Image and Voice must advertise the same field and managed capability before the controller accepts them as current. A legacy app is `retryable_failure` with `exact component updater unavailable`; it never silently falls back to `update.js`.

## Durable controller state

`release_reconciliation.json`, atomically replaced under `DATA_DIR`, is separate from history-only `fleet_versions.json`. Current `fleet_ops._load_state()` terminalizes interrupted work; the new reconciler must not use that behaviour.

```text
desired: manifest + received_at
activation: release_id + activated_at + optional GenStudio run reference
jobs[job_id]: state + timestamps + catalog evidence
  machines[stable_machine_id]: last host evidence + optional agent_job_id
    components[hub|image|voice]: expected/observed version+SHA, state,
      attempt, sanitized last_error, next_retry
```

Site states: `pending`, `queued`, `running`, `waiting_busy`, `degraded`, `blocked_release`, `complete`. Component states: `not_installed`, `pending_offline`, `pending_busy`, `checking`, `updating`, `restarting`, `verifying`, `current`, `retryable_failure`, `auth_blocked`, `release_blocked`. `complete` and `blocked_release` are the only terminal states. `degraded` remains nonterminal whenever any component is `pending_offline`, `pending_busy`, `retryable_failure`, or `auth_blocked`; it must retain a persisted `next_retry`, survive restart, and re-enter the due scan.

Nonterminal jobs are adopted at Hub restart. A remote POST stores agent `job_id` before polling; a transport drop reconnects to that same ID. Retryable rows use 60s, 5m, 15m, 1h, 4h, then 24h delays. The reconciler scans due work every 15 minutes and peer recovery schedules that machine immediately.

## Model evidence, UI, and bootstrap

After due software components complete, the controller calls existing `FleetModelBaselines.reconcile()` and records only that catalog reconciliation was requested. It must not represent downloading models as complete. Capabilities advance from schema v2 to v3 with additive controller/machine/worker `managed_release` evidence: desired release ID, expected/observed version/SHA, state, retry time, convergence, and catalog timestamp. If an active desired release exists, missing/pending/blocked/mismatched Hub or worker evidence makes the worker non-routable with exactly `managed_release_pending`, `managed_release_blocked`, or `managed_release_mismatch`. Existing model audit, revision, cache, memory, busy, and health gates remain.

Updates receives a separate, status-only **Managed release** card. It shows target, state, canary, component rows, retry time, degraded/blocked explanation, and catalog evidence. It has no local activation/retry control and does not alter ordinary automatic update controls.

PPS was offline before this protocol. It is `pending_bootstrap`, not automatic-rejoin proof. Its existing 2.6.x remote update calls moving-main `update.js`, which cannot safely bootstrap an immutable target: it could overshoot the frozen commit and exact no-downgrade validation would correctly refuse convergence. Therefore GenStudio must not invoke legacy moving-main bootstrap. PPS stays `physical_bootstrap_required` until an operator applies a separately published, immutable bootstrap artifact/commit that is attested as an ancestor of the desired Hub target, or the owner creates a new immutable release intent after an observed bootstrap. Until one of those two proofs exists, PPS remains excluded while every other site continues.

## Acceptance evidence

Tests must prove canonical validation and idempotency, target tuple durability across busy/restart, exact SHA/version/origin/health verification, serial canary/controller-last order, nonblocking pending recovery, duplicate-safe remote adoption after a lost response, release-wide block, capability quarantine, model reconciliation request, token redaction, machine-token-only write authorization (owner cookie, missing/invalid/wrong-role token, and cross-origin rejection), unchanged normal updater settings, and matching 2.8.0 release metadata. Restart evidence must cover intent receipt, activation, between components, remote Hub restart, lost POST/poll, and controller-Hub-last self-update adoption. Full pytest, compile, JavaScript/shell checks, dependency check, `git diff --check`, review, one versioned release commit per repository, push, and a controlled one-site canary are mandatory before fleet rollout.
