---
kind: studiohub.claude-task
schema_version: 1
task_id: TASK-001
batch_id: BATCH-2026-08-02-GROUP-B-TRIAGE
title: Audit existing Voice Studio evidence for Group B models
state: queue
priority: high
execution: parallel
dependencies: []
parallel_group: evidence-audit
repository_root: /Users/thengmacmini/pinokio/api/voicestudio-mac.git
orchestration_root: /Users/thengmacmini/pinokio/api/studiohub-mac
base_branch: main
base_commit: e5b764ac465f7e6cb75f03411bcd88e5afb7710d
task_branch: claude/group-b-triage-voice-evidence
worktree_path: /Users/thengmacmini/pinokio/.claude-worktrees/voicestudio-mac/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001
allowed_paths:
  - AGENTS.md
  - VERSION
  - CHANGELOG.md
  - README.md
  - model-audits/**
  - app/requirements.txt
  - app/requirements.lock.txt
  - app/requirements-generation.txt
  - app/requirements-generation.lock.txt
  - app/backend/catalog.py
  - app/backend/generation.py
  - app/backend/main.py
  - app/backend/model_audits.py
  - app/backend/model_storage.py
  - app/backend/voicestudio_genstudio_integration.py
  - app/backend/voices.py
  - app/tests/test_model_audit_contract.py
  - app/tests/test_priority_mlx_models.py
  - app/tests/test_qwen_revision_evidence.py
  - app/tests/test_generation_artifact_contract.py
  - app/tests/test_reference_audio_contract.py
  - app/tests/test_resource_telemetry.py
  - app/tests/test_model_storage.py
forbidden_paths:
  - .env
  - .env.*
  - ENVIRONMENT
  - credentials/**
  - data/**
  - logs/**
  - outputs/**
  - models/**
  - app/backend/cache.py
  - app/backend/downloads.py
  - ../studiohub-mac/**
  - ../imagestudio-mac/**
contract_files: []
report_path: .claude-work/reports/BATCH-2026-08-02-GROUP-B-TRIAGE/TASK-001.md
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
  stop_condition: Provider calls, network model access, downloads, and generation are denied.
created_by: controller
created_at: 2026-08-02T17:01:03Z
updated_at: 2026-08-02T17:01:03Z
---

# Objective

Produce a truthful source-and-test evidence matrix for every exact Voice Studio
checkpoint and operation currently associated with the six Group B families.
Do not change Voice Studio source or make a commercial approval decision.

## Why this task exists

Voice Studio currently has catalog claims and implementation history for Group
B, but only Group A has durable model-audit records. The owner needs to know
what is already proven, what is merely claimed, and the exact offline and live
qualification work still required before deliberately exposing any candidate.

## Required reading

- Voice Studio `AGENTS.md`
- Studio Hub `.claude-work/README.md`
- The active batch and this complete task
- `README.md`, `CHANGELOG.md`, and `VERSION`
- `model-audits/` and `app/backend/model_audits.py`
- Every allowed backend, dependency, and test file relevant to a reported fact

## Architecture boundaries

- Voice Studio is the sibling execution worker and evidence producer.
- It may report an audited candidate, but cannot approve, cache fleet-wide,
  route, price, publish, or make it customer-visible.
- Model-specific voice-reference preparation and adapter-managed long form
  remain Voice Studio responsibilities.
- One customer request may contain up to 40,000 characters even when Voice
  Studio must use sentence-safe, model-specific internal chunks.
- Failure of a raw model to accept 40,000 characters in one pass does not make
  the adapter-managed product ineligible.
- RAM thresholds are unknown until measured on the owner's available 8, 16, and
  24 GB machine tiers. Catalog values must be labeled as claims, not evidence.
- Do not inspect caches, live machines, private voices, customer audio, or
  credentials in this task.

## In scope

- Chatterbox: all exact catalog variants, with the recommended 4-bit variant
  clearly separated from 8-bit and Turbo.
- OmniVoice: the compatible bfloat16 checkpoint and the documented reason the
  compact conversions are currently incompatible.
- Qwen3-TTS: Base, CustomVoice, and VoiceDesign checkpoints and their distinct
  operations; do not treat the family as one interchangeable model.
- VoxCPM2: 4-bit and bf16 checkpoints as distinct qualification identities.
- VibeVoice Realtime: every catalog precision, while identifying the intended
  initial 0.5B 4-bit candidate.
- Fish Audio S2 Pro: the 8-bit candidate, license/commercial evidence, and the
  source of the current 24 GB claim; bf16 only as adjacent context.
- Existing adapter, catalog, test, dependency, revision, artifact, reference,
  long-form, cancellation, cleanup, telemetry, and storage evidence.

## Out of scope

- Editing any Voice Studio file.
- Reading or changing `app/backend/cache.py` or `app/backend/downloads.py`; both
  contain unrelated concurrent work in the primary checkout.
- Running models, generating samples, downloading or completing checkpoints,
  accessing Hugging Face or any provider, or inspecting fleet machines.
- Declaring a model sellable, approving it, changing RAM requirements, or
  publishing a sibling capability.
- Editing Studio Hub, GenStudio, or another sibling repository.

## Allowed-path rationale

The allowlist contains the committed catalog and adapter claims, durable audit
records, locked dependency evidence, and focused fake/local tests needed for a
read-only determination. The primary checkout's two dirty cache/download files
are explicitly forbidden and absent from the pinned worktree state.

The only authorized mutations are this task's queue-state moves and sanitized
report under the Studio Hub `orchestration_root`; no Voice Studio source path
may change.

## Evidence requirements

1. Inspect Git before work and confirm the primary Voice checkout's unrelated
   dirty files are preserved and excluded from the pinned worktree.
2. Inventory exact Group B catalog rows and group them by family, checkpoint,
   precision, operation, and immutable revision evidence.
3. For every exact row, report in a compact matrix:
   - internal model ID and display name;
   - catalog operations and implemented operations;
   - immutable revision/hash evidence and durable audit record, if any;
   - adapter/runtime identity and locked dependency evidence;
   - built-in voices and whether the complete set is enumerated;
   - supported languages and whether each claim is tested;
   - voice cloning, voice design, preset voice, or streaming behavior;
   - reference-audio minimum, recommended and maximum duration, format,
     transcript, and multi-reference requirements where applicable;
   - native limit versus adapter-managed long-form behavior;
   - sentence-safe chunking, joining, artifact validation, final speed control,
     progress, cancellation, cleanup, retry and resource telemetry evidence;
   - catalog RAM claim and whether any measured 8/16/24 GB evidence exists;
   - license and commercial-use evidence;
   - candidate, internal-only, incompatible, or evidence-blocked recommendation,
     explicitly non-authoritative.
4. Distinguish implementation existence from tested proof. A changelog or UI
   claim alone is not qualification evidence.
5. Identify exact gaps for short-form quality, approximately 40,000-character
   adapter-managed stability, missing/duplicated/reordered text, identity drift,
   pacing, clicks/gaps/overlaps, output integrity, progress, cancellation,
   partial cleanup, deterministic retry, memory peak/pressure/swap/recovery, and
   post-failure health.
6. Propose the later grounded qualification matrix on 8, 16, and 24 GB tiers.
   Include model-specific operations and safe chunk headroom, but do not run it.
7. Identify questions that truly require the owner, licensing evidence, or a
   live machine rather than guessing.

## Verification commands

Run every command from the pinned Voice Studio worktree. These are local tests;
they must not download a model or perform inference.

```sh
git status --short --branch
/Users/thengmacmini/pinokio/api/voicestudio-mac.git/conda_env/bin/pytest -q app/tests/test_model_audit_contract.py app/tests/test_priority_mlx_models.py app/tests/test_qwen_revision_evidence.py app/tests/test_generation_artifact_contract.py app/tests/test_reference_audio_contract.py app/tests/test_resource_telemetry.py app/tests/test_model_storage.py
git diff --check
git status --short --branch
```

## Acceptance criteria

- [ ] All six families and every exact relevant catalog row appear in the report.
- [ ] Claims, implementation evidence, test evidence, and missing live evidence
  are visibly distinct.
- [ ] Qwen modes and model sizes are not collapsed into one candidate.
- [ ] RAM and commercial-use conclusions are not invented.
- [ ] The later 8/16/24 GB test matrix is model- and operation-specific.
- [ ] No Voice Studio source file changed and no local commit was created.
- [ ] Only allowed paths were read; forbidden paths and live state were untouched.
- [ ] Every verification command passed, or the exact sanitized failure is reported.
- [ ] No secret, private endpoint, customer data, or raw worker error appears.
- [ ] The task report was written immediately after work stopped.

## Safe stop conditions

Stop and move the task to `blocked/` when a required fact cannot be established
without a forbidden file, network access, credential, model execution, live
machine, license decision, or source edit. Record the missing evidence and
continue only with conclusions that remain independently supportable.

## Controller decisions already recorded

- Codex, `2026-08-02T17:01:03Z`: authorize read-only source inspection, isolated
  worktree creation, focused local tests, task-state moves, and sanitized report
  writing only.
- Human owner: all Group B families are desired sellable candidates when the
  evidence permits; otherwise they may remain internal-only.
- Human owner: later quality qualification must cover both English and supported
  non-English languages, all built-in voices, cloning where supported, and
  adapter-managed long form; this task does not perform that qualification.

## State history

- `2026-08-02T17:01:03Z` — created in `queue/` by controller.
