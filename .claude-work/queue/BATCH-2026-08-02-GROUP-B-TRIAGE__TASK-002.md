---
kind: studiohub.claude-task
schema_version: 1
task_id: TASK-002
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
title: Audit Studio Hub readiness to ingest audited Group B candidates
state: queue
priority: high
execution: parallel
dependencies: []
parallel_group: evidence-audit
repository_root: /Users/thengmacmini/pinokio/api/studiohub-mac
orchestration_root: /Users/thengmacmini/pinokio/api/studiohub-mac
base_branch: main
base_commit: eb1c1ce6035858eba4d901b86b434e44eeda7479
task_branch: claude/group-b-triage-hub-readiness
worktree_path: /Users/thengmacmini/pinokio/.claude-worktrees/studiohub-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002
allowed_paths:
  - AGENTS.md
  - CAPABILITY_CONTRACT.md
  - CONTROLLER_ARCHITECTURE.md
  - studiohub_genstudio_integration.md
  - app/backend/capabilities.py
  - app/backend/memory_admission.py
  - app/backend/model_baselines.py
  - app/backend/model_exposure.py
  - app/backend/monitor.py
  - app/backend/peers.py
  - app/backend/registry.py
  - app/backend/resources.py
  - app/backend/shared_voices.py
  - app/tests/conftest.py
  - app/tests/test_capabilities.py
  - app/tests/test_model_baselines.py
  - app/tests/test_model_exposure.py
  - app/tests/test_monitor.py
forbidden_paths:
  - .env
  - .env.*
  - ENVIRONMENT
  - credentials/**
  - data/**
  - logs/**
  - outputs/**
  - app/backend/migrations/**
  - ../voicestudio-mac.git/**
  - ../imagestudio-mac/**
contract_files:
  - CAPABILITY_CONTRACT.md
  - CONTROLLER_ARCHITECTURE.md
  - studiohub_genstudio_integration.md
report_path: .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002.md
permissions:
  local_commit: denied
  push: denied
  paid_provider_calls: denied
  production_data_read: denied
  production_data_write: denied
  credential_read: denied
  credential_write: denied
  create_migration: denied
  apply_migration_local: denied
  apply_migration_production: denied
  restart_services: denied
  deploy: denied
  live_configuration_change: denied
  live_fleet_state_change: denied
controller_gates:
  merge: pending
  push: pending
  production_data_write: pending
  credential_change: pending
  migration_application: pending
  restart: pending
  deployment: pending
  live_configuration: pending
  live_fleet_state: pending
provider_call_policy:
  providers: []
  endpoints: []
  maximum_spend_usd: 0
  maximum_requests: 0
  test_data: none
  stop_condition: Provider calls, worker network calls, and live controller calls are denied.
created_by: controller
created_at: 2026-08-02T17:01:03Z
updated_at: 2026-08-02T17:01:03Z
---

# Objective

Determine exactly which audited Group B fields and lifecycle behaviors Studio
Hub can already ingest, persist, gate, aggregate, and expose, and identify only
the concrete contract or test gaps that require later implementation.

## Why this task exists

Voice Studio will eventually report exact audited candidates. Studio Hub must
accept that evidence without becoming the qualification authority, must keep
candidates unapproved, and must report global desired-state and fleet supply
truthfully. We need a source-backed readiness assessment before implementing
or expanding any shared contract.

## Required reading

- Studio Hub `AGENTS.md`
- `.claude-work/README.md`
- The active batch and this complete task
- `CAPABILITY_CONTRACT.md`
- `CONTROLLER_ARCHITECTURE.md`
- `studiohub_genstudio_integration.md`
- Every allowed backend and test file relevant to a reported conclusion

## Architecture boundaries

- Studio Hub is a site controller, not the global commercial authority.
- GenStudio owns the approved global desired-state catalog.
- Sibling workers own capability and audit evidence and execute assigned jobs.
- Candidate discovery, local availability, approval, desired caching, routing,
  and customer publication are distinct states.
- A sibling candidate must never become automatically approved, cached,
  routed, or customer-visible.
- Capability GET must remain cache-only and must not contact workers.
- Unreachable workers retain last-good inventory as stale evidence, not ready
  capacity.
- Detailed worker and machine evidence remains authoritative; aggregates are
  derived views.

## In scope

- Candidate identity: exact model ID, operation, immutable runtime revision,
  audit contract hash, adapter identity, and audit pass state.
- Voice-specific capability fields for built-in voices, languages, cloning,
  design, reference requirements, adapter-managed long form, chunk-level
  progress, final speed control, limits, artifacts, and resource evidence.
- Durable catalog refresh, last-good persistence, staleness, approval removal,
  desired caching, hardware eligibility, and supply aggregation.
- Existing unit tests that prove or fail to prove these behaviors.
- A matrix of `already supported`, `contract extension needed`, `test only`, or
  `owned by Voice Studio/GenStudio` for every identified requirement.

## Out of scope

- Editing any Studio Hub file or contract.
- Editing Voice Studio, GenStudio, or another sibling repository.
- Reading live controller state, calling workers, accessing credentials or
  production data, changing desired state, or triggering downloads.
- Approving or revoking a model, changing memory policy, restarting a service,
  or deploying anything.
- Inventing a shared contract. Proposed fields must be labeled proposals and
  assigned to a future versioned contract task.

## Allowed-path rationale

The allowlist covers the current source of truth for cache-only discovery,
exact exposure identity, GenStudio desired-state reconciliation, memory
admission, supply aggregation, and private/shared voice behavior, plus the
focused tests that assert those boundaries. No runtime data or migrations are
needed for this read-only assessment.

The only authorized mutations are this task's queue-state moves and sanitized
report under the Studio Hub `orchestration_root`; no application or contract
file may change.

## Evidence requirements

1. Map each requirement to exact source and test evidence rather than relying
   on architecture prose alone.
2. Verify candidate eligibility requires deliberate sibling audit evidence and
   that Studio Hub exposure remains exact by model, operation, immutable
   revision, and contract hash.
3. Verify global desired state from GenStudio cannot be overridden silently at
   a site and that removal stops new targeting without deleting partial/cache
   evidence.
4. Verify catalog refresh is independent, bounded, non-overlapping, concurrent
   where safe, last-good persisted, stale-aware, and cache-only on read.
5. Verify supply retains ready, busy, offline, quarantined, incompatible, and
   unknown-hardware states separately with per-machine reasons and slots.
6. Compare the current capability payload to the Voice Studio Group B evidence
   that will be needed: complete voices/languages, reference preparation
   constraints and revision, operation-specific limits, adapter-managed long
   form, resource telemetry, and artifact/retry semantics.
7. Identify fields that belong only inside Voice Studio's worker contract,
   fields Studio Hub should pass through, and fields GenStudio owns. Do not
   duplicate authority.
8. Produce a bounded future Studio Hub implementation/test backlog. Separate
   source changes, shared contract changes, and UI/operator changes.

## Verification commands

Run every command from the pinned Studio Hub worktree. Tests must use fixtures
and cached fake-worker evidence only.

```sh
git status --short --branch
/Users/thengmacmini/pinokio/api/studiohub-mac/conda_env/bin/pytest -q app/tests/test_model_exposure.py app/tests/test_model_baselines.py app/tests/test_capabilities.py app/tests/test_monitor.py
git diff --check
git status --short --branch
```

## Acceptance criteria

- [ ] Every conclusion cites current source and/or test evidence.
- [ ] Existing support is separated from proposed contract extensions.
- [ ] Voice Studio, Studio Hub, and GenStudio ownership stays explicit.
- [ ] Candidate discovery cannot be mistaken for exposure or desired caching.
- [ ] Cache-only, stale last-good, exact approval, and supply semantics are covered.
- [ ] No Studio Hub source/contract file changed and no local commit was created.
- [ ] Every verification command passed, or the exact sanitized failure is reported.
- [ ] No secrets, private endpoints, customer data, or raw worker errors appear.
- [ ] The task report was written immediately after work stopped.

## Safe stop conditions

Stop and move the task to `blocked/` if a conclusion requires a forbidden
runtime file, worker call, credential, production record, source edit, or an
unmade authority decision. Report bounded gaps without guessing.

## Controller decisions already recorded

- Codex, `2026-08-02T17:01:03Z`: authorize read-only source inspection, isolated
  worktree creation, focused local fake-worker tests, task-state moves, and
  sanitized report writing only.
- Human owner: models enter the global approved catalog only after deliberate
  audit and review; eligible devices may cache them only after that global
  desired-state decision.

## State history

- `2026-08-02T17:01:03Z` — created in `queue/` by controller.
