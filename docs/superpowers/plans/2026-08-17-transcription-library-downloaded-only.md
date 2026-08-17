# Transcription Library Downloaded-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide uncached transcription models from Studio Hub's Model Library without changing backend inventory contracts.

**Architecture:** Add one frontend row-selection helper used by `renderModels()`. The helper removes only rows whose modality is `transcription` and whose `downloaded` flag is false; all other rows and backend payloads remain unchanged.

**Tech Stack:** Vanilla JavaScript embedded in `app/frontend/index.html`, pytest frontend contract tests, Studio Hub release metadata.

## Global Constraints

- Backend API and capability schema remain unchanged.
- No launcher, dependency, fleet, model, download, or worker changes.
- Ship as Studio Hub `2.9.1` with synchronized VERSION, CHANGELOG, and What's New metadata.

---

### Task 1: Protect and implement downloaded-only transcription rows

**Files:**
- Modify: `app/frontend/index.html`
- Create: `app/tests/test_frontend_model_library.py`

**Interfaces:**
- Consumes: model rows returned by `/api/hub/models`.
- Produces: `modelLibraryRows(models)` returning the UI-visible model rows.

- [x] **Step 1: Add a failing behavioral test**

  Execute the real inline JavaScript helper with literal rows for downloaded and
  uncached transcription models plus an uncached image model. Assert that the
  result contains the downloaded transcription and image rows but not the
  uncached transcription row.

- [x] **Step 2: Run the focused test and capture the expected failure**

  Run `conda_env/bin/python -m pytest -q app/tests/test_frontend_model_library.py`.
  Expected: failure because `modelLibraryRows` does not exist.

- [x] **Step 3: Implement the minimal frontend filter and empty state**

  Add `modelLibraryRows(models)` and use it before machine filtering and counts
  in `renderModels()`. Show `No downloaded transcription models.` only when the
  Transcription modality is selected and no row remains.

- [x] **Step 4: Run focused and frontend regression tests**

  Run the focused test, `node --check` against extracted inline scripts, and the
  existing frontend test set.

### Task 2: Release and verify Studio Hub 2.9.1

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html`

- [x] **Step 1: Synchronize release metadata**

  Set VERSION to `2.9.1`, add a dated changelog entry, and add the first embedded
  What's New entry describing the UI-only downloaded transcription filter.

- [x] **Step 2: Run release and full verification**

  Run release metadata tests, the full `app/tests` suite, compileall, JavaScript
  syntax checks, `pip check`, and `git diff --check`.

- [x] **Step 3: Commit and publish**

  Stage only the spec, plan, UI/test, and release metadata files; commit once and
  push the fast-forward branch to `origin/main`. Do not update the fleet.
