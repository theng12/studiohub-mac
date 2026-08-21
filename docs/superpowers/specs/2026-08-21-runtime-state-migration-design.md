# Runtime-State Migration Design

## Goal

Make one physical visit sufficient to put each TerraNash Mac onto a durable update baseline: future Studio Hub fleet updates may update Studio code and declared dependencies without runtime-generated files making otherwise clean checkouts look dirty.

The canonical migration kit must remain tracked in Studio Hub and be reproducible onto this SSD or a replacement SSD.

## Scope

- Image Studio KH 1.30.4.
- Voice Studio KH 2.4.3.
- Studio Hub KH 2.11.6.
- The tracked TerraNash SSD bootstrap kit and its synchronized copy on `/Volumes/ugreen-terranash`.
- A local, operator-run migration command. No remote shell, new fleet maintenance API, live fleet update, model mutation, enrollment change, roster change, or GenStudio change.

## Root cause

Image Studio and Voice Studio currently track `ENVIRONMENT` in Git while their service installers append three machine-specific startup ownership settings to it. Studio Hub 2.9.0 also created runtime repair journal/lock files before every runtime filename was ignored. Exact updaters correctly reject these changed or untracked paths; removing that safety check would hide real operator edits and is not acceptable.

## Durable repository contract

### Image and Voice

- Track the default template as `ENVIRONMENT.example`.
- Ignore root `ENVIRONMENT`; it is per-machine runtime state.
- Seed `ENVIRONMENT` from `ENVIRONMENT.example` only when the machine-local file is absent.
- Never overwrite an existing `ENVIRONMENT` during install, update, service installation, rollback, or restart.
- Service ownership settings continue to be written atomically to the machine-local file.
- When the new updater rolls back to a legacy commit that still tracks `ENVIRONMENT`, it snapshots and atomically restores the regular machine-local file so Git cannot overwrite operator settings. The legacy tree may again report that tracked file as dirty, but data is preserved and the physical migration can be retried.
- Updater dirty-path rendering parses Git porcelain without stripping the status-column leading space or truncating the first filename character.
- Existing dependency-convergence and rollback health safeguards remain unchanged.

### Studio Hub

- Ignore root `.enrollment_repair_journal.json` in addition to the existing repair lock files.
- Preserve repair durability; the journal is ignored, never deleted by update or migration.
- Fix any equivalent Git porcelain path parsing without weakening dirty-checkout refusal.
- Preserve dependency convergence, release reconciliation, enrollment, and capability schema behavior.

## One-time physical migration

The SSD exposes `5 Migrate Studio Updates.command`, backed by a standard-library Python tool in the canonical kit.

The tool:

1. Resolves `PINOKIO_HOME` through the existing supported bootstrap resolver and recognizes canonical and legacy `.git` folder names.
2. Supports a no-write `--dry-run` mode.
3. Refuses if customer work or an update is active when a supported local health/status endpoint can prove that state.
4. Inspects Git porcelain with NUL-safe parsing.
5. Refuses every unknown dirty path and every unknown `ENVIRONMENT` edit.
6. Accepts an old tracked Image/Voice `ENVIRONMENT` only when its entire delta from `HEAD:ENVIRONMENT` is exactly one copy of each approved service-ownership line:
   - `PINOKIO_SCRIPT_AUTOLAUNCH=start.js`
   - `PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false`
   - `PINOKIO_SCRIPT_REQUIRES=`
7. Writes a mode-0600 backup under `~/Library/Application Support/TerraNash/runtime-state-migration/<timestamp>/`; no machine secret is copied to the SSD.
8. Restores only the verified tracked `ENVIRONMENT` preimage needed for Git to fast-forward. It never runs broad `reset`, `clean`, `stash`, or deletion.
9. Adds only the exact root-anchored legacy Hub runtime patterns to `.git/info/exclude`; it never deletes a journal or lock inode.
10. Invokes each Studio's existing Pinokio `update.js` through the supported local Pinokio interface, Image then Voice then Hub, and stops on the first failure.
11. Verifies Image/Voice now track `ENVIRONMENT.example`, ignore `ENVIRONMENT`, preserve the prior machine values and service ownership lines, and report dependency convergence. It verifies Hub ignores the repair journal and reports dependency convergence.
12. Is idempotent: new installations and already-migrated Macs report `Already ready` without changing files.

If verification cannot prove an edit is the known historical runtime mutation, the command stops and names the Mac/repository/path requiring manual review. It never guesses.

## New-machine behavior

Stage 2 installs current mains, so new machines receive the durable baseline directly. Stage 5 is harmless and reports already ready. Models remain a separate Stage 3 concern and are not downloaded or changed by this migration.

## SSD source of truth

- Canonical files live under `studiohub-mac/ssd_bootstrap/kit` and `studiohub-mac/ssd_bootstrap/root`.
- `tools/sync_ssd_bootstrap.py` remains the only deployment path to a mounted SSD.
- The synchronized SSD inventory includes the new command and helper.
- `START HERE - TerraNash Mac Setup.md` explains when to run Stage 5 and that the Git copy can reproduce a second SSD.

## Release and verification

- Each repository follows its own version, changelog, release-note, review, and push rules.
- Tests cover seed-without-overwrite, service ownership, exact dirty-path formatting, migration refusal, backup/rollback, idempotency, legacy names, and SSD byte-for-byte synchronization.
- Run focused suites first, then each repository's full suite, syntax checks, dependency checks, metadata checks, and `git diff --check`.
- No live fleet update is part of this release. The owner will run the SSD migration locally during physical visits, then use ordinary updates or allow the normal update schedule.
