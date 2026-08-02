---
kind: studiohub.claude-batch-report
schema_version: 1
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
batch_state: completed
started_at: 2026-08-02T17:11:37Z
finished_at: 2026-08-02T17:25:31Z
---

# Batch outcome

**The batch achieved its intended outcome, with one significant blocker.**

A source-backed, model-by-model Group B evidence matrix now exists for all
**16 exact checkpoints across the six requested families**; Studio Hub's ability
to ingest audited candidates has been assessed against current source; and the
combined findings are converted into six independently committable future batch
proposals. This was evidence and planning work only, and it stayed that way: no
source change, commit, push, provider call, download, generation, restart,
deployment, credential access, or live-state change occurred.

**The blocker:** the isolated-worktree pattern mandated by
`.claude-work/README.md` is incompatible with the verification suites of **both**
repositories. TASK-001 and TASK-002 are `blocked` on that basis. Their
substantive deliverables are complete; only their verification gates failed.

**Headline technical finding:** the bottleneck is **not** Studio Hub. The Hub can
already ingest the entire Voice Studio Group B audit contract with no schema
change. What is missing is upstream — **0 of 16 Group B rows have a durable audit
record**, so every one runs on unaudited developer defaults and none is exposable.

## Task results

| Task | Repository | State | Commit | Verification | Report |
|---|---|---|---|---|---|
| TASK-001 | `voicestudio-mac.git` | **blocked** | none | **failed** (suite aborted at collection; 77 passed in a supplementary run) | `reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001.md` |
| TASK-002 | `studiohub-mac` | **blocked** | none | **failed** (0 of 4 files executed) | `reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002.md` |
| TASK-003 | `studiohub-mac` | **completed** | none | **passed** (4/4) | `reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-003.md` |

Task files: TASK-001 and TASK-002 in `blocked/`; TASK-003 and the batch
definition in `completed/`. `queue/` and `active/` are empty. Each task moved
`queue/` → `active/` → terminal with a timestamped history entry and a stable
filename. The batch file moved directly `queue/` → `completed/` without an
`active/` stage — recorded honestly in its own history.

## Dependency execution

The declared graph was followed exactly.

- **Phase 1 (parallel, group `evidence-audit`):** TASK-001 and TASK-002 ran in
  disjoint pinned worktrees across two different repositories. Neither depends
  on the other.
- **Phase 2 (sequential, `integration-plan`):** TASK-003 started only after both
  dependency reports existed. Both dependencies were `blocked` — the batch
  explicitly authorizes this, and TASK-003's own safe-stop condition requires
  using bounded findings and carrying the blocker into the plan. It did exactly
  that and invented no missing evidence.
- **Blocked work did not stop independent work.** TASK-001 blocking did not
  prevent TASK-002, and neither prevented TASK-003. All three reached a terminal
  state in one pass.

## Combined changes

**No repository source file was changed anywhere.**

- `voicestudio-mac.git` — zero changes. `git diff HEAD` empty in the pinned
  worktree; `git diff --check` exit 0. The primary checkout's unrelated dirty
  files (`app/backend/cache.py`, `app/backend/downloads.py`) were verified
  present before and after and were never opened; the pinned base excludes them.
- `studiohub-mac` — zero application/contract changes. The controller checkout
  contains only `.claude-work/**` state moves and the four reports.

Three task branches exist and are **unmerged**:
`claude/group-b-triage-voice-evidence`, `claude/group-b-triage-hub-readiness`,
`claude/group-b-triage-integration-plan` — each at its pinned base commit with no
commits added. Nothing has been merged anywhere.

## Contract and rollout summary

- **Contract versions changed:** none.
- **Producer changes:** none.
- **Consumer changes:** none.
- **Backward compatibility:** not affected — nothing shipped.
- **Required Studio Hub rollout order:** none from this batch.
- **Required sibling rollout order:** none from this batch.
- **Recovery and rollback:** not applicable. To discard entirely: remove the
  three worktrees and delete the three unmerged branches.

TASK-002 found `CAPABILITY_CONTRACT.md` (schema_version 2) already names every
field Group B needs. The only candidate contract change is the optional
enumeration-truncation signal (GAP-1), which would be additive and
backward-compatible and must go through a dedicated versioned contract task.

## Verification summary

| Task | Commands | Result |
|---|---|---|
| TASK-001 | 4 (git status ×2, pytest, git diff --check) | 3 passed, **1 failed** |
| TASK-002 | 4 (git status ×2, pytest, git diff --check) | 3 passed, **1 failed** |
| TASK-003 | 4 (test -s ×2, rg sweep, git diff --check) | **4 passed** |
| Batch-level shared | 4 (test -s ×3, rg family sweep) | **4 passed** — 57 matching lines; all six families present across the reports |

### The single root cause

Every verification failure in this batch reduces to one identical error:

```
UpdateError: Updater root must be a real Git checkout.
```

raised during auto-updater spec validation at import of `backend.main`.

- **TASK-001:** 3 test files failed collection, aborting the session; a
  supplementary run of the 4 collectible files gave **77 passed, 3 failed**,
  those 3 failing with the same error. **Zero genuine assertion failures.**
- **TASK-002:** worse — `test_model_baselines.py` failed collection and the other
  three errored at *fixture* setup. **No test executed at all.**

Supporting evidence gathered inside the allowlists: in both repositories the
linked worktree's `.git` is a plain ASCII **gitfile**, whereas each primary
checkout's `.git` is a directory. The validation appears to reject the gitfile
form. This remains a **hypothesis** — `app/backend/auto_update.py` is outside
both tasks' `allowed_paths` and was deliberately not opened in either repository.

This is an orchestration-harness defect, not a Group B defect, and it will block
**every** future batch in this queue that uses the mandated worktree pattern and
touches `backend.main`.

## Permission audit

**No permission was exercised. Every operational action in the batch policy was
`denied` and none was performed.**

| Action | Authorized | Performed | Controller review |
|---|---|---|---|
| Local commit | no | **no** | Required before any future implementation |
| Push | no | **no** | Always required |
| Paid or free-tier provider call | no | **no** | Always required |
| Model download or generation | no | **no** | Always required |
| Production data read/write | no | **no** | Always required |
| Credential read/write | no | **no** | Always required |
| Migration create/apply | no | **no** | Always required |
| Service restart | no | **no** | Always required |
| Deployment | no | **no** | Always required |
| Live configuration / fleet state | no | **no** | Always required |

Zero network access of any kind occurred. All tests were local; no inference ran.
Forbidden paths were honoured in both repositories, including the two concurrent
Voice Studio files and both repos' `auto_update.py` — the latter withheld even
though it holds the answer to the batch's own blocker. No credential,
registration or fleet token, private endpoint, customer datum, or raw worker
error appears in any task file, command, or report.

## Unresolved risks and blockers

1. **Verification harness incompatible with mandated worktrees** *(affects
   TASK-001, TASK-002, and all future batches)*. Safe work still possible:
   source-derived analysis, planning, and any task whose verification avoids
   `backend.main`.
2. **TASK-002's conclusions have zero executed test backing.** They are
   source-derived; declared test coverage names strong guarantees but none was
   observed passing.
3. **Two licence blockers.** VoxCPM2 (×2 rows) has **no licence claim anywhere**
   in the catalog — the only Group B family with none. Fish S2 Pro is
   research/non-commercial on the public licence.
4. **VibeVoice phantom-voice risk** (×3 rows). The preset roster is a free-text
   field because it "isn't verifiable without the model on disk" — structurally
   the same defect class that previously shipped two phantom Qwen speakers.
5. **Zero measured RAM evidence** for any Group B row at any tier. Two rows also
   carry self-contradictory guidance (OmniVoice 8 vs 16 GB; VoxCPM2-bf16 12 vs
   16 GB).
6. **Silent enumeration truncation** at 100 items in the Hub relay (GAP-1) —
   invisible to consumers, and Group B includes a 646-language claim.
7. **Language placeholders leak today** (GAP-2). Voice Studio emits literal
   `'+16 more'` / `'+636 more'` strings inside a typed `languages` field; the Hub
   relays them unvalidated on the unaudited path — where all 16 rows currently
   sit. Landing audit records cures this automatically.

## Exact controller decisions needed

1. **Verification-environment decision — blocking, affects TASK-001 and
   TASK-002.** Choose: (a) extend a follow-up task's `allowed_paths` to include
   `app/backend/auto_update.py` and `auto_update_config.py` in both repositories
   so the incompatibility can be confirmed and fixed; (b) authorize running these
   suites in a non-worktree checkout, accepting reduced isolation; or (c) accept
   the supplementary results as sufficient for triage and defer. Until decided,
   no task in this queue can satisfy a `backend.main`-dependent gate.
2. **Approve BATCH-A (harness repair) first** — every other proposed batch
   inherits the broken gate.
3. **Owner — Fish Audio commercial licence:** pursue a grant, keep internal-only,
   or drop. Gates the most expensive candidate (24 GB floor, ~134 sections).
4. **Owner — VoxCPM2 licence:** source and record it; no claim exists today.
   Both VoxCPM2 rows are commercially evidence-blocked until then.
5. **Approve BATCH-C wave 1 scope** — rows #6 (`Qwen3-0.6B-CustomVoice`),
   #5 (`Qwen3-0.6B-Base`), #1 (`chatterbox-4bit`) as the pilot audit wave.
6. **GAP-1 disposition** — signal enumeration truncation (additive contract
   change, needs a versioned contract task) or accept as documented limitation.
7. **GAP-2 ownership** — confirm the primary fix is Voice Studio's, and whether
   defensive Hub-side validation is also wanted.
8. **Confirm Fish bf16 is deferred** as out-of-tier (32 GB) context only.
9. **Confirm no Studio Hub contract work is scheduled for Group B ingestion** —
   TASK-002 found none necessary; confirming prevents a speculative batch.
10. **Confirm the `'+N more'` placeholders are treated as a Voice Studio data
    defect** to fix in BATCH-B, not silently reinterpreted.

## Recommended integration order

Nothing in this batch is mergeable — there are no commits. The recommended
*review* order is:

1. **Reports first, in dependency order:** TASK-001 → TASK-002 → TASK-003. The
   plan is only as good as the two bounded evidence reports beneath it.
2. **Resolve decision 1**, then create and review **BATCH-A** as two
   repository-separated tasks (Voice Studio, Studio Hub). This is the only work
   that should start immediately.
3. **Then the Voice Studio lane:** BATCH-B (data-truth repair) → BATCH-C wave 1
   (durable audit records). **BATCH-D** (Hub enumeration hardening) may run in
   parallel — different repository, disjoint paths — and is optional.
4. **Only after** BATCH-C wave 1 exists and the owner has recorded machine
   availability and both licence decisions: **BATCH-E** (live 8/16/24 GB
   qualification), then **BATCH-F** (samples and commercial approval review).
5. Delete the three unmerged task branches and their worktrees whenever the
   controller has finished inspecting them.

Do not merge, push, deploy, restart, or change live state from this report.
Every approval, price, publication, and routing decision remains GenStudio's and
the human owner's.
