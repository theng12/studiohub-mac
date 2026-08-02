---
kind: studiohub.claude-batch-report
schema_version: 1
batch_id: BATCH-000
batch_state: completed
started_at: YYYY-MM-DDTHH:MM:SSZ
finished_at: YYYY-MM-DDTHH:MM:SSZ
---

# Batch outcome

Lead with whether the batch achieved its intended outcome.

## Task results

| Task | Repository | State | Commit | Verification | Report |
|---|---|---|---|---|---|
| TASK-001 | path | completed/blocked/skipped | SHA/none | passed/failed | path |

## Dependency execution

Explain which tasks ran sequentially, which ran in parallel, and which
independent tasks continued after a blocker.

## Combined changes

Summarize changes by repository. Never imply that separate task branches have
already been merged.

## Contract and rollout summary

- Contract versions changed:
- Producer changes:
- Consumer changes:
- Backward compatibility:
- Required Studio Hub rollout order:
- Required sibling rollout order:
- Recovery and rollback:

## Verification summary

Summarize every task's commands and any batch-level integration checks. Link to
the task reports instead of reproducing large logs.

## Permission audit

State whether any commit, push, provider call, production-data access,
credential action, migration, restart, deployment, or live configuration/fleet
change occurred. Record authorization and controller review for each action.

## Unresolved risks and blockers

List exact blockers, affected tasks, and safe work that remains possible.

## Exact controller decisions needed

1. Decision, affected task or commit, and available choices.
2. Include merge, correction, push, rollout, deployment, and live-state gates.

## Recommended integration order

Provide a bounded, repository-by-repository review and merge order. Do not
merge, push, deploy, restart, or change live state from this report.
