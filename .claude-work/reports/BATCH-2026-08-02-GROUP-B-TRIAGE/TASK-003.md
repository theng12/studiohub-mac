---
kind: studiohub.claude-task-report
schema_version: 1
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
task_id: TASK-003
task_state: completed
repository_root: /Users/thengmacmini/pinokio/api/studiohub-mac
worktree_path: /Users/thengmacmini/pinokio/.claude-worktrees/studiohub-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-003
branch: claude/group-b-triage-integration-plan
base_commit: eb1c1ce6035858eba4d901b86b434e44eeda7479
head_commit: eb1c1ce6035858eba4d901b86b434e44eeda7479
started_at: 2026-08-02T17:22:12Z
finished_at: 2026-08-02T17:24:14Z
---

# Outcome

**completed** — both dependency reports were synthesised into an exact
per-checkpoint decision matrix and a sequence of independently committable
future batch proposals. Both dependencies are `blocked`; per the task's safe
stop condition their bounded findings are used as-is and their blocker is
carried explicitly into the plan. No evidence was invented.

No queue file for a future batch was created (explicitly out of scope). No
source, contract, test, or release file was touched.

## Changes made

- `.claude-work/active/…TASK-003.md` — state move from `queue/`.
- `.claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-003.md` — this report.
- `.claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/BATCH_REPORT.md` — final
  batch report, written immediately after this one.

Only `allowed_paths` were read: the two dependency reports and the orchestration
templates. `app/**` and all three contract documents are in `forbidden_paths`
for this task and were **not** opened — every technical claim below is carried
forward from the dependency reports' citations, not re-derived.

## Git evidence

- Starting status: clean worktree on `claude/group-b-triage-integration-plan`.
- Ending status: unchanged; no file in the worktree modified.
- Commit created: no (`local_commit: denied`). Push: no.

## Verification

| Command | Result | Notes |
|---|---|---|
| `test -s …/TASK-001.md` | passed | 22,158 bytes. |
| `test -s …/TASK-002.md` | passed | 16,427 bytes. |
| `rg -n "Chatterbox\|OmniVoice\|Qwen3-TTS\|VoxCPM2\|VibeVoice\|Fish Audio" …` | passed | 38 matching lines; all six families present. |
| `git diff --check -- .claude-work` | passed | Exit 0. |

## Reconciliation of the two reports

**No contradictions.** The reports are complementary and converge on one
conclusion, reinforcing each other at three points:

1. **The bottleneck is Voice Studio audit records, not Hub capability.**
   TASK-001: 0/16 rows have a durable audit record. TASK-002: the Hub can
   already ingest the full audit contract with no schema change. The missing
   artifact is upstream.
2. **The `'+N more'` placeholder is a single defect with a cross-repo path.**
   TASK-001 found Voice Studio emitting it in a typed `languages` field;
   TASK-002 found the Hub relaying it unvalidated on the unaudited path — and
   that landing an audit record cures it automatically, because the candidate's
   `controls` then replace the derived ones. One fix, one owner (Voice Studio),
   with optional Hub hardening.
3. **The verification blocker is systemic, not local.** Both repositories fail
   identically in a linked worktree. TASK-001 kept 77 passing tests; TASK-002
   lost all four files. This is an orchestration-harness defect affecting every
   future batch in this queue.

## Exact decision matrix

One row per checkpoint **and operation**. Identity, revision, and contract-hash
distinctions are preserved; no family is collapsed.

Status vocabulary: **IU** implemented but unaudited · **AR** audit-ready offline ·
**RR** requires adapter/dependency repair · **LQ** requires live 8/16/24 GB
qualification · **LB** license/commercial blocked · **IO** internal-only ·
**PS** potentially sellable pending owner approval.

| # | Checkpoint | Operation | Status | Missing evidence | Owner | Tier | Controller decision |
|---|---|---|---|---|---|---|---|
| 1 | `chatterbox-4bit` | clone | IU · AR · LQ · PS | Audit record; measured RAM; language enum (`+16 more`); long-form endurance | Voice Studio | 8 GB | Approve as first audit wave |
| 2 | `chatterbox-8bit` | clone | IU · AR · LQ · PS | As #1, plus no revision test | Voice Studio | 8 GB | Approve; lower priority than #1 |
| 3 | `chatterbox-turbo-4bit` | clone | IU · AR · LQ · PS | As #2; distinct 400-char budget, English-only | Voice Studio | 8 GB | Confirm Turbo is a separate product identity |
| 4 | `OmniVoice-bfloat16` | clone + design | IU · AR · LQ · PS | Audit; **RAM claim self-contradictory (8 vs 16 GB)**; 646-language claim unverifiable; no revision test | Voice Studio | 8 + 16 GB | Resolve the RAM contradiction before qualification |
| 5 | `Qwen3-TTS-0.6B-Base-8bit` | clone | IU · AR · LQ · PS | Audit; measured RAM; long-form endurance | Voice Studio | 8 GB | Approve as first audit wave |
| 6 | `Qwen3-TTS-0.6B-CustomVoice-8bit` | preset (9 speakers) | IU · **AR (strongest)** · LQ · PS | Audit; measured RAM. Roster provenance already strong | Voice Studio | 8 GB | **Recommended pilot** — cheapest credible first audit |
| 7 | `Qwen3-TTS-1.7B-Base-8bit` | clone | IU · AR · LQ · PS | Audit; measured RAM; no revision test | Voice Studio | 16 GB | Approve after #5 proves the pattern |
| 8 | `Qwen3-TTS-1.7B-CustomVoice-8bit` | preset (9 speakers) | IU · AR · LQ · PS | As #7 | Voice Studio | 16 GB | Approve after #6 |
| 9 | `Qwen3-TTS-1.7B-VoiceDesign-8bit` | voice design | IU · AR · LQ · PS | Audit; **no design-prompt limits audited**; no dedicated test | Voice Studio | 16 GB | Decide whether design ships in wave 1 |
| 10 | `VoxCPM2-4bit` | clone/design/zero-shot | IU · AR · LQ · **LB** | Audit; measured RAM; **no license claim exists at all** | Voice Studio + owner | 8 GB | **Owner must source the license** |
| 11 | `VoxCPM2-bf16` | clone/design/zero-shot | IU · AR · LQ · **LB** | As #10; RAM guidance inconsistent (12 vs 16 GB); no revision test | Voice Studio + owner | 16 GB | As #10 |
| 12 | `VibeVoice-Realtime-0.5B-4bit` | preset + streaming | IU · **RR** · LQ · PS | **Voice roster not enumerable** — free-text field, phantom-voice risk; no revision test | Voice Studio | 8 GB | Approve roster enumeration as repair work first |
| 13 | `VibeVoice-Realtime-0.5B-8bit` | preset + streaming | IU · RR · LQ · PS | As #12 | Voice Studio | 8 GB | Follows #12 |
| 14 | `VibeVoice-Realtime-0.5B-fp16` | preset + streaming | IU · RR · LQ · PS | As #12 | Voice Studio | 8/16 GB | Follows #12 |
| 15 | `fish-audio-s2-pro-8bit` | clone + style | IU · AR · LQ · **LB** | Audit; **24 GB is an unmeasured catalog claim**; 134 sections at 40k chars | Voice Studio + owner | 24 GB | **Owner decides commercial licence before spending qualification time** |
| 16 | `fish-audio-s2-pro-bf16` | clone + style | IU · LB · **out of fleet tiers** | 32 GB exceeds 8/16/24 GB tiers | Owner | 32 GB | Recommend deferring as adjacent context only |

### Aggregate

- **16/16 implemented but unaudited.** None is currently exposable.
- **13/16 potentially sellable** pending audit + owner approval.
- **3 rows license-blocked**: VoxCPM2 ×2 (no claim at all), Fish 8-bit
  (research/non-commercial). Fish bf16 is additionally out of tier.
- **3 rows need repair before qualification**: VibeVoice ×3 (voice roster).
- **0 rows have measured RAM evidence** at any tier.

## Recommended future batches

Repository-separated, disjoint paths, independently committable. **Proposals
only — no queue file was created.**

### BATCH-A — Orchestration harness repair *(blocks everything; do first)*

- **Repository:** both (separate tasks). **Depends on:** nothing.
- **Problem:** the mandated isolated worktree cannot run either repo's tests.
- **Allowed paths:** `app/backend/auto_update.py`,
  `app/backend/auto_update_config.py`, their tests (per repo).
- **Forbidden:** everything else.
- **Permissions:** local commit `allowed` (gated); push/deploy/restart `denied`.
- **Verification:** each dependency task's original suite, from a linked worktree.
- **Stop condition:** stop if the fix would weaken the real-checkout guarantee
  outside test contexts.
- **Rollout:** none — developer-tooling only.

### BATCH-B — Voice Studio data-truth repair

- **Repository:** `voicestudio-mac.git`. **Depends on:** BATCH-A.
- **Scope:** (1) remove `'+N more'` placeholders from the typed `languages`
  field and enumerate or explicitly bound the real set; (2) enumerate the
  VibeVoice preset roster from the checkpoint, closing the phantom-voice risk;
  (3) resolve the OmniVoice 8-vs-16 GB and VoxCPM2 12-vs-16 GB contradictions.
- **Allowed paths:** `app/backend/catalog.py`, `app/backend/generation.py`,
  focused tests. **Forbidden:** `app/backend/cache.py`,
  `app/backend/downloads.py` (concurrent work), `model-audits/**`.
- **Permissions:** local commit `allowed` (gated); provider calls `denied`.
- **Note:** roster enumeration may require the checkpoint on disk — if so it
  splits into an offline-inspection task, **not** a provider call.

### BATCH-C — Voice Studio durable audit records *(the critical path)*

- **Repository:** `voicestudio-mac.git`. **Depends on:** BATCH-A, BATCH-B.
- **Scope:** produce Group A-quality `model-audits/**` records per checkpoint and
  operation, following the Kokoro exemplar (hash-bound contract, enumerated
  controls, `license_sources`, measured `hardware`).
- **Wave 1 (recommended):** rows #6, #5, #1 — cheapest 8 GB, strongest existing
  provenance. **Wave 2:** #2, #3, #4, #7, #8, #9. **Wave 3:** #12–14 after
  BATCH-B. **Deferred:** #10, #11, #15 pending licence; #16 out of tier.
- **Allowed paths:** `model-audits/**`, `app/backend/model_audits.py`, its tests.
- **Permissions:** local commit `allowed` (gated). Model execution is **not**
  authorized here — this batch writes records from BATCH-E measurements.
- **Curative effect:** landing these records also fixes the Hub-side language
  relay, because the candidate's `controls` then replace the derived ones.

### BATCH-D — Studio Hub enumeration-capacity hardening *(optional)*

- **Repository:** `studiohub-mac`. **Depends on:** controller decision only.
- **Scope:** signal or raise the silent 100-item enumeration truncation (GAP-1);
  optional defensive language-code validation (GAP-2 secondary).
- **Allowed paths:** `app/backend/capabilities.py`, `app/backend/model_exposure.py`,
  `app/tests/test_capabilities.py`, `CAPABILITY_CONTRACT.md` *only if* the
  controller approves a versioned contract change.
- **Rollout:** additive and backward-compatible; needs a `schema_version`
  decision, fake-worker tests, and a rollback plan **before** any sibling relies
  on it. Runs in parallel with BATCH-B/C — disjoint repositories.

### BATCH-E — Live qualification on 8/16/24 GB *(separate by design)*

- **Repository:** none (operational). **Depends on:** BATCH-B, BATCH-C wave 1.
- **Scope:** execute TASK-001's proposed matrix — short-form per language and
  voice, operation-specific paths, one ~40,000-character adapter-managed job per
  candidate, lifecycle (progress, cancellation, partial cleanup, deterministic
  retry, post-failure health), and resource telemetry at each tier.
- **Permissions:** model execution `allowed` **only** under an explicit owner
  authorization naming machines and models; provider calls `denied`; production
  data `denied`.
- **Preconditions (owner-recorded):** machines powered, reachable, fully updated,
  idle, and not downloading or maintaining models.
- **Kept separate** from BATCH-B/C so fleet idleness and power availability can
  never compromise source review.

### BATCH-F — Samples and commercial approval review

- **Depends on:** BATCH-C, BATCH-E, plus owner licence decisions.
- **Scope:** per-voice/per-language samples, then a deliberate GenStudio
  approval decision. **No approval, pricing, publication, or routing is decided
  by any worker.**

### Dependency and parallelism summary

```
BATCH-A ──┬── BATCH-B ── BATCH-C(w1) ── BATCH-E ── BATCH-F
          └── (Voice Studio lane)
BATCH-D ─────(Studio Hub lane, parallel, disjoint repo, optional)
```

Only BATCH-D may run parallel to the Voice Studio lane. BATCH-A blocks
everything that must pass a verification gate.

## Planning-requirement compliance

1. No unverified catalog claim was converted into fact — RAM, licence, language,
   and voice-roster claims stay labelled as claims throughout.
2. Exact checkpoint, operation, revision, and contract-hash identity preserved;
   Qwen's three operations and Chatterbox Turbo remain distinct rows.
3. All seven required per-model distinctions are applied in the matrix.
4. 40,000 characters is treated as an **adapter-managed public job requirement**;
   no row was judged ineligible for a low native one-pass window. Section counts
   (111–134) are planned as an integrity risk to test, not a disqualifier.
5. Later coverage includes every built-in voice and supported language, with
   English as default rather than the only language.
6. Clone testing is planned around an immutable original plus model-specific
   derived references. No destructive splitting, looping, stretching, or
   fabricated speech is proposed.
7. Source implementation (B, C, D) and live qualification (E) are separate
   batches.

## Architecture and contract review

- Controller/worker boundary preserved: yes. No model-specific preprocessing,
  chunking, synthesis, joining, or artifact validation is proposed for Studio
  Hub; BATCH-D touches only relay capacity.
- GenStudio ownership preserved: yes. Approval, products, prices, publication,
  customer assets, retention, and billing stay in GenStudio; BATCH-F ends at a
  *recommendation*.
- Candidate approval remains deliberate: yes. No proposed batch grants automatic
  approval, caching, routing, or customer visibility.
- Contract impact: only BATCH-D could change a shared contract, and only with an
  explicit schema version, producer/consumer list, rollout order, fake-worker
  tests, compatibility statement, and rollback plan in a dedicated contract task.

## Permissions exercised

| Action | Authorized | Performed | Evidence |
|---|---|---|---|
| Local commit | no | no | Worktree clean. |
| Push | no | no | None attempted. |
| Paid provider call | no | no | Zero; no network access. |
| Production data | no | no | Only the two reports and templates read. |
| Credential access/change | no | no | Zero. |
| Migration create/apply | no | no | Zero. |
| Restart/deploy/live change | no | no | Zero. |

## Risks and limitations

1. The plan rests on two `blocked` reports; TASK-002's conclusions have **zero
   executed test backing**. BATCH-A must land before any conclusion here is
   treated as verified.
2. Roster and language enumeration (BATCH-B) may require checkpoints on disk;
   if so, scope splits and the estimate grows.
3. BATCH-E is the only expensive batch and depends on owner scheduling.
4. Three rows may never become sellable if licence evidence does not materialise.

## Controller decisions required

1. **Approve BATCH-A first.** Every other batch inherits the broken gate.
2. **Owner: Fish Audio commercial licence** — pursue, internal-only, or drop.
3. **Owner: VoxCPM2 licence** — source and record it; no claim exists today.
4. **Approve BATCH-C wave 1 scope** (rows #6, #5, #1) as the pilot.
5. **Decide GAP-1 disposition** (BATCH-D optional, and whether it changes a
   versioned contract).
6. **Confirm Fish bf16 (#16) is deferred** as out-of-tier context.
7. **Confirm no Studio Hub contract work is scheduled for Group B ingestion** —
   TASK-002 found none necessary.

## Suggested next action

Create and review BATCH-A as two repository-separated tasks. Do not schedule
BATCH-E until BATCH-C wave 1 exists and the owner has recorded machine
availability and the two licence decisions.
