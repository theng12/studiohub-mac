# Durable Release Integration and Rollout Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Safely join released Image/Voice exact updaters, Studio Hub 2.8.0 reconciliation, and GenStudio durable release intent into one controlled production path.

**Architecture:** This plan changes no execution algorithm. It enforces compatibility ordering, release evidence, one-site canary, and the truthful PPS bootstrap boundary across the three independently released products.

## Prerequisites

- Image and Voice releases report managed_exact_commit true, app_version, and app_commit; their handoff names exact versions and commits.
- Studio Hub 2.8.0 has released intent APIs, durable state/adoption, schema-v3 evidence, and Managed release status card.
- GenStudio has released immutable intent delivery, MAX_SITE_CONCURRENCY=1, stale-supply quarantine, and recovery retries. PPS remains excluded unless it satisfies the pinned bootstrap boundary below.
- All repository worktrees are clean and every release has version/changelog/release-note evidence.

### Task 1: Verify cross-product contract compatibility

**Files:**
- Create: /Users/thengmacmini/Developer/_handoffs/2026-08-15_to-claude-genstudio_from-gpt-studiofleet_exact-release-contract-confirmed.md
- Modify: Studio Hub CAPABILITY_CONTRACT.md and studiohub_genstudio_integration.md only if a verified additive field differs.

**Interfaces:**
- Consumes: Image/Voice updater handoff, Studio Hub 2.8.0 API docs, GenStudio response handoff.
- Produces: one frozen compatible manifest shape and a recorded compatibility matrix.

- [ ] **Step 1: Write contract fixture test**

~~~python
def test_genstudio_manifest_is_accepted_and_response_identity_matches(authed):
    manifest = frozen_release_manifest(hub="2.8.0", image=IMAGE_SHA, voice=VOICE_SHA)
    accepted = authed.put("/api/hub/maintenance/release-intent", json=manifest)
    assert accepted.status_code == 200
    assert accepted.json()["release_id"] == manifest["release_id"]
    assert accepted.json()["site_id"] == "site-a"
~~~

- [ ] **Step 2: Run compatibility fixture**

Run: conda_env/bin/python -m pytest -q app/tests/test_api.py app/tests/test_release_reconciliation.py -k 'release_intent or identity'

Expected: PASS with exact Image/Voice production release commits, not placeholder values.

- [ ] **Step 3: Freeze and send contract confirmation**

Record schema version, body fields, response identity, state vocabulary, capability schema-v3 fields, and no-token boundary. If a discrepancy is additive, update tests/docs and release the owning repository before activation. If not additive, do not activate; return a concrete incompatibility handoff.

### Task 2: Run a controlled site canary

**Files:**
- Create: /Users/thengmacmini/Developer/_handoffs/2026-08-15_to-gpt-studiofleet_from-studiohub_controlled-release-canary.md

**Interfaces:**
- Consumes: one GenStudio owner-approved manifest and one reachable controlled location.
- Produces: measured proof that exact release, restart adoption, and capacity quarantine work.

- [ ] **Step 1: Select one reachable non-production/controlled site**

Use GenStudio one-site activation. Confirm no customer work is active; the controller drains work instead of force-cancelling.

- [ ] **Step 2: Verify event order and exact evidence**

Assert:
1. frozen release ID/sequence stays unchanged;
2. first reachable agent is canary;
3. each agent runs Hub then installed Image then Voice, serially;
4. controller Image/Voice run before controller Hub;
5. every current component reports requested app_version and app_commit;
6. forced restarts after intent receipt, after activation, between components, during a remote-Hub restart, after a lost POST/poll response, and during controller-Hub-last all adopt the same local and remote job IDs without duplicate component execution;
7. normal Off/Notify/Auto settings are byte-for-byte unchanged;
8. model-baseline reconciliation is requested and a later capability snapshot restores only current workers.

- [ ] **Step 3: Simulate one target-local failure**

Use a fake/offline test target or controlled unavailable agent. Verify pending_offline/retry evidence, reduced capacity, later target/site continuation, and idempotent replay of an identical POST after a dropped response: two HTTP requests may occur, but exactly one agent child job/execution must exist. Do not use production model generation.

- [ ] **Step 4: Write canary handoff**

Include release commits, route/job IDs, exact sequence, test/failure evidence, expected vs observed versions/SHA, schema-v3 routing result, and no secret/customer data.

### Task 3: Enable automatic return and manage PPS truthfully

**Files:**
- Create: /Users/thengmacmini/Developer/_handoffs/2026-08-15_to-claude-genstudio_from-gpt-studiofleet_rejoin-rollout-ready.md

**Interfaces:**
- Consumes: successful canary and GenStudio pending-site coordinator.
- Produces: automatic rejoin enabled for current/future sites without a manual retry rule.

- [ ] **Step 1: Verify one offline-site recovery simulation**

Start with a site pending delivery. Restore only its Hub reachability. Confirm GenStudio queues the same frozen intent, controller adopts/executes it, and a later fresh capability snapshot re-enables only converged workers. Verify daily retry ceiling by setting the test clock rather than waiting.

- [ ] **Step 2: Verify new-site inheritance**

Register a new enabled site in GenStudio. Confirm it receives a delivery row for the current intent without hard-coded site ID, hostname, IP range, or machine count.

- [ ] **Step 3: Process PPS through the documented boundary**

On PPS recovery probe the intent endpoint. A known 2.6.x controller is never
given legacy `update.js`/moving-main bootstrap: it could advance past the frozen
commit and exact no-downgrade validation must reject it. Mark the site
`physical_bootstrap_required` and retain it pending/nonblocking unless an
operator applies a separately published immutable bootstrap artifact or commit
that is attested as an ancestor of the desired Hub target. After that observed
bootstrap, either deliver the still-valid frozen intent or have the owner issue
a new immutable intent whose Hub target descends from the observed bootstrap.
If this proof is unavailable, leave PPS excluded and continue all other sites.
Do not describe PPS as automatically recovered until an exact intent has been
observed current.

- [ ] **Step 4: Send final operating handoff**

Tell GenStudio the Hub side is enabled, list release versions/commits, confirm daily recovery/retry and stale quarantine semantics, and include the exact conditions that make PPS physical bootstrap required.

## Completion gate

Do not enable fleet-wide automatic activation until contract fixture, canary, target-local failure, restart adoption, offline recovery, future-site inheritance, release metadata, full suites, and handoffs all pass. One unavailable node or site remains nonblocking; one immutable manifest-wide integrity failure stops that manifest only.
