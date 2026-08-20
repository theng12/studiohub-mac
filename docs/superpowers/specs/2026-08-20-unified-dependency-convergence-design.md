# Unified Dependency Convergence Design

## Outcome

Studio Hub, Image Studio, and Voice Studio each own one deterministic dependency
convergence command. Fresh install, local Pinokio update, controller-driven
update, and explicit generation installation call that same repository-owned
command instead of maintaining separate dependency lists. A controller update
may report success only after dependency convergence, required tool and import
verification, restart, and health/version attestation have all succeeded.

Models remain a separate explicit workflow. A normal update never downloads a
model or creates a generation environment on a Mac that did not already have
one.

## Existing-machine compatibility

The first release in each repository is a dependency-neutral bridge: it adds
the convergence command and capability evidence but introduces no new mandatory
system dependency. Existing machines may remain on their current releases and
continue working. When they eventually receive the bridge through the existing
controller rollout, they gain the new behavior without a physical visit or an
individual Pinokio click.

Each app advertises `dependency_convergence: 1` in its existing automatic-update
capabilities. Future releases that add a new Conda/system dependency must be
withheld from a target lacking that capability. The supported rollout order is:

1. update the location controller to the bridge-capable Studio Hub;
2. roll the dependency-neutral bridge to installed Image, Voice, and Agent Hub
   applications;
3. verify `dependency_convergence: 1` and normal health on every participating
   target;
4. only then activate a later dependency-bearing release.

Offline or intentionally excluded machines remain on their last working
release and do not block other machines. They must receive the bridge before a
later dependency-bearing release when they return.

## Repository-owned command

Each repository adds a small standard-library module at
`app/backend/dependency_convergence.py`. It is repository-local because the
three products have different requirements and must remain independently
installable. No shared package, plugin, manifest framework, or new dependency
is introduced.

Image Studio and Voice Studio support exactly three invocation modes:

- `base`: converge every dependency required for the API/server to run;
- `generation`: converge the complete generation stack and verify its required
  imports and tools.

An `all-installed` invocation always runs `base` and runs `generation` only when
the repository's existing generation marker proves that generation was already
installed. It never bootstraps a multi-gigabyte generation stack implicitly.

Studio Hub has no generation stack. Its command accepts `base` and
`all-installed` (which are equivalent for Hub) and rejects `generation`.

The command uses the active app environment and Pinokio's existing Conda/uv
toolchain. It runs only fixed repository-authored commands. Request bodies,
controller data, registry values, and remote input can never supply a command,
package, channel, executable, or path.

## Product contracts

### Studio Hub

- Base: install `app/requirements.lock` into `conda_env`.
- Verify the backend import and Hub application version through the existing
  updater verification and health checks.
- Hub has no generation mode.

### Image Studio

- Base: install the current base requirements used by Image Studio updates.
- Generation: install `requirements-generation.lock.txt` and verify `mflux`.
- `all-installed` runs generation only when the existing `mflux` marker is
  present.

### Voice Studio

- Base: ensure `ffmpeg` and `ffprobe` through the current Pinokio Conda
  environment, verify both executables, then install the current base Python
  requirements.
- Generation: install `requirements-generation.txt` and verify the existing
  Voice generation imports.
- `all-installed` runs generation only when the existing generation marker is
  present.

## Callers

Within each repository:

- `install.js` invokes `base` after creating/selecting `conda_env`;
- `update.js` invokes `all-installed` after its Git update;
- the internal `AutoUpdater._install_dependencies()` invokes the same
  `all-installed` command after its fast-forward;
- `install_generation.js` invokes `generation`;
- reset behavior remains unchanged.

Launcher files keep their current Pinokio structure, run mode, stop/restart
ownership, and URL-capture behavior. Only their duplicated dependency command
lists are replaced by the single module invocation.

## Failure and rollback

The convergence command exits nonzero on a package installation, required-tool,
or import failure. Local launcher runs withhold their success notification.
Controller-driven updates remain under the existing app-local update lock,
readiness gate, rollback point, restart logic, and health checks. A failed
convergence therefore produces a visible failed update and invokes the existing
rollback path; it cannot become `complete` merely because the server process
answers its basic health endpoint.

No arbitrary privilege escalation is added. If a future dependency cannot be
installed in the user-owned Pinokio environment, that release must provide a
separate owner-approved installer rather than weakening controller execution.

## Controller behavior

The controller continues orchestrating target-local updaters; it does not
install dependencies over the network itself. Ordinary rolling updates remain
serial and health-gated. Exact managed releases retain commit attestation.

Inventory and job evidence expose the target's dependency-convergence
capability and retain the existing target-local failure behavior. A missing
bridge capability is a clear `bridge_update_required` outcome for any future
dependency-bearing rollout, not a generic success or a site-wide block.

## Verification

Each repository uses TDD to prove:

- all four callers invoke the same convergence module and contain no duplicate
  package-install command list;
- base, generation, and all-installed select the exact expected commands;
- all-installed does not create a missing generation environment;
- Voice verifies `ffmpeg` and `ffprobe` in the controller path;
- a convergence failure prevents success and exercises existing rollback;
- capability evidence is present only on the bridge-capable release;
- launcher JavaScript parses and preserves current run-mode/restart behavior;
- release metadata, focused updater tests, and the full repository suites pass.

The bridge implementation stops after local commits and pushes only when the
owner has authorized them. It performs no live fleet update, dependency install,
service restart, generation, model, or enrollment action.
