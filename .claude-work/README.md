# Studio Hub Claude work queue

This directory is the durable handoff between the human owner, the Codex
controller, and Claude Desktop workers. It coordinates implementation work; it
is not part of the Studio Hub runtime and must never contain credentials,
customer data, registration tokens, fleet tokens, raw private endpoints, or
unsanitized worker errors.

A commit whose staged diff is entirely inside `.claude-work/**` is queue-only
orchestration state and does not require a Studio Hub application version bump.
If the same commit touches code, tests, launchers, configuration, migrations,
shared contracts, or runtime documentation, the repository's normal release
discipline applies without exception.

## Responsibilities

### Studio Hub controller

Studio Hub is the site controller. It authenticates GenStudio, persists the
last-good global desired state, schedules eligible sibling workers, transports
approved inputs and outputs, and reports truthful fleet evidence. It does not
invent customer products, prices, publication, or global routing policy.

### Sibling execution workers

Image Studio, Voice Studio, Video Studio, Music Studio, and Chat Studio report
their exact capabilities and execute jobs assigned through Studio Hub. A
sibling may publish an audited candidate, but that candidate never becomes
approved, cached, routed, or customer-visible automatically.

### GenStudio global controller

GenStudio owns global desired state, customer products, prices, publication,
and cross-site routing policy. GenStudio communicates with sibling workers
through a Studio Hub controller and never dispatches directly to a sibling.

### Claude Desktop worker

Claude performs only the focused tasks assigned in a batch. It works in the
task's isolated Git worktree, respects allowed and forbidden paths, runs every
verification command, commits only when authorized, and writes sanitized task
and batch reports. It never merges, pushes, deploys, restarts services, changes
live fleet state, or spends provider credits without explicit task authority
and the required controller review gate.

### Human owner and Codex controller

The human owner decides product intent, spending, production authority,
credentials, live deployments, and unresolved business or safety choices. The
Codex controller defines batches, inspects worker output, requests corrections,
reviews contracts and verification evidence, and decides what is ready to
merge. Neither role is delegated implicitly to a worker.

## Directory lifecycle

- `queue/`: reviewed batch and task files waiting to start.
- `active/`: batch and task files currently being worked.
- `completed/`: terminal task or batch definitions that met their acceptance
  criteria.
- `blocked/`: work that stopped because a required decision, dependency, or
  authority is missing.
- `skipped/`: work deliberately not attempted, with the reason recorded.
- `reports/`: one report per task and one final report per batch.

Keep the filename stable while moving it between state directories. Suggested
names are `BATCH-<batch-id>.md` and `<batch-id>__TASK-<task-id>.md`. Reports use
`reports/<batch-id>/<task-id>.md` and
`reports/<batch-id>/BATCH_REPORT.md`.

Task files are the state authority. A task must exist in exactly one state
directory. Reports are evidence and do not replace task state.

## Batch execution

A batch defines a dependency graph and one or more phases:

- `sequential`: start only after every listed dependency completed.
- `parallel`: may run with other ready tasks when repositories and allowed
  paths are disjoint.
- `mixed`: the batch defines explicit parallel groups separated by sequential
  gates.

Blocked work does not stop independent tasks. Claude continues every task whose
dependencies are satisfied and whose path boundaries do not overlap. The batch
report records all completed, blocked, and skipped work and the exact decisions
still required from the controller.

## Isolated worktrees

Implementation never occurs in the controller's primary checkout. Every task
records:

- the exact repository root;
- base branch and immutable base commit;
- task branch;
- absolute isolated worktree path;
- allowed and forbidden paths.

Prefer a worktree outside the repository, for example
`<repo-parent>/.claude-worktrees/<repo-name>/<batch-id>/<task-id>`. Never reuse
one implementation worktree for concurrent tasks. Cross-repository work must
be split into a Studio Hub task and one task for each sibling repository, each
with its own root, worktree, branch, verification, and report.

The `.claude-work` state files remain in the controller checkout. A worker may
move only its assigned files and write its assigned reports there. Source
changes belong only in the task worktree.

## Hard path boundaries

`allowed_paths` is an allowlist, not a hint. A worker must stop if required work
falls outside it. `forbidden_paths` records especially sensitive or competing
areas and always wins over the allowlist. Paths must be repository-relative and
must not use unresolved globs for destructive or state-changing commands.

Sibling repositories are forbidden unless a separate task names their exact
repository root and exact allowed paths. Preserve unrelated dirty work. Never
stash, reset, force-checkout, broadly stage, or rewrite another task's commit.

## Permission model

Every task records each permission independently. Use only these values:

- `denied`: the action is prohibited.
- `allowed`: the action is within task scope, subject to any controller gate.
- `read_only`: inspection only; no mutation.
- `local_only`: local development state only; never production or fleet state.
- `requires_controller`: stop and request a decision before the action.

Required task permissions are:

- local commit;
- push;
- paid-provider calls;
- production-data read and write;
- credential read and write;
- migration creation, local application, and production application;
- service restart;
- deployment;
- live configuration change;
- live fleet-state change.

Commit permission does not imply push permission. Push permission does not
imply merge or deployment permission. Controller review is always required
before merge, push, deployment, restart, migration application, credential
change, production-data mutation, or live configuration/fleet mutation.

## Provider and production safety

Provider calls default to denied, including free-tier calls that consume quota
or create remote artifacts. Documentation reads and local fake-worker tests are
not provider calls. A task authorizing provider calls must name the provider,
maximum spend or quota, permitted endpoints, test data, and stop condition.

Never place credentials in task files, commands, commits, or reports. Tasks may
refer only to a credential handle or approved secret store. Production data is
denied by default. Reports must aggregate or sanitize operational evidence and
must never reproduce customer content or raw private errors.

## Job and fleet safety

- Never interrupt or restart active jobs unless a task explicitly authorizes a
  safe drain and recovery procedure.
- Never automatically retry an accepted job whose outcome is uncertain.
- Never expose registration tokens, fleet tokens, credentials, internal
  endpoints, or raw worker errors.
- Global desired state comes from GenStudio. Studio Hub persists the last-good
  state and reconciles only eligible sibling workers.
- Shared contract changes must be versioned and backward-compatible or include
  an explicit coordinated rollout and rollback plan.

## Creating a batch

1. Copy `BATCH_TEMPLATE.md` into `queue/BATCH-<batch-id>.md`.
2. Copy `TASK_TEMPLATE.md` once per task into
   `queue/<batch-id>__TASK-<task-id>.md`.
3. Pin the base commits and record disjoint path boundaries.
4. Fill every permission; never rely on a default hidden from the task.
5. Add contract files using `CONTRACT_TEMPLATE.md` when a shared boundary
   changes.
6. Have the controller review the complete batch before handing it to Claude.
7. Paste `WORKER_PROMPT.md` into Claude Desktop from the repository root.

## Controller acceptance

After Claude finishes, the controller must inspect Git status, diffs, commits,
worktrees, reports, and verification output. A passing worker report is not a
merge decision. The controller records requested corrections or explicit
approval separately and performs any authorized integration through the normal
Studio Hub release process.
