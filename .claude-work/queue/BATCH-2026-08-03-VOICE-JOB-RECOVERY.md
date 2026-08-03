---
kind: studiohub.claude-batch
schema_version: 1
batch_id: BATCH-2026-08-03-VOICE-JOB-RECOVERY
title: Make Voice Studio jobs genuinely cancellable and safely recoverable
state: queue
execution_strategy: sequential
controller: Codex
human_owner: owner
created_at: 2026-08-03T14:29:50Z
updated_at: 2026-08-03T14:29:50Z
task_files:
  - BATCH-2026-08-03-VOICE-JOB-RECOVERY__TASK-001.md
  - BATCH-2026-08-03-VOICE-JOB-RECOVERY__TASK-002.md
controller_review:
  batch_ready: pending
  implementation_review: pending
  merge: pending
  push: pending
  deployment: pending
---

# Batch outcome

Prevent a native TTS inference call from trapping a Voice Studio worker for
hours, and give Studio Hub a service-aware last-resort recovery path that never
turns an accepted or uncertain job into an automatic duplicate.

The Voice Studio task must land first because it owns model execution and the
terminal job contract. Studio Hub may then consume that contract and coordinate
safe recovery. The recently added OmniVoice 288-character sections already
permit cancellation *between* sections; they do not interrupt a native call
that hangs *inside* one section.

## Required reading

- `.claude-work/README.md`
- Both task files in this batch
- `CONTROLLER_ARCHITECTURE.md`
- `studiohub_genstudio_integration.md`
- Voice Studio `AGENTS.md` and generation/job contracts

## Architectural invariants

- GenStudio → Studio Hub controller → Voice Studio worker.
- Voice Studio owns synthesis execution, chunking, joining, cancellation,
  partial-artifact cleanup, and worker-side terminal evidence.
- Studio Hub owns draining, scheduling, service identity, recovery coordination,
  and reconciliation; it does not manipulate text or audio.
- Cancellation and restart never imply that accepted work is safe to resubmit.
- An uncertain accepted job is reconciled by durable identity and polling, not
  retried automatically.
- No active job is restarted unless the explicit recovery policy and controller
  gate authorize it.

## Task graph

| Task | Repository | Depends on | Execution | Outcome |
|---|---|---|---|---|
| TASK-001 | `/Users/thengmacmini/pinokio/api/voicestudio-mac.git` | none | sequential | Preemptible native voice inference and truthful terminal states |
| TASK-002 | `/Users/thengmacmini/pinokio/api/studiohub-mac` | TASK-001 | sequential | Service-aware drain, restart, and accepted-job reconciliation |

## Batch-wide permissions

Implementation is intentionally deferred. Local commits may be prepared in
isolated worktrees, but push, deployment, service restart, live fleet changes,
credentials, production data, migrations, and provider calls remain denied
until a controller explicitly opens the corresponding gate.

## Shared verification

The integrated verification must prove cancellation during an intentionally
hung fake native call, safe service recovery, durable job reconciliation after
worker restart, no duplicate submission, and cleanup of partial artifacts.

## Batch completion criteria

- [ ] Voice Studio can terminate a hung section within a bounded deadline.
- [ ] Cancellation is distinct from failure and uncertain acceptance.
- [ ] Studio Hub identifies and controls the exact managed Voice Studio service.
- [ ] Recovery drains normal work and requires explicit escalation for a stuck job.
- [ ] Accepted work is never automatically resubmitted after cancellation or restart.
- [ ] Fake-worker integration tests cover recovery and rollback.
- [ ] Controller reviews both releases before rollout.

## State history

- `2026-08-03T14:29:50Z` — queued by Codex from owner annotation; implementation deferred until a safe work window.
