---
kind: studiohub.claude-task
schema_version: 1
task_id: TASK-003
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
title: Convert Group B findings into exact next batches
state: completed
priority: high
execution: sequential
dependencies:
  - TASK-001
  - TASK-002
parallel_group: null
repository_root: /Users/thengmacmini/pinokio/api/studiohub-mac
orchestration_root: /Users/thengmacmini/pinokio/api/studiohub-mac
base_branch: main
base_commit: eb1c1ce6035858eba4d901b86b434e44eeda7479
task_branch: claude/group-b-triage-integration-plan
worktree_path: /Users/thengmacmini/pinokio/.claude-worktrees/studiohub-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-003
allowed_paths:
  - AGENTS.md
  - .claude-work/README.md
  - .claude-work/REPORT_TEMPLATE.md
  - .claude-work/BATCH_REPORT_TEMPLATE.md
  - .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001.md
  - .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002.md
forbidden_paths:
  - .env
  - .env.*
  - ENVIRONMENT
  - credentials/**
  - data/**
  - logs/**
  - app/**
  - CAPABILITY_CONTRACT.md
  - CONTROLLER_ARCHITECTURE.md
  - studiohub_genstudio_integration.md
  - ../voicestudio-mac.git/**
contract_files: []
report_path: .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-003.md
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
  stop_condition: All external, source-changing, and live actions are denied.
created_by: controller
created_at: 2026-08-02T17:01:03Z
updated_at: 2026-08-02T17:01:03Z
---

# Objective

Combine TASK-001 and TASK-002 into an implementation-ready decision matrix and
a sequence of safe, independently committable future batches without changing
source or starting qualification.

## Why this task exists

The controller needs one place to decide what can be accepted as existing
evidence, what Voice Studio must implement, what Studio Hub must implement, and
which exact live tests require owner scheduling on 8, 16, and 24 GB machines.

## Required reading

- Studio Hub `AGENTS.md`
- `.claude-work/README.md`
- The active batch and all three task files
- The complete TASK-001 and TASK-002 reports, including blockers
- `.claude-work/REPORT_TEMPLATE.md`
- `.claude-work/BATCH_REPORT_TEMPLATE.md`

## Architecture boundaries

- Do not move model-specific preprocessing, chunking, synthesis, joining, or
  artifact validation into Studio Hub.
- Do not move global approval, products, prices, publication, customer assets,
  retention, or billing out of GenStudio.
- Do not let a sibling candidate trigger approval, caching, routing, or customer
  visibility.
- Every shared contract change requires an explicit schema version, producer,
  consumers, rollout order, fake-worker tests, compatibility, recovery, and
  rollback plan in a later contract task.

## In scope

- Reconcile contradictions between the two reports using cited evidence only.
- Produce one exact row per checkpoint and operation with current state, missing
  evidence, owning repository, required machine tier, and controller decision.
- Recommend future batches split by repository and disjoint paths:
  - Voice Studio adapter/capability fixes;
  - Voice Studio durable audit-record work;
  - Studio Hub contract/ingestion work, only if proven necessary;
  - offline fake-worker/integration tests;
  - live model qualification on owner-approved 8/16/24 GB machines;
  - later samples and commercial approval review.
- State dependencies, parallel opportunities, allowed paths, forbidden paths,
  permissions, verification, rollout order, and stop conditions for each future
  batch proposal.

## Out of scope

- Creating the proposed future queue files.
- Editing source, tests, contracts, release metadata, or application docs.
- Running tests already owned by TASK-001 or TASK-002 again.
- Downloading, generating, accessing live machines, approving models, changing
  desired state, merging, pushing, restarting, or deploying.

## Allowed-path rationale

This task needs only the two dependency reports and orchestration templates.
Application and contract paths are forbidden because this is a synthesis and
controller-decision task, not implementation.

The only authorized mutations are this task's state move, its sanitized report,
and the required final batch report under the Studio Hub orchestration root.

## Planning requirements

1. Do not convert an unverified catalog claim into a fact.
2. Preserve exact checkpoint, operation, revision, and contract-hash identity.
3. For each model, distinguish:
   - implemented but unaudited;
   - audit-ready offline;
   - requires adapter or dependency repair;
   - requires live 8/16/24 GB qualification;
   - license/commercial decision blocked;
   - internal-only candidate;
   - potentially sellable candidate pending owner approval.
4. Treat 40,000 characters as an adapter-managed public job requirement, not a
   raw one-pass requirement. Plan quality and integrity testing accordingly.
5. Include every built-in voice and supported language in later model-specific
   coverage where applicable, with English as the default rather than the only
   supported language.
6. Plan voice-clone reference testing around an immutable original plus
   model-specific derived references; do not propose generic destructive
   splitting, looping, stretching, or fabricated speech.
7. Put source implementation and live qualification in separate batches so
   power availability and fleet idleness cannot compromise source review.

## Verification commands

Run from the Studio Hub controller checkout after both dependency reports exist.

```sh
test -s .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001.md
test -s .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002.md
rg -n "Chatterbox|OmniVoice|Qwen3-TTS|VoxCPM2|VibeVoice|Fish Audio" .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001.md .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002.md
git diff --check -- .claude-work
```

## Acceptance criteria

- [ ] Every Group B checkpoint/operation has a next action and owner.
- [ ] Proposed batches are repository-separated and independently committable.
- [ ] Live qualification is separate from source implementation.
- [ ] Owner decisions and evidence blockers are explicit.
- [ ] No new task manifest, source file, contract, test, or commit was created.
- [ ] All required report checks passed, or the exact sanitized failure is reported.
- [ ] One final `BATCH_REPORT.md` was written after this task report.

## Safe stop conditions

Stop and mark blocked if either dependency report is missing. If one dependency
is present but blocked, use its bounded findings and explicitly carry the
blocker into the plan; never invent the missing evidence.

## Controller decisions already recorded

- Codex, `2026-08-02T17:01:03Z`: authorize report synthesis, task-state moves,
  and final sanitized task/batch report writing only.
- Human owner: later live testing waits until machines are powered, reachable,
  fully updated, idle, and not downloading or maintaining models.

## State history

- `2026-08-02T17:01:03Z` — created in `queue/` by controller.
- `2026-08-02T17:23:50Z` — moved to `active/` by Claude worker. Both dependency reports exist (each blocked with bounded findings), satisfying the batch Phase 2 gate.
- `2026-08-02T17:26:02Z` — moved to `completed/` by Claude worker. All four verification commands passed; synthesis delivered from both bounded dependency reports.
