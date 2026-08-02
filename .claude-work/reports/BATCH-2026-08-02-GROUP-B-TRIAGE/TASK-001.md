---
kind: studiohub.claude-task-report
schema_version: 1
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
task_id: TASK-001
task_state: blocked
repository_root: /Users/thengmacmini/pinokio/api/voicestudio-mac.git
worktree_path: /Users/thengmacmini/pinokio/.claude-worktrees/voicestudio-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001
branch: claude/group-b-triage-voice-evidence
base_commit: e5b764ac465f7e6cb75f03411bcd88e5afb7710d
head_commit: e5b764ac465f7e6cb75f03411bcd88e5afb7710d
started_at: 2026-08-02T17:11:37Z
finished_at: 2026-08-02T17:17:35Z
---

# Outcome

**blocked** — the complete Group B evidence matrix was produced and is delivered
below as bounded findings, but the task's mandated verification suite could not
be executed in the pinned isolated worktree. Three of seven required test files
never collected and three further tests failed, all from one identical root
cause. Confirming that root cause requires reading a file outside
`allowed_paths`, so the fix is a controller decision, not worker work.

No Voice Studio source file was changed, no commit was created, and no model,
provider, credential, or live machine was touched.

## Changes made

No Voice Studio file was created, modified, or deleted. `git diff HEAD` in the
pinned worktree is empty and `git diff --check` exits 0.

The only mutations are inside the Studio Hub `orchestration_root`:

- `.claude-work/active/BATCH-2026-08-02-GROUP-B-TRIAGE__TASK-001.md` — state
  move from `queue/`, `state:` field and timestamped history appended.
- `.claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001.md` — this report.

Every path read was inside `allowed_paths`. `app/backend/cache.py` and
`app/backend/downloads.py` were never opened. `app/backend/auto_update.py` and
`app/backend/auto_update_config.py` are **not** in `allowed_paths` and were
deliberately not opened, even though they hold the root cause of the
verification failure.

## Git evidence

- Starting status: `## claude/group-b-triage-voice-evidence` — clean.
- Ending status: `## claude/group-b-triage-voice-evidence` — clean.
- Commit created: no (`local_commit: denied`).
- Push performed: no (`push: denied`).
- Primary Voice Studio checkout: the two unrelated dirty files
  (`app/backend/cache.py`, `app/backend/downloads.py`) were present before work
  and remain present, unmodified, afterwards. The pinned base commit
  `e5b764ac…` excludes those working-tree edits, so the worktree never saw them.

## Verification

| Command | Result | Notes |
|---|---|---|
| `git status --short --branch` (opening) | passed | Clean worktree on the task branch. |
| `conda_env/bin/pytest -q` over the 7 required test files | **failed** | Session aborted: `Interrupted: 3 errors during collection`. Zero tests executed. |
| `git diff --check` | passed | Exit 0, no whitespace or conflict damage. |
| `git status --short --branch` (closing) | passed | Clean; no source file changed. |

### Exact sanitized failure

All six symptoms reduce to **one** root cause. At import time
`app/backend/main.py:300` constructs the auto-updater, whose spec validation
raises `UpdateError: Updater root must be a real Git checkout.`

- **Collection errors (3 files, never ran):** `test_model_audit_contract.py`,
  `test_generation_artifact_contract.py`, `test_model_storage.py` — each imports
  `backend.main`.
- **Test failures (3 tests):** `test_bark_api_preserves_native_controls`,
  `test_voxcpm_api_preserves_advanced_controls`,
  `test_tts_request_has_no_default_generated_audio_duration_cap` — each builds
  the API client, importing `backend.main`.

There are **zero genuine assertion failures**. Every failure is the same
environment rejection.

Supporting evidence gathered **within** `allowed_paths`: in a linked Git
worktree `.git` is a plain ASCII *gitfile*, whereas in the primary checkout
`.git` is a directory. The updater's "real Git checkout" validation appears to
reject the gitfile form. This is a hypothesis from filesystem shape and the
traceback only — it is **not confirmed**, because confirming it requires reading
`app/backend/auto_update.py`, which is outside this task's allowlist.

### Supplementary run (not a substitute for the required command)

To size the blast radius without weakening the mandated command, the four
collectible files were run separately: **77 passed, 3 failed**, the 3 failures
being the same `UpdateError`. So the Group B evidence below rests on 77 passing
local tests; the unavailable coverage is specifically the audit-contract,
artifact-contract, and model-storage assertions.

## Group B evidence matrix

Six families, **16 exact catalog rows**, enumerated programmatically from
`app/backend/catalog.py` (32 rows total). Variants are **not** collapsed.

### Evidence-class legend

- **CLAIM** — catalog/changelog prose only. Not qualification evidence.
- **IMPL** — implementation exists in adapter source.
- **TEST** — asserted by a local test that passed in this run.
- **AUDIT** — durable `model-audits/**` record, hash-bound.
- **GAP** — no evidence; requires later offline or live work.

### Finding 0 — the dominant, batch-defining fact

**0 of 16 Group B rows have a durable audit record.** Verified by calling
`model_audits.audit_record()` for every row: all return `None`, and
`model_audits.input_limits()` returns `{}` for all 16. The entire
`model-audits/` tree contains exactly three Group A records
(`Kokoro-82M-bf16`, `whisper-large-v3-turbo`, `whisper-tiny`).

Consequence in source: `generation.py:844 _internal_mlx_text_chunks()` reads
`private_section_max_characters` from the audit and, finding none, falls back to
the hard-coded family constant for **every** Group B model. Group B therefore
runs entirely on unaudited developer defaults.

### Per-family matrix

#### Chatterbox — 3 rows, family `chatterbox-mlx`

| Row | RAM claim | Caps | Languages | Section budget |
|---|---|---|---|---|
| `mlx-community/chatterbox-4bit` (recommended) | 8 GB | tts, clone, expressive, multilingual | 8 listed, incl. literal `'+16 more'` | 500 chars |
| `mlx-community/chatterbox-8bit` | 8 GB | tts, clone, expressive, multilingual | 8 listed, incl. `'+16 more'` | 500 chars |
| `mlx-community/chatterbox-turbo-4bit` | 8 GB | tts, clone, expressive (**no** multilingual) | `('en',)` only | 400 chars |

- Adapter: `mode: clone_with_intensity`, 24 kHz — **IMPL**.
- Per-job revision + reference digest for `chatterbox-4bit` — **TEST**
  (`test_chatterbox_clone_records_model_and_reference_revisions`).
- `chatterbox-8bit` and `chatterbox-turbo-4bit` have **no** revision test — **GAP**.
- Turbo is a genuinely distinct identity: English-only, 400-char budget. Must
  not be qualified as a Chatterbox precision variant.
- License: MIT — **CLAIM** (family summary prose only).

#### OmniVoice — 1 row, family `omnivoice`

| Row | RAM claim | Caps | Languages |
|---|---|---|---|
| `mlx-community/OmniVoice-bfloat16` | 8 GB (rec. hw text says 16 GB) | tts, clone, multilingual, expressive | 10 listed + literal `'+636 more'` |

- **Documented incompatibility reason found** (CHANGELOG §"Audited — OmniVoice
  dependencies and quantized artifacts"): the published 4-bit and 8-bit rows are
  excluded because their custom `omnivoice-rowwise` manifests are not loadable
  by MLX-Audio v0.4.6's generic quantization loader. bf16 remains the only
  supported conversion — **IMPL/CLAIM**, exactly as the task anticipated.
- **Internal contradiction:** `min_unified_memory_gb=8` while
  `recommended_hardware` says "M1 Pro / M2 16 GB recommended". Unresolved — **GAP**.
- No revision-evidence test — **GAP**. License Apache-2.0 — **CLAIM**.

#### Qwen3-TTS — 5 rows, family `qwen3-tts`, **three distinct operations**

| Row | Operation | RAM claim | Section budget |
|---|---|---|---|
| `Qwen3-TTS-12Hz-0.6B-Base-8bit` | clone | 8 GB | 360 chars |
| `Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit` | preset speakers | 8 GB | 360 chars |
| `Qwen3-TTS-12Hz-1.7B-Base-8bit` | clone | 16 GB | 360 chars |
| `Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit` | preset speakers | 16 GB | 360 chars |
| `Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit` | voice design | 16 GB | 360 chars |

- Operation is resolved from the repo name by `_qwen3_mode_from_repo()`
  (`generation.py:718`) → `design` / `custom` / `clone`, with `custom` as the
  fallback — **IMPL**. These are three different products and must be qualified
  separately.
- **Built-in voices are the strongest Group B evidence:** exactly 9 preset
  speakers (`Ryan, Aiden, Serena, Vivian, Uncle_Fu, Dylan, Eric, Ono_Anna,
  Sohee`), documented as verified against the CustomVoice `config.json` `spk_id`
  map, with an explicit regression note that two phantom speakers ("Ethan",
  "Chelsie") once passed app validation but were rejected by mlx-audio (removed
  v1.4.4) — **IMPL** with strong provenance; the enumeration itself is not
  asserted by any test in this run — **GAP**.
- Revision evidence tested for 0.6B CustomVoice (preset) and 0.6B Base (clone)
  — **TEST**. All three 1.7B rows — **GAP**.
- VoiceDesign has **no** dedicated test and no audited design-prompt limits — **GAP**.
- License Apache-2.0 — **CLAIM**.

#### VoxCPM2 — 2 rows, family `voxcpm-mlx`

| Row | RAM claim | Sample rate | Section budget |
|---|---|---|---|
| `mlx-community/VoxCPM2-4bit` (recommended) | 8 GB | 48 kHz | 400 chars |
| `mlx-community/VoxCPM2-bf16` | 12 GB | 48 kHz | 400 chars |

- `mode: voxcpm_flex`, the only family with `uses_cfg: True` (cfg_value +
  inference_timesteps) — **IMPL**. Voice-design instructions capped at 500 chars
  (`generation.py:2519`) — **IMPL**.
- 400-char budget is justified in-source by clone fidelity drifting past ~30 s —
  a developer judgement, **not** measured evidence — **CLAIM**.
- Revision evidence tested for `VoxCPM2-4bit` only — **TEST**; bf16 — **GAP**.
- **License: no claim exists anywhere.** Programmatic scan of family summary,
  how-to-use, every `best_for`, and every `use_case` found **zero**
  license-bearing prose for this family — the only Group B family with none.
  Both rows are commercially **evidence-blocked** — **GAP**.

#### VibeVoice Realtime — 3 rows, family `vibevoice`

| Row | RAM claim | Size | Caps |
|---|---|---|---|
| `VibeVoice-Realtime-0.5B-4bit` (intended initial candidate) | 8 GB | 0.7 GB | tts, streaming |
| `VibeVoice-Realtime-0.5B-8bit` | 8 GB | 1.2 GB | tts, streaming |
| `VibeVoice-Realtime-0.5B-fp16` | 8 GB | 2.1 GB | tts, streaming |

- All three English-only, 3000-char section budget, `_VIBEVOICE_MAX_TOKENS=4096`
  — **IMPL**.
- **Highest-risk finding.** `mode: voice_picker` requires a preset voice, but
  `generation.py:664-671` states plainly that VibeVoice (with KittenTTS) is
  deliberately excluded from the verified preset rosters "because their exact
  rosters aren't verifiable without the model on disk", leaving a free-text
  field. The catalog nonetheless advertises a concrete example id
  (`en-Emma_woman`). This is structurally the **same defect class** as the fixed
  Qwen phantom-speaker bug: an unverifiable voice id can pass app validation and
  fail inside mlx-audio. No row can be qualified until the roster is enumerated
  from the checkpoint — **GAP**.
- No revision-evidence test for any of the three rows — **GAP**.
- License MIT — **CLAIM**.

#### Fish Audio S2 Pro — 2 rows, family `fish-audio-mlx`

| Row | RAM claim | Size | Sample rate | Section budget |
|---|---|---|---|---|
| `mlx-community/fish-audio-s2-pro-8bit` (fleet candidate) | **24 GB** | 6.73 GB | 44.1 kHz | 300 chars |
| `mlx-community/fish-audio-s2-pro-bf16` (adjacent context) | 32 GB | 11.01 GB | 44.1 kHz | 300 chars |

- **Source of the 24 GB claim, as the task required:** it is the
  `min_unified_memory_gb=24` field in the catalog row, elaborated by
  `recommended_hardware` — "24 GB as the practical floor; 32 GB preferred for
  long-form cloning". It is an unmeasured catalog assumption. **No measured
  memory evidence exists** — **CLAIM only**.
- Revision + reference digest tested for the 8-bit row — **TEST**.
- License: the strongest and most explicit Group B license position — the public
  Fish model license is research/non-commercial unless separately granted,
  stated in family how-to-use, both `best_for` strings, and an `avoid` use-case.
  Still **CLAIM** (prose, no `license_sources`), and it is a blocking commercial
  question, not a technical one — **GAP + owner decision**.
- bf16 at 32 GB is outside the owner's 8/16/24 GB tiers; treat as context only.

### Cross-cutting findings

1. **Language claims are not enumerable.** Four families carry literal
   placeholder strings inside the typed `languages` tuple — `'+16 more'`
   (Chatterbox ×2), `'+636 more'` (OmniVoice), `'+19 more'` (VoxCPM2 ×2),
   `'+70 more'` (Fish ×2). These are display artifacts sitting in a data field.
   No complete language set can be derived from the catalog for these rows, and
   any consumer treating the tuple as language codes would ingest a non-code
   string. Per-language quality is untested for **every** Group B row — **GAP**.

2. **Catalog license prose has been wrong before.** The Group A Kokoro audit
   record carries `license_sources` URLs and a finding stating the previous
   catalog MIT wording was corrected to Apache-2.0. Group B license status is
   catalog prose with no `license_sources` for **all 16 rows**. Precedent shows
   this class of claim is not reliable evidence.

3. **Adapter runtime identity is pinned and shared.** All six families run
   through one runtime: `mlx==0.31.2`, `mlx-audio` pinned to git commit
   `d28d68c6ac4e28f7d2d66007f640b06cf3fd8ceb`, `mlx-lm==0.31.3` (Fish only),
   `transformers` (OmniVoice, Fish). This matches the runtime recorded in the
   Group A `run.json`, so one shared runtime identity covers Group A and B —
   **IMPL/TEST**, a genuine strength for later audit records.

4. **Long-form machinery is real, shared, and partly proven.**
   `_sentence_safe_text_chunks()` splits at paragraph → sentence → punctuation →
   whitespace → character, never dropping or rewriting text.
   `_join_long_form_wavs()` validates **every** section's sample rate, channel
   count, and non-zero frames *before* creating any candidate output, then joins
   via a temporary path with a 0.12 s pause — an all-or-nothing publish that
   prevents a partial render from looking successful — **IMPL**. The tests that
   would assert atomic publication and artifact evidence
   (`test_generation_artifact_contract.py`) are exactly the ones that could not
   run — **GAP**.

5. **40,000 characters is an adapter-managed product requirement, not a raw
   limit.** The Group A Kokoro audit proves the pattern end-to-end: 40,000 input
   characters → 55 private sections → 2567.7 s output, 99.35 % Whisper
   reference-word coverage, 3.55 GB measured peak. Group B section budgets imply
   roughly 111 sections (360 chars, Qwen) to 134 (300 chars, Fish) for the same
   job — **2–2.4× more join boundaries than any run ever validated**. No Group B
   row has any long-form endurance evidence — **GAP**.

6. **Resource telemetry exists and is well-formed** —
   `voicestudio.resource-telemetry` v1 captures peak RSS, MLX peak active,
   minimum available, max used %, peak pressure level, swap used and delta, and
   an outcome block — **TEST** (passed). It has simply never been *run* against
   any Group B model — **GAP**.

7. **The worker→Hub evidence envelope is versioned and ready.**
   `voicestudio_genstudio_integration.py` v1.1 exports 23 worker-owned fields
   including `internal_model_id`, `runtime_revision`, `voice_revision`,
   `reference_*`, `long_form_strategy`, `chunk_total`, and artifact
   `sha256`/duration/sample-rate, deliberately omitting `worker_id` because
   Studio Hub supplies it — **IMPL**. Correct ownership boundary; TASK-002
   assesses the Hub side.

### RAM: claims versus measured evidence

Per the task's explicit requirement, these are kept strictly separate.

| Tier | Catalog claim (unmeasured) | Measured 8/16/24 GB evidence |
|---|---|---|
| 8 GB | Chatterbox ×3, OmniVoice, Qwen 0.6B ×2, VoxCPM2-4bit, VibeVoice ×3 | **none** |
| 12/16 GB | VoxCPM2-bf16 (12), Qwen 1.7B ×3 (16) | **none** |
| 24 GB+ | Fish 8-bit (24), Fish bf16 (32) | **none** |

**Zero** measured memory evidence exists for any Group B row. The only measured
figure anywhere in the repository is Kokoro's 3.55 GB peak (Group A, 16 GB M4).
Two rows additionally carry internally inconsistent guidance (OmniVoice 8 vs
16 GB; VoxCPM2-bf16 12 GB vs "16 GB recommended").

### Proposed later qualification matrix (specification only — not run)

Per-row, per-operation, on owner-approved 8 / 16 / 24 GB machines:

1. **Short-form quality** — English plus each *enumerated* language; every
   built-in voice for preset families (Qwen CustomVoice: all 9).
2. **Operation-specific** — clone (Base, Chatterbox, VoxCPM2, OmniVoice, Fish);
   preset (Qwen CustomVoice, VibeVoice); design (Qwen VoiceDesign, VoxCPM2 flex,
   OmniVoice traits); streaming (VibeVoice).
3. **Adapter-managed long form** — one ~40,000-character job per candidate,
   recording section count, missing/duplicated/reordered text, identity drift
   across sections, pacing, clicks/gaps/overlaps at joins, and output integrity.
4. **Lifecycle** — chunk progress, mid-job cancellation, partial cleanup,
   deterministic retry, post-failure health.
5. **Resource** — peak memory, pressure level, swap delta, recovery, via the
   existing telemetry schema, at each tier.
6. **Headroom** — validate or correct each family's section budget (360/400/500/
   300/3000) against measured drift rather than developer estimate.

Suggested ordering by risk: VibeVoice-0.5B-4bit and Qwen-0.6B-CustomVoice are
cheapest to qualify (8 GB, small, preset-only); Fish-8bit is most expensive
(24 GB floor, 134 sections) **and** commercially blocked, so it should not lead.

## Architecture and contract review

- Controller/worker boundary preserved: yes — read-only inspection of the
  sibling repository; no Studio Hub or GenStudio path touched.
- GenStudio desired-state ownership preserved: yes — nothing approved, priced,
  published, or routed. Every recommendation is explicitly non-authoritative.
- Candidate approval remains deliberate: yes — reinforced by evidence.
  `model_audits.py` states a passed audit is only a *candidate*; 0 of 16 Group B
  rows even reach candidate status.
- Contract version or compatibility impact: none. No contract was read or
  changed in this task. `voicestudio_genstudio_integration.py` v1.1 observed
  read-only.
- Required rollout order: none from this task.

## Permissions exercised

| Action | Authorized | Performed | Evidence |
|---|---|---|---|
| Local commit | no | no | Worktree clean; no commit object created. |
| Push | no | no | No network git operation attempted. |
| Paid provider call | no | no | Zero. No network access of any kind. |
| Production data | no | no | No runtime data, cache, or live machine read. |
| Credential access/change | no | no | Zero. `ENVIRONMENT`/`credentials/**` untouched. |
| Migration create/apply | no | no | Zero. |
| Restart/deploy/live change | no | no | Zero. No service or fleet interaction. |
| Model download/generation | no | no | Zero. All tests are local/fake; no inference. |

Forbidden paths honoured: `app/backend/cache.py`, `app/backend/downloads.py`,
`models/**`, `logs/**`, `outputs/**`, `data/**`, `credentials/**`, `ENVIRONMENT`,
and both sibling repositories were never opened.

## Risks and limitations

1. The mandated verification suite cannot run in the mandated worktree. This
   affects **every** future task in this repository that uses the required
   isolated-worktree pattern and touches `backend.main` — not just this batch.
2. Audit-contract, artifact-contract, and model-storage assertions are unproven
   in this run; conclusions touching atomic publication and artifact evidence
   rest on source reading plus the Group A precedent, not on executed tests.
3. The gitfile root-cause hypothesis is unconfirmed by design (allowlist).
4. Group B language and voice rosters cannot be completed from the repository
   alone; the checkpoints are required.
5. The VibeVoice free-text voice field is a latent runtime-failure risk of a
   class that has already caused a production defect once.

## Controller decisions required

1. **Verification-environment decision (blocking).** Choose one: (a) extend a
   follow-up task's `allowed_paths` to include `app/backend/auto_update.py` and
   `app/backend/auto_update_config.py` so the worktree incompatibility can be
   confirmed and fixed; (b) authorize running this suite in a non-worktree
   checkout, accepting the reduced isolation; or (c) accept the 77-passing
   supplementary result as sufficient for triage and defer. Until this is
   decided, no task in this repository can satisfy a `backend.main`-dependent
   verification gate from an isolated worktree.
2. **Fish Audio commercial licensing (owner).** The public license is
   research/non-commercial. Decide whether to pursue a commercial grant, keep
   Fish internal-only, or drop it. This gates the most expensive candidate.
3. **VoxCPM2 license evidence (owner/controller).** No license claim exists
   anywhere for either VoxCPM2 row. Source and record it before any commercial
   consideration.
4. **VibeVoice roster policy.** Approve enumerating the preset roster from the
   checkpoint before qualification, or accept that VibeVoice stays internal-only.
5. **Scope confirmation.** Confirm that the `'+N more'` placeholder strings in
   the `languages` field are treated as a Voice Studio data defect to be fixed in
   a later implementation batch, not silently reinterpreted here.

## Suggested next action

Have TASK-003 consume these bounded findings as-is (the batch explicitly permits
a blocked dependency), and resolve decision 1 above before scheduling any Voice
Studio implementation batch, since that batch would inherit the same
verification gate.
