---
kind: studiohub.claude-task-report
schema_version: 1
batch_id: BATCH-000
task_id: TASK-000
task_state: completed
repository_root: /absolute/path/to/repository
worktree_path: /absolute/path/to/worktree
branch: claude/BATCH-000-TASK-000
base_commit: full-40-character-commit
head_commit: null
started_at: YYYY-MM-DDTHH:MM:SSZ
finished_at: YYYY-MM-DDTHH:MM:SSZ
---

# Outcome

State completed, blocked, or skipped first, followed by one short explanation.

## Changes made

- List each changed file and its purpose.
- Confirm every path was allowed.

## Git evidence

- Starting status:
- Ending status:
- Commit created: yes/no
- Commit SHA and subject, if authorized:
- Push performed: yes/no; normally no

## Verification

| Command | Result | Notes |
|---|---|---|
| `exact command` | passed/failed/not run | Sanitized explanation |

Include important counts and failure summaries without copying credentials,
private endpoints, customer data, or raw worker errors.

## Architecture and contract review

- Controller/worker boundary preserved:
- GenStudio desired-state ownership preserved:
- Candidate approval remains deliberate:
- Contract version or compatibility impact:
- Required rollout order:

## Permissions exercised

| Action | Authorized | Performed | Evidence |
|---|---|---|---|
| Local commit | yes/no | yes/no | Sanitized |
| Push | yes/no | yes/no | Sanitized |
| Paid provider call | yes/no | yes/no | Count and cost only |
| Production data | yes/no | yes/no | Sanitized |
| Credential access/change | yes/no | yes/no | Handle only; never value |
| Migration create/apply | yes/no | yes/no | Environment only |
| Restart/deploy/live change | yes/no | yes/no | Sanitized |

## Risks and limitations

- List concrete remaining risks, or `None identified`.

## Controller decisions required

1. State each exact decision needed before correction, merge, push, migration,
   restart, deployment, or live change.
2. Write `None` when no decision remains.

## Suggested next action

Give the controller one bounded next step. Do not perform it unless authorized.
