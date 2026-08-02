---
kind: studiohub.claude-batch
schema_version: 1
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
title: Establish the truthful Group B voice-model qualification backlog
state: queue
execution_strategy: mixed
controller: Codex
human_owner: owner
created_at: 2026-08-02T17:01:03Z
updated_at: 2026-08-02T17:01:03Z
task_files:
  - BATCH-2026-08-02-GROUP-B-TRIAGE__TASK-001.md
  - BATCH-2026-08-02-GROUP-B-TRIAGE__TASK-002.md
  - BATCH-2026-08-02-GROUP-B-TRIAGE__TASK-003.md
controller_review:
  batch_ready: approved
  implementation_review: pending
  merge: pending
  push: pending
  deployment: pending
---

# Batch outcome

Produce a source-backed, model-by-model Group B evidence matrix for Voice
Studio, prove what Studio Hub can already ingest safely, and turn the combined
findings into independently committable implementation and qualification
batches. This is evidence and planning work only.

The six requested Group B families are Chatterbox, OmniVoice, Qwen3-TTS,
VoxCPM2, VibeVoice Realtime, and Fish Audio S2 Pro 8-bit. Treat every exact
checkpoint and operation as a separate qualification identity where the source
requires it; do not collapse variants merely because they share a family name.

## Required reading

- `.claude-work/README.md`
- `.claude-work/WORKER_PROMPT.md`
- Every task listed in `task_files`
- `CAPABILITY_CONTRACT.md`
- `CONTROLLER_ARCHITECTURE.md`
- `studiohub_genstudio_integration.md`

## Architectural invariants

- GenStudio → Studio Hub controller → sibling execution worker.
- Global desired state is GenStudio-owned and last-good persisted by Studio Hub.
- Sibling candidates remain unapproved until a deliberate global decision.
- A candidate is not automatically cached, routed, or customer-visible.
- Workers never define customer products, prices, publication, or routing.
- Voice Studio owns model-specific reference preparation, synthesis chunking,
  joining, artifact validation, and final speed processing.
- Studio Hub authenticates, schedules, transports, and aggregates evidence; it
  does not cut reference audio or manipulate generated audio.
- GenStudio owns the customer's original private reference asset, consent,
  retention, deletion, customer job, and billing state.
- A model may be sellable through qualified adapter-managed long form even when
  its native one-pass limit is far below 40,000 characters.
- Cross-repository changes use separate tasks and separate worktrees.
- Shared contract changes are versioned and backward-compatible or have an
  explicit coordinated rollout plan.

## Task graph

| Task | Repository | Depends on | Execution | Parallel group | Outcome |
|---|---|---|---|---|---|
| TASK-001 | `/Users/thengmacmini/pinokio/api/voicestudio-mac.git` | none | parallel | evidence-audit | Group B Voice Studio evidence matrix |
| TASK-002 | `/Users/thengmacmini/pinokio/api/studiohub-mac` | none | parallel | evidence-audit | Studio Hub ingestion/readiness matrix |
| TASK-003 | `/Users/thengmacmini/pinokio/api/studiohub-mac` | TASK-001, TASK-002 | sequential | integration-plan | Exact next implementation and qualification batches |

## Execution phases

1. **Phase 1 — parallel evidence:** run TASK-001 and TASK-002 independently in
   their pinned, disjoint worktrees.
2. **Phase 2 — sequential synthesis:** run TASK-003 only after both reports
   exist, including when either report records a bounded blocker.
3. **Phase 3 — controller gate:** stop after the final batch report. Do not
   begin implementation, model downloads, generation, or fleet work.

## Worktree plan

| Task | Base commit | Branch | Absolute worktree path | Allowed paths disjoint? |
|---|---|---|---|---|
| TASK-001 | `e5b764ac465f7e6cb75f03411bcd88e5afb7710d` | `claude/group-b-triage-voice-evidence` | `/Users/thengmacmini/pinokio/.claude-worktrees/voicestudio-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001` | yes, separate repository and report |
| TASK-002 | `eb1c1ce6035858eba4d901b86b434e44eeda7479` | `claude/group-b-triage-hub-readiness` | `/Users/thengmacmini/pinokio/.claude-worktrees/studiohub-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002` | yes, read-only and separate report |
| TASK-003 | `eb1c1ce6035858eba4d901b86b434e44eeda7479` | `claude/group-b-triage-integration-plan` | `/Users/thengmacmini/pinokio/.claude-worktrees/studiohub-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-003` | yes, report-only after dependencies |

The Voice Studio primary checkout currently contains unrelated modifications to
`app/backend/cache.py` and `app/backend/downloads.py`. Preserve them exactly.
The pinned clean base commit intentionally excludes those working-tree edits.

## Batch-wide permissions

Task permissions are authoritative and all operational actions are denied.

| Action | Batch policy | Controller review required |
|---|---|---|
| Local commits | denied | Yes before any future implementation |
| Push | denied | Always |
| Paid or free-tier provider calls | denied | Always |
| Model downloads or generation | denied | Always |
| Production data | denied | Always |
| Credentials | denied | Always |
| Migrations | denied | Always before application |
| Service restart | denied | Always |
| Deployment | denied | Always |
| Live configuration/fleet state | denied | Always |

## Shared verification

After the task reports exist, TASK-003 must verify the orchestration artifacts:

```sh
test -s .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001.md
test -s .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002.md
test -s .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-003.md
rg -n "Chatterbox|OmniVoice|Qwen3-TTS|VoxCPM2|VibeVoice|Fish Audio" .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE
```

## Batch completion criteria

- [ ] Every task is in `completed/`, `blocked/`, or `skipped/`.
- [ ] Every task has one sanitized report.
- [ ] Independent tasks continued despite unrelated blockers.
- [ ] Every exact Group B checkpoint and operation has a truthful evidence gap.
- [ ] RAM claims distinguish catalog assumptions from measured 8/16/24 GB data.
- [ ] Commercial/license status is evidence-backed and never inferred.
- [ ] The next batches separate Voice Studio, Studio Hub, and live qualification.
- [ ] One `BATCH_REPORT.md` summarizes every result and controller decision.
- [ ] No source change, commit, push, provider call, download, generation,
  restart, deployment, credential access, or live-state change occurred.

## State history

- `2026-08-02T17:01:03Z` — created in `queue/` and approved for read-only execution by Codex.
