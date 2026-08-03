---
kind: studiohub.claude-task
schema_version: 1
task_id: TASK-001
batch_id: BATCH-2026-08-03-VOICE-JOB-RECOVERY
title: Add bounded cancellation for native Voice Studio inference
state: queue
priority: high
execution: sequential
dependencies: []
parallel_group: null
repository_root: /Users/thengmacmini/pinokio/api/voicestudio-mac.git
base_branch: main
base_commit: 233a559c379f1fc5519e6babb9990c80f2d7a6b2
task_branch: codex/voice-native-cancellation
worktree_path: /Users/thengmacmini/pinokio/.codex-worktrees/voicestudio-mac/BATCH-2026-08-03-VOICE-JOB-RECOVERY/TASK-001
allowed_paths:
  - app/backend/generation.py
  - app/backend/main.py
  - app/backend/resource_telemetry.py
  - app/tests/
  - docs/
  - VERSION
  - CHANGELOG.md
forbidden_paths:
  - .env
  - cache/
  - app/output/
  - app/voices/
  - model-audits/
contract_files: []
report_path: .claude-work/reports/BATCH-2026-08-03-VOICE-JOB-RECOVERY/TASK-001.md
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
  test_data: synthetic-only
  stop_condition: Provider and live model calls are denied.
created_by: controller
created_at: 2026-08-03T14:29:50Z
updated_at: 2026-08-03T14:29:50Z
---

# Objective

Make a Voice Studio job cancellable even when the currently executing native
model call does not cooperatively return.

## Why this task exists

Current local TTS cancellation is checked between adapter-managed sections.
One hung or extremely slow native section can still hold the worker and model
lock indefinitely. The old unbounded OmniVoice qualification demonstrated this
failure mode; the 288-character fix reduced exposure but did not eliminate it.

## Architecture boundaries

- Voice Studio owns this execution boundary and terminal state.
- Studio Hub may request cancellation but must not kill an unidentified process.
- Cancellation must not fabricate a successful artifact.
- Partial files must never appear as terminal output.
- The implementation must preserve exact job and client-request identity.

## In scope

- Execute cancellable model work behind a bounded, identifiable process boundary
  or an equally strong cooperative mechanism.
- Add a configurable grace period followed by bounded termination of only the
  exact job-owned execution process.
- Report `cancel_requested`, `cancelled`, `failed`, and `uncertain` distinctly.
- Retain section-level progress and resource telemetry.
- Clean partial section directories and release accelerator memory.
- Cover cancellation before generation, during a section, between sections,
  during joining, and during final speed processing.

## Out of scope

- Studio Hub service restart controls.
- Customer retry policy, billing, or publication.
- Real fleet restarts or live model calls.

## Verification commands

```sh
python3 release_metadata_check.py
python3 -m pytest app/tests -q
git diff --check
```

## Acceptance criteria

- [ ] A fake native call that ignores cancellation is terminated within a bounded deadline.
- [ ] Only the job-owned process is terminated.
- [ ] No partial artifact is returned.
- [ ] Terminal identity survives polling and restart recovery.
- [ ] Accelerator and temporary resources are released.
- [ ] Version and changelog describe the shipped behavior truthfully.

## Safe stop conditions

Stop if implementation requires killing a shared server process, changing the
accepted-job retry contract without a coordinated plan, or exercising a live
fleet/model without explicit controller authorization.

## Controller decisions already recorded

- Owner requested this remain on the high-priority repair list on 2026-08-03.

## State history

- `2026-08-03T14:29:50Z` — queued by Codex.
