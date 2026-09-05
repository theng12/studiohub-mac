---
kind: studiohub.claude-task
schema_version: 1
task_id: TASK-002
batch_id: BATCH-2026-08-03-VOICE-JOB-RECOVERY
title: Add service-aware Studio Hub recovery for stuck Voice Studio jobs
state: complete
priority: high
execution: sequential
dependencies:
  - TASK-001
parallel_group: null
repository_root: /Users/thengmacmini/pinokio/api/studiohub-mac
base_branch: main
base_commit: b449709817afdf383cdcad02d8dae93e6b108356
task_branch: codex/hub-voice-job-recovery
worktree_path: /Users/thengmacmini/pinokio/api/studiohub-mac/.worktrees/hub-voice-job-recovery
allowed_paths:
  - app/backend/startup_services.py
  - app/backend/fleet_ops.py
  - app/backend/broker.py
  - app/backend/activity.py
  - app/backend/ledger.py
  - app/backend/voice_qualification.py
  - app/backend/main.py
  - app/tests/
  - app/frontend/
  - docs/
  - README.md
  - VERSION
  - CHANGELOG.md
forbidden_paths:
  - .env
  - data/
  - credentials/
  - app/backend/auth.py
  - app/backend/hf_credentials.py
contract_files: []
report_path: .claude-work/reports/BATCH-2026-08-03-VOICE-JOB-RECOVERY/TASK-002.md
permissions:
  local_commit: allowed
  push: allowed
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
  merge: approved
  push: approved
  production_data_write: denied
  credential_change: denied
  migration_application: denied
  restart: denied
  deployment: denied
  live_configuration: denied
  live_fleet_state: denied
provider_call_policy:
  providers: []
  endpoints: []
  maximum_spend_usd: 0
  maximum_requests: 0
  test_data: fake-worker-only
  stop_condition: Provider and live fleet calls are denied.
created_by: controller
created_at: 2026-08-03T14:29:50Z
updated_at: 2026-09-05T09:18:49Z
---

# Objective

Give Studio Hub a bounded, service-aware recovery workflow for a Voice Studio
job that remains stuck after normal cancellation.

## Why this task exists

Generic process controls cannot safely determine which managed Voice Studio
service owns a stuck accepted job. Recovery must verify the registered machine,
service identity, job identity, and drain state before any escalation.

## Architecture boundaries

- Voice Studio remains authoritative for job execution and terminal evidence.
- Studio Hub coordinates recovery and stores the recovery audit trail.
- A restart is a last resort after a normal cancellation deadline.
- Accepted or uncertain work is reconciled after recovery and never submitted again automatically.
- The controller cannot restart unrelated sibling services or active jobs.

## In scope

- Resolve the exact machine and managed Voice Studio startup service.
- Drain new work before escalation.
- Request normal cancellation and wait a bounded grace period.
- Require an explicit operator/controller gate before forced service recovery.
- Restart only the verified Voice Studio service through its supported service manager.
- Reconcile the original durable job identity after health returns.
- Surface sanitized recovery phase, reason, timestamps, and terminal outcome.
- Add fake-service and fake-worker tests for launchd and unsupported platforms.

## Out of scope

- Voice Studio process-boundary implementation from TASK-001.
- Automatic retry or rerouting of accepted work.
- Fleet deployment, real service restart, credentials, or customer data.

## Verification commands

```sh
../../conda_env/bin/python -m pytest app/tests -q
../../conda_env/bin/python -m pytest app/tests/test_release_metadata.py -q
../../conda_env/bin/python -m compileall -q app/backend
git diff --check
```

## Acceptance criteria

- [x] Recovery refuses unknown machine, service, or job identities.
- [x] Normal drain/cancellation precedes escalation.
- [x] Forced recovery requires an explicit controller decision.
- [x] Only the verified Voice Studio service is restarted.
- [x] Accepted work is reconciled and never automatically duplicated.
- [x] Fake-worker tests cover restart failure, health timeout, and successful reconciliation.
- [x] Version and changelog describe the shipped behavior truthfully.

## Safe stop conditions

Stop if service identity is ambiguous, another job is active, recovery would
touch an unrelated service, or a test requires a real restart or live fleet
mutation without new authorization.

## Controller decisions already recorded

- Owner requested this remain on the high-priority repair list on 2026-08-03.
- On 2026-09-05, the repository owner's standing integration rule authorized a
  normal commit, push, pull request, and merge after local verification and
  independent review. It did not authorize deployment, live service restart,
  live fleet mutation, production data access, credentials, or paid calls.
- On 2026-09-05, the controller reviewed the final source and the loopback-only
  desktop/mobile browser evidence and approved integration.

## State history

- `2026-08-03T14:29:50Z` — queued by Codex; waits for TASK-001 contract and controller review.
- `2026-09-05T09:18:49Z` — completed on `codex/hub-voice-job-recovery` from
  `b449709817afdf383cdcad02d8dae93e6b108356`; focused and full local suites,
  release metadata, compile, diff, and mock browser gates passed.
