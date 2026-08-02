---
kind: studiohub.claude-batch
schema_version: 1
batch_id: BATCH-000
title: Replace with one coherent batch outcome
state: queue
execution_strategy: mixed
controller: Codex
human_owner: owner
created_at: YYYY-MM-DDTHH:MM:SSZ
updated_at: YYYY-MM-DDTHH:MM:SSZ
task_files:
  - BATCH-000__TASK-001.md
controller_review:
  batch_ready: pending
  implementation_review: pending
  merge: pending
  push: pending
  deployment: pending
---

# Batch outcome

Describe the combined result and why the tasks belong in one batch.

## Required reading

- `.claude-work/README.md`
- Every task listed in `task_files`
- List architecture and contract documents shared by the batch.

## Architectural invariants

- GenStudio → Studio Hub controller → sibling execution worker.
- Global desired state is GenStudio-owned and last-good persisted by Studio Hub.
- Sibling candidates remain unapproved until a deliberate global decision.
- Workers never define customer products, prices, publication, or routing.
- Cross-repository changes use separate tasks and separate worktrees.
- Shared contract changes are versioned and backward-compatible or have an
  explicit coordinated rollout plan.

## Task graph

| Task | Repository | Depends on | Execution | Parallel group | Outcome |
|---|---|---|---|---|---|
| TASK-001 | `/absolute/repository` | none | sequential | none | Replace |

## Execution phases

1. **Phase 1 — sequential gate:** list tasks that establish contracts or facts.
2. **Phase 2 — parallel:** list tasks with disjoint repositories and paths.
3. **Phase 3 — sequential integration:** list verification or compatibility
   tasks that require prior outputs.

Independent ready tasks continue when another task is blocked. Do not bypass a
declared dependency.

## Worktree plan

| Task | Base commit | Branch | Absolute worktree path | Allowed paths disjoint? |
|---|---|---|---|---|
| TASK-001 | full SHA | branch | absolute path | yes/no |

## Batch-wide permissions

Task permissions are authoritative and may be stricter than this summary.

| Action | Batch policy | Controller review required |
|---|---|---|
| Local commits | Per task | Yes before integration |
| Push | Per task | Always |
| Paid provider calls | Per task; default denied | Always |
| Production data | Per task; default denied | Always |
| Credentials | Per task; default denied | Always |
| Migrations | Per task; default denied | Always before application |
| Service restart | Per task; default denied | Always |
| Deployment | Per task; default denied | Always |
| Live configuration/fleet state | Per task; default denied | Always |

## Shared verification

List commands that validate the integrated batch without replacing each task's
own commands.

```sh
command-that-validates-cross-task-contracts
```

## Batch completion criteria

- [ ] Every task is in `completed/`, `blocked/`, or `skipped/`.
- [ ] Every task has one report.
- [ ] Independent tasks continued despite unrelated blockers.
- [ ] Contract and rollout evidence is complete where applicable.
- [ ] One `BATCH_REPORT.md` summarizes every result and controller decision.
- [ ] No merge, push, deployment, restart, or live change occurred without the
  recorded controller gate.

## State history

- `YYYY-MM-DDTHH:MM:SSZ` — created in `queue/` by controller.
