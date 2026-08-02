---
kind: studiohub.claude-task
schema_version: 1
task_id: TASK-000
batch_id: BATCH-000
title: Replace with one focused outcome
state: queue
priority: normal
execution: sequential
dependencies: []
parallel_group: null
repository_root: /absolute/path/to/repository
base_branch: main
base_commit: full-40-character-commit
task_branch: claude/BATCH-000-TASK-000
worktree_path: /absolute/path/outside/repository
allowed_paths:
  - exact/repository/relative/path
forbidden_paths:
  - .env
  - credentials/
  - other/repository/**
contract_files: []
report_path: .claude-work/reports/BATCH-000/TASK-000.md
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
  stop_condition: Provider calls are denied.
created_by: controller
created_at: YYYY-MM-DDTHH:MM:SSZ
updated_at: YYYY-MM-DDTHH:MM:SSZ
---

# Objective

State one observable, independently reviewable outcome.

## Why this task exists

Describe the user or architectural need without broadening scope.

## Required reading

- `.claude-work/README.md`
- `.claude-work/active/BATCH-000.md`
- List exact repository documents and contract files.

## Architecture boundaries

- Studio Hub remains the site controller.
- GenStudio reaches sibling workers only through Studio Hub.
- Sibling workers report capabilities and execute assigned jobs; they do not
  decide products, prices, publication, approval, or global routing.
- A candidate never becomes approved, cached, routed, or visible implicitly.
- Add task-specific invariants here.

## In scope

- List exact behavior to implement or inspect.

## Out of scope

- List adjacent work that must not be attempted.

## Allowed-path rationale

Explain why every allowed path is sufficient and disjoint from parallel work.

## Implementation requirements

1. Add exact requirements in execution order.
2. Label proposed backend or shared-contract changes explicitly.
3. Preserve backward compatibility or reference an approved rollout contract.

## Verification commands

Run every command from the task worktree. Replace placeholders before queueing.

```sh
command-that-must-pass
```

## Acceptance criteria

- [ ] The focused outcome is complete.
- [ ] Only allowed paths changed.
- [ ] Every verification command passed, or the exact failure is reported.
- [ ] No secrets, private endpoints, customer data, or raw worker errors appear.
- [ ] No unauthorized external or live action occurred.
- [ ] The task report was written immediately after work stopped.

## Safe stop conditions

Stop and move the task to `blocked/` when:

- a required path is not allowed;
- architecture or contract ownership is unclear;
- a security, migration, credential, production, or spending decision is
  missing;
- an active or uncertain job could be interrupted;
- verification cannot be completed without unauthorized action.

## Controller decisions already recorded

List explicit decisions with reviewer and timestamp. `None` is valid.

## State history

- `YYYY-MM-DDTHH:MM:SSZ` — created in `queue/` by controller.
