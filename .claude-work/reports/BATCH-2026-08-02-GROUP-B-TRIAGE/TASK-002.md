---
kind: studiohub.claude-task-report
schema_version: 1
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
task_id: TASK-002
task_state: blocked
repository_root: /Users/thengmacmini/pinokio/api/studiohub-mac
worktree_path: /Users/thengmacmini/pinokio/.claude-worktrees/studiohub-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002
branch: claude/group-b-triage-hub-readiness
base_commit: eb1c1ce6035858eba4d901b86b434e44eeda7479
head_commit: eb1c1ce6035858eba4d901b86b434e44eeda7479
started_at: 2026-08-02T17:11:37Z
finished_at: 2026-08-02T17:22:12Z
---

# Outcome

**blocked** — the readiness assessment is complete and delivered below, but the
task's mandated verification suite could not execute in the pinned isolated
worktree. **Zero of the four required test files ran.** The root cause is
identical to TASK-001's and is now confirmed to be **systemic across both
repositories**, not a Voice Studio quirk.

The substantive finding is strongly positive: Studio Hub needs **no shared
contract extension** to ingest audited Group B candidates. Two bounded,
concrete gaps were found, both narrow.

No Studio Hub source or contract file was changed, and no commit was created.

## Changes made

No Studio Hub source, contract, or test file was created, modified, or deleted.
`git diff HEAD` in the pinned worktree is empty; `git diff --check` exits 0.

The only mutations are inside the `orchestration_root`:

- `.claude-work/active/…TASK-002.md` — state move from `queue/`.
- `.claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-002.md` — this report.

Every path read was inside `allowed_paths`. `app/backend/migrations/**`,
`data/**`, `logs/**`, `credentials/**`, `ENVIRONMENT`, and both sibling
repositories were never opened. `app/backend/auto_update.py` holds the root
cause of the verification failure and is **not** in `allowed_paths`; it was
deliberately not opened.

## Git evidence

- Starting status: `## claude/group-b-triage-hub-readiness` — clean.
- Ending status: `## claude/group-b-triage-hub-readiness` — clean.
- Commit created: no (`local_commit: denied`).
- Push performed: no (`push: denied`).
- Controller checkout: contains only `.claude-work/**` state moves and reports.
  No application file is modified.

## Verification

| Command | Result | Notes |
|---|---|---|
| `git status --short --branch` (opening) | passed | Clean worktree on task branch. |
| `conda_env/bin/pytest -q` over the 4 required test files | **failed** | `Interrupted: 1 error during collection`. Zero tests executed. |
| `git diff --check` | passed | Exit 0. |
| `git status --short --branch` (closing) | passed | Clean; no source change. |

### Exact sanitized failure

`test_model_baselines.py` fails collection with
`UpdateError: Updater root must be a real Git checkout.` raised from
`app/backend/auto_update.py:133`, aborting the whole session.

A supplementary run of the other three files showed the failure is **total, not
partial**: `test_model_exposure.py`, `test_capabilities.py`, and
`test_monitor.py` each error at *fixture setup* with the same `UpdateError`
(20 occurrences in `test_monitor.py` alone). **No test in this task's required
set can execute inside a linked worktree.**

This is worse than TASK-001, where 77 tests still ran. Combined with TASK-001,
the conclusion is unambiguous: **the isolated-worktree pattern mandated by
`.claude-work/README.md` is incompatible with the verification suites of both
Studio Hub and Voice Studio.** Confirmed evidence: in both repositories the
worktree `.git` is a plain ASCII gitfile rather than a directory.

Because no test ran, every conclusion below is **source-derived**. Where a test
name proves a behavior, it is cited as *declared coverage* — the test exists and
names the guarantee, but it did not execute in this run.

## Readiness matrix

Legend: **SUPPORTED** (in source now) · **TEST-ONLY** (works; needs a test) ·
**EXTENSION** (contract change needed) · **OWNED-ELSEWHERE**.

### Candidate identity and approval

| Requirement | Status | Evidence |
|---|---|---|
| Exact identity by model + operation + revision + contract hash | **SUPPORTED** | `exposure_key()` = SHA-256 over the 4-tuple (`model_exposure.py:175`). |
| Revision/contract change returns to review automatically | **SUPPORTED** | `state_for()` returns `suspended` / `runtime_revision_or_contract_changed` when a prior approval exists under a different key (`:283-291`). |
| Candidate requires deliberate sibling audit | **SUPPORTED** | `state_for()` gate order: `unverified`(missing/invalid) → `blocked`(`audit_not_passed`) → `blocked`(`sibling_candidate_not_approved`) → `blocked`(`operation_not_audited`) → `candidate`/`awaiting_hub_approval`. Declared coverage: `test_candidate_requires_a_passed_deliberate_sibling_audit`. |
| Candidate never auto-approves/caches/routes | **SUPPORTED** | Terminal unapproved state is `candidate`; `is_approved()` requires literal `approved`. Declared coverage: `test_candidate_without_hub_approval_is_not_advertised`, `test_unapproved_or_revoked_model_is_not_advertised`. |
| Strict validation of sibling evidence | **SUPPORTED** | `candidate_summary()` enforces schema + version, 40–64 hex revision, `sha256:`+64hex contract hash, non-empty operations list; anything invalid returns `None` and stays out of the workflow. |

### GenStudio desired state

| Requirement | Status | Evidence |
|---|---|---|
| Global desired state cannot be silently overridden at a site | **SUPPORTED** | `sync_global_catalog()` stamps `authority: "genstudio"` + `authority_revision`; `global_authority_active()` gates site action. Declared coverage: `test_site_owner_cannot_override_global_catalog`. |
| Tampered/invalid exact key rejected | **SUPPORTED** | Each `candidate_key` is recomputed and compared; mismatch raises `ValueError` (`:221`). Declared coverage: `test_invalid_or_tampered_exact_contract_is_rejected`. |
| Removal stops targeting without deleting evidence | **SUPPORTED** | Omitted contracts transition `approved → revoked` with reason; the record and cached/partial files are preserved. Declared coverage: `test_revocation_stops_targeting_without_deleting_cached_or_partial_files`, `test_global_catalog_replaces_site_approval_without_deleting_history`. |
| Revocation possible during worker outage | **SUPPORTED** | `revoke_key()` acts on the immutable exposure key even if the worker vanished (`:344`). |
| Last-good survives restart | **SUPPORTED** | Atomic durable write: `O_EXCL` temp → `fsync` → `os.replace` → `chmod 0600`. Declared coverage: `test_last_good_catalog_survives_controller_restart`, `test_persisted_last_good_catalog_survives_monitor_restart`. |

### Catalog refresh, capability read, supply

| Requirement | Status | Evidence |
|---|---|---|
| Capability GET is cache-only, never contacts workers | **SUPPORTED** | Declared coverage: `test_models_read_is_cache_only`, `test_capability_snapshot_uses_stale_caches_without_worker_network`, `test_aggregate_skips_down_studios_no_network`. |
| Refresh bounded and non-overlapping | **SUPPORTED** | Declared coverage: `test_overlapping_catalog_refresh_is_rejected`. |
| Failed refresh retains last-good as stale evidence | **SUPPORTED** | Declared coverage: `test_failed_refresh_retains_last_good_catalog`, `test_provider_health_marks_cached_result_stale_after_failure`. |
| Machine states kept distinct with reasons and slots | **SUPPORTED** | Declared coverage: `test_supply_keeps_machine_states_hardware_and_reasons_distinct`, `test_busy_physical_machine_has_zero_available_capacity`, `test_pause_and_maintenance_are_reported_as_drain_without_mutating_work`. |
| Ineligible/unknown memory never downloaded | **SUPPORTED** | Declared coverage: `test_ineligible_and_unknown_memory_are_never_downloaded` — directly relevant to Group B, whose RAM figures are all unmeasured claims. |
| No credentials/ownership in capability payload | **SUPPORTED** | `_PRIVATE_KEYS` + `_PRIVATE_SUFFIXES` strip token/secret/password/credential/authorization/api_key/`*_path`/customer_content at every nesting level. Declared coverage: `test_capability_snapshot_never_exposes_content_credentials_or_ownership_ids`. |

### Voice-specific Group B fields — the headline result

**Studio Hub can already ingest the entire Voice Studio Group B audit contract
with no schema change.** Two independent mechanisms:

1. **Generic sanitized passthrough.** `candidate_summary()` preserves `adapter`,
   `controls`, `input_limits`, `output_limits`, `capacity`, `hardware` via
   `_safe_contract()` — arbitrary nested structure, bounded to depth 5, 100
   items, 500-char strings, 120-char keys.
2. **Candidate replaces derived fields.** In `capabilities.py:309-322`, when a
   candidate exists, `input_limits`, `output_limits`, `controls`, `adapter`, and
   `capacity` are taken **from the candidate**, not re-derived.

`CAPABILITY_CONTRACT.md` (schema_version 2) already names every field Group B
needs: `voice_modes` (`preset_voice`, `reference_audio_clone`, `voice_design`,
`provider_voice_id`); audit-bound `text_max_characters`, `long_form_strategy`,
`private_section_max_characters`, and a `reference_audio` object; and result
evidence `reference_source_sha256`, `reference_audio_sha256`,
`reference_preparation_revision`, `reference_duration_s`, `long_form_strategy`,
`chunk_total`. It also already specifies `chunk_index`/`chunk_total` progress
relay, cancellation forwarding, single-worker pinning for private references,
and stable staging error codes.

Cross-checked against TASK-001: Voice Studio's
`voicestudio_genstudio_integration.py` v1.1 exports 23 worker-owned fields.
**Every one has a defined home in the current Hub contract.** The two sides are
already aligned; the missing artifact is the Voice Studio audit record, not a
Hub contract.

### The two real gaps

**GAP-1 — Enumeration ceiling silently truncates (EXTENSION).**
Both `_safe_contract()` (`value[:100]`, `list(value.items())[:100]`) and
`_string_list(limit=100)` cap enumerations at 100 items with **no error, no
marker, and no truncation flag**. Kokoro fits comfortably (54 voices, 9
languages). Group B does not: OmniVoice claims 600+ languages, and any future
voice roster above 100 entries would be silently cut. A consumer cannot
distinguish "101 languages truncated to 100" from "exactly 100 languages". This
is a genuine contract-capacity gap for Group B specifically, and it silently
corrupts completeness evidence — precisely the evidence the owner wants.

**GAP-2 — `languages` is relayed without shape validation (cross-repository).**
On the **unaudited** path, `_controls()` sets `controls["languages"] =
_string_list(model.get("languages"))`, which coerces items to strings with no
language-code validation. Per TASK-001, Voice Studio ships literal placeholder
strings (`'+16 more'`, `'+636 more'`, `'+19 more'`, `'+70 more'`) inside that
typed field. Studio Hub would therefore faithfully advertise `'+636 more'` to
GenStudio **as if it were a language code**. Neither side validates.

Important nuance that shapes the fix: this leak exists **only** on the unaudited
path — which is exactly where all 16 Group B rows sit today (0 audit records).
Once a proper audit record lands, `capabilities.py:317` takes `controls` from the
candidate instead, and an audit-quality roster (like Kokoro's enumerated
`controls.language.values`) replaces the placeholder automatically. **Landing
durable Group B audit records cures GAP-2 as a side effect.** The correct
primary fix is in Voice Studio (stop emitting placeholders in a data field); a
defensive Hub-side validation is a secondary hardening, not the main remedy.

### Ownership boundaries (no duplicated authority)

| Concern | Owner |
|---|---|
| Reference preparation, chunking, joining, artifact validation, final speed | **Voice Studio** — Hub relays only |
| Audit evidence, contract hash, measured hardware | **Voice Studio** |
| Exact approval, last-good persistence, supply aggregation, cache-only reads, transport | **Studio Hub** |
| Global desired state, products, prices, publication, routing, customer assets, retention, billing | **GenStudio** |
| Customer voice upload ownership | **GenStudio** — never enters Hub's Shared Voices library |

No boundary violation was found. `CAPABILITY_CONTRACT.md` explicitly states the
Hub forwards reference bytes to one pinned Voice Studio and never manipulates
audio, matching TASK-001's finding that `worker_id` is deliberately absent from
the worker envelope because the Hub supplies it.

### Bounded Studio Hub backlog (proposals only — not implemented)

**Source changes**
1. Add explicit truncation signalling for enumerations exceeding the 100-item
   bound (e.g. `languages_truncated: true` + `languages_total`), or raise the
   bound for enumerated-evidence fields. *(GAP-1)*
2. Optional defensive validation rejecting non-conforming language codes on the
   unaudited path. *(GAP-2, secondary to the Voice Studio fix.)*

**Shared contract changes**
3. None required for Group B ingestion. Item 1 is additive and
   backward-compatible; if adopted it needs a `schema_version` decision and a
   versioned contract task.

**Test-only**
4. Fake-worker fixtures for a voice candidate exercising `reference_audio_clone`,
   `voice_design`, and `preset_voice` with audit-bound `input_limits`.
5. A regression test asserting an over-100 enumeration is either flagged or
   preserved.
6. A test asserting a malformed language entry never reaches the payload.

**UI/operator** — none identified.

## Architecture and contract review

- Controller/worker boundary preserved: yes — read-only; no worker contacted.
- GenStudio desired-state ownership preserved: yes — verified as a source
  property (`sync_global_catalog` authority stamping), not merely prose.
- Candidate approval remains deliberate: yes — the terminal unapproved state is
  `candidate`/`awaiting_hub_approval`; no path auto-promotes.
- Contract version or compatibility impact: **none from this task.**
  `CAPABILITY_CONTRACT.md` is schema_version 2 and already Group-B-capable.
- Required rollout order: none. Should proposal 1 be approved, it must ship as a
  versioned, backward-compatible additive change with fake-worker tests before
  any sibling relies on it.

## Permissions exercised

| Action | Authorized | Performed | Evidence |
|---|---|---|---|
| Local commit | no | no | Worktree clean; no commit object. |
| Push | no | no | No network git operation. |
| Paid provider call | no | no | Zero. No network access. |
| Production data | no | no | `data/**`, `logs/**` never read. |
| Credential access/change | no | no | Zero. `ENVIRONMENT`, `credentials/**` untouched. |
| Migration create/apply | no | no | Zero. `app/backend/migrations/**` never read. |
| Restart/deploy/live change | no | no | Zero. No live controller or fleet call. |

## Risks and limitations

1. **Zero executed test evidence.** Every conclusion is source-derived. Declared
   coverage names strong guarantees, but none was observed passing in this run.
2. The worktree incompatibility is systemic across both repositories and will
   block every future task in this queue that uses the mandated pattern.
3. GAP-1 is a silent-failure mode: truncation is invisible to consumers.
4. GAP-2 is live today for all 16 Group B rows.
5. The gitfile root cause is unconfirmed by design (allowlist boundary).

## Controller decisions required

1. **Verification-environment decision (blocking, shared with TASK-001).** Same
   three options; TASK-002 raises the stakes because *nothing* ran here. Until
   resolved, no Studio Hub task in this queue can satisfy a verification gate
   from an isolated worktree.
2. **GAP-1 disposition.** Decide whether enumeration truncation is signalled
   (additive contract change, needs a versioned contract task) or accepted as a
   documented limitation.
3. **GAP-2 ownership.** Confirm the primary fix belongs to Voice Studio, and
   whether a defensive Hub-side validation is also wanted.
4. **Confirm no Hub contract work is scheduled for Group B.** This task found
   none necessary; confirming prevents a speculative contract batch.

## Suggested next action

Let TASK-003 consume these findings and sequence Voice Studio audit-record work
**ahead of** any Studio Hub change, since landing Group B audit records both
unblocks exposure and cures GAP-2 without a contract change.
