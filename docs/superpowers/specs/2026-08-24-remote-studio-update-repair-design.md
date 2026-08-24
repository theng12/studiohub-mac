# Remote Studio Update Repair Design

## Goal

Let an owner repair the known legacy `ENVIRONMENT` update blocker once from
Studio Hub, locally or through the authenticated fleet controller, without
losing machine-specific settings. After repair, the Studio uses its ordinary
updater and dependency-convergence path for future releases.

## Problem and root cause

Voice and Image releases predating their runtime-state migration tracked the
root `ENVIRONMENT` file. Pinokio then wrote machine-specific startup settings
to that tracked file. Some Macs also require an operator setting such as
`CPLUS_INCLUDE_PATH` for their local compiler installation. The legacy
`update.js` starts with `git pull`, so Git correctly refuses to replace the
dirty tracked file before the Mac can receive the newer updater that treats
`ENVIRONMENT` as ignored runtime state.

A repository-only push cannot safely bootstrap both cohorts:

- legacy Macs have a dirty tracked `ENVIRONMENT`;
- migrated Macs have an untracked, ignored `ENVIRONMENT` at the same path; and
- the blocked legacy updater cannot load newly pushed repair code before its
  initial `git pull` succeeds.

The repair therefore belongs to the already-running Studio Hub, outside the
blocked sibling Studio checkout.

## Scope

- Repair the installed sibling Voice Studio and Image Studio checkouts on a
  Mac whose Studio Hub has this capability.
- Expose the repair in the controller's Updates workspace and through an
  authenticated controller-to-Agent maintenance operation.
- Preserve the complete machine-local `ENVIRONMENT` bytes and file mode,
  including the 0310 `CPLUS_INCLUDE_PATH` workaround and future legitimate
  local settings.
- Reuse each Studio's normal updater so declared application and system
  dependencies, restart, health, exact-commit, and rollback verification remain
  authoritative.
- Keep SSD Stage 5 as the local fallback for a Hub that cannot receive or run
  the remote capability.

Out of scope: repairing Studio Hub's own checkout from inside the running Hub,
changing models or model caches, modifying enrollment or fleet tokens, changing
voices or jobs, weakening Git safety checks, or initiating a live fleet repair
as part of the software release.

## Safety contract

The Agent Hub performs all filesystem and Git mutations locally. The controller
never receives `ENVIRONMENT` contents or local paths.

Before changing a sibling checkout, the Agent must prove all of the following:

1. The target is the registered canonical/legacy `.git` sibling directory
   below `PINOKIO_HOME/api`, not a symlink or caller-provided path.
2. It is a regular Git checkout on `main`, with the expected repository origin,
   and can fast-forward to `origin/main`.
3. The only dirty path is the root tracked `ENVIRONMENT`, or the checkout is
   already migrated and otherwise clean.
4. `ENVIRONMENT` is a regular non-symlink file and remains the same inode,
   bytes, and mode between validation and the atomic claim.
5. No work or another maintenance operation is active for the Studio.

Unlike the original physical migration, repair does not interpret or allowlist
individual environment lines. The whole regular file is machine-owned state
and is preserved exactly. Every other dirty or untracked source path still
fails closed with a visible reason. Detached, divergent, wrong-origin,
symlinked, concurrently changed, or unsupported layouts also fail closed.

## Local repair transaction

For each requested sibling Studio, the Agent Hub:

1. Drains active work using the existing broker maintenance guard.
2. Writes a mode-0600 recovery copy under Studio Hub's local application-state
   backup directory; no secret or backup is written to the SSD or controller.
3. Atomically moves `ENVIRONMENT` into Git-owned recovery storage in the target
   checkout and verifies the claimed bytes, mode, and identity.
4. If the current legacy commit tracks `ENVIRONMENT`, restores only the exact
   `HEAD:ENVIRONMENT` preimage needed for Git to fast-forward. It never uses
   broad reset, clean, stash, or deletion.
5. Invokes the target Studio's trusted existing `update.js` through the local
   Pinokio interface and waits for its finite result.
6. Verifies the updated checkout no longer tracks `ENVIRONMENT`, root
   `ENVIRONMENT` is ignored, the expected release/commit is running, declared
   dependencies converged, and health returned.
7. Atomically restores the original machine bytes and mode, verifies them, and
   removes only transaction claims that are proven unchanged.

If a failure occurs before or after update, the engine restores the exact
machine file when safe. If concurrent state prevents safe restoration, it
retains the claim and recovery copy and reports their local recovery location.
It never overwrites an unknown concurrent file.

An already-migrated clean Studio is idempotent: repair may run its ordinary
update if a release is available, otherwise it returns `Already ready` without
altering runtime state.

## Durable fleet operation

Studio Hub adds a durable `studio-update-repair` job patterned after generation
dependency installation:

- one Studio at a time per Mac;
- independent Macs may proceed in parallel;
- local work is drained before each target;
- state is saved after every transition;
- authenticated Agents expose only a fixed local repair operation and status
  lookup;
- the controller records per-machine/per-Studio queued, draining, validating,
  repairing, updating, verifying, complete, pending, or failed state;
- offline/unreachable Agents remain pending/retryable rather than being marked
  repaired; and
- retries are idempotent and target only unfinished items.

Capability discovery advertises an exact repair schema. The controller enables
remote repair only for Agents reporting that capability. Older reachable Hubs
remain fully usable for existing work and ordinary updates, but require one Hub
update before remote sibling repair becomes available.

## User experience

The Updates workspace gains **Repair blocked Studio updates** with a concise
explanation that it is a one-time migration for legacy dirty `ENVIRONMENT`
checkouts. The owner can run it for affected rows or all eligible machines and
see the durable status and refusal reason per Studio.

The action does not claim that every failed update is this known blocker. A
refusal names the exact safety condition and leaves the ordinary update button
unchanged. Models, enrollment, voices, and jobs are explicitly shown as
untouched.

## Remote boundary and fallback

Once an Agent Hub has this release, is reachable, and accepts the controller's
fleet authentication, Voice/Image repair can be started remotely after the
owner leaves the location. A Hub cannot safely update its own blocked checkout
from code it has not received. If that Agent Hub is itself blocked, offline, or
too old to advertise the capability, the owner must use SSD Stage 5 (or the
equivalent local migration) once on that Mac. Future sibling Studio updates can
then use normal controller automation.

## Verification

Tests use disposable Git repositories and fixed fake Pinokio commands to prove:

- legacy dirty tracked `ENVIRONMENT` fast-forwards to the ignored runtime-file
  layout;
- arbitrary bytes, line endings, mode, startup settings, and
  `CPLUS_INCLUDE_PATH` survive exactly;
- migrated and already-current checkouts are idempotent;
- unknown dirt, wrong origin/branch, divergence, symlinks, concurrent edits,
  update failure, dependency failure, and health failure all fail closed and
  retain recovery evidence;
- local and remote authorization, capability gating, persistence, polling,
  pending/retry, and one-at-a-time-per-Mac behavior are enforced; and
- the Updates UI renders eligible, pending, complete, and refused states.

Focused suites run first, followed by the complete Studio Hub suite, frontend
syntax checks, dependency checks, release-metadata checks, and
`git diff --check`. The release is committed and pushed, but it performs no live
fleet repair or update by itself.
