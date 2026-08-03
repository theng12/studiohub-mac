---
kind: studiohub.claude-task
schema_version: 1
task_id: TASK-002
batch_id: BATCH-2026-08-03-VOICE-JOB-RECOVERY
title: Add service-aware Studio Hub recovery for stuck Voice Studio jobs
state: queue
priority: high
execution: sequential
dependencies:
  - TASK-001
parallel_group: null
repository_root: /Users/thengmacmini/pinokio/api/studiohub-mac
base_branch: main
base_commit: aacefb75e6023d5ad41c999e88bffea4b67923f6
task_branch: codex/voice-service-recovery
worktree_path: /Users/thengmacmini/pinokio/.codex-worktrees/studiohub-mac/BATCH-2026-08-03-VOICE-JOB-RECOVERY/TASK-002
allowed_paths:
  - app/backend/startup_services.py
  - app/backend/fleet_ops.py
  - app/backend/broker.py
  - app/backend/voice_qualification.py
  - app/backend/main.py
  - app/tests/
  - app/frontend/
  - docs/
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
  test_data: fake-worker-only
  stop_condition: Provider and live fleet calls are denied.
created_by: controller
created_at: 2026-08-03T14:29:50Z
updated_at: 2026-08-03T14:29:50Z
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
python3 -m pytest app/tests/test_startup_services.py app/tests/test_voice_qualification.py app/tests/test_fleet_ops.py -q
python3 release_metadata_check.py
git diff --check
```

## Acceptance criteria

- [ ] Recovery refuses unknown machine, service, or job identities.
- [ ] Normal drain/cancellation precedes escalation.
- [ ] Forced recovery requires an explicit controller decision.
- [ ] Only the verified Voice Studio service is restarted.
- [ ] Accepted work is reconciled and never automatically duplicated.
- [ ] Fake-worker tests cover restart failure, health timeout, and successful reconciliation.
- [ ] Version and changelog describe the shipped behavior truthfully.

## Safe stop conditions

Stop if service identity is ambiguous, another job is active, recovery would
touch an unrelated service, or a test requires a real restart or live fleet
mutation without new authorization.

## Controller decisions already recorded

- Owner requested this remain on the high-priority repair list on 2026-08-03.

## State history

- `2026-08-03T14:29:50Z` — queued by Codex; waits for TASK-001 contract and controller review.
