# Controller-Managed Enrollment Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Studio Hub 2.9.0 with owner-initiated, Controller-managed repair of a registered Mac's Agent identity, without re-enrollment, registry re-keying, fleet-token mutation, or disruption of later Macs when one result is uncertain.

**Architecture:** Add a digest-only SQLite ticket/batch store and one Controller coordinator beside the permanent-enrollment store; add a target-local mode-0600 journal and a shared settings-writer lock; expose three narrowly authenticated service routes plus three Controller-owner routes; and add status-driven controls to Remote > Registered Machines. The Controller starts only one fresh dispatch at a time, parks an uncertain target after a 15-second scheduling wait, and continues different Macs. The target makes at most one live-process atomic replacement of `controller_settings.json`; restart only classifies hashes and never resumes a forward write.

**Tech Stack:** Python 3.12 standard library (`sqlite3`, `secrets`, `hashlib`, `fcntl`, `tempfile`, `os.replace`, `socket`, `ssl`, `http.client`), existing FastAPI/Pydantic/httpx runtime, plain HTML/CSS/JavaScript, and pytest. Add no dependency and change no launcher, sibling Studio, model, generation, GenStudio, or capability schema.

## Global Constraints

- Start from approved design commit `b1dc1df82d938280e18e61ded0a948e3f292131f`. Preserve that Studio Hub `2.8.2` design-only commit; do not amend, squash, or describe it as shipped repair behavior.
- The implementation is Studio Hub `2.9.0`, a minor release because it adds owner API and UI. Update `VERSION`, `CHANGELOG.md`, and frontend `RELEASE_NOTES` together on `2026-08-16`.
- This Mac is development/control only. Every command in this plan is a local source/test/read-only Git command. Do not call a live Hub, alter fleet data, update/restart/enroll/repair a real Mac, deploy, push, or run a canary.
- A later canary or rollout is owner-performed outside this plan through a separately authorized manual or overnight update. The handoff may contain a read-only checklist but no live-action procedure or command.
- Keep the current permanent enrollment code stable and reusable until its existing rotate/revoke actions are explicitly used. Repair never reads, returns, transports, rotates, revokes, or increments it.
- Keep `studiohub.site-capabilities` v3 and all managed-release state names and durable keys unchanged. Repair may only call the existing peer invalidation and managed-reconciliation wake interfaces after exact confirmation.
- General Controller-to-Agent dashboard SSO and broad hardening of historical fleet-token routes remain separate non-goals. New repair routes receive their own strict checks without changing old-route middleware behavior.
- Registry re-key/merge and delete/re-enroll are not repair behavior. Delete/re-enroll remains emergency-only and requires separate cleanup of old durable managed-release rows, especially during an active degraded release.
- Repair never returns or writes a fleet token. Eligibility, coordinator, and all three service routes use a read-only current-token accessor that never creates, writes, or chmods; absent/empty fails closed. `apply`, `redeem`, and `status` independently require the exact current fleet value in `X-Hub-Token`; every fleet-token file must remain absent or byte/mode-identical.
- The only forward mutation is one mode-0600 staged replacement of `controller_settings.json`, preserving every unlisted key and changing only `role`, `site_id`, `site_name`, `controller_id`, and `parent_controller_url`.
- No forward apply survives restart. A restarted executor classifies current hash as staged/exact `complete`, preimage/exact `never_applied`, or third state `needs_review`, and performs no write.
- The 15-second interval is Controller scheduling/UI only. It is absent from the claim and Agent journal and never revokes a paused live executor's one accepted write.
- All Studio Hub-managed identity/parent writers use one re-entrant local OS/file lock. Setup/join holds one outer acquisition from before its multi-file snapshot through every local side effect and, on failure, through `_restore` plus settings-cache reload. Enrollment-code file/SQLite rollback remains owned by `create_enrollment_code()` inside that lock; setup neither snapshots those stores nor promises to reverse a committed code transaction. Arbitrary manual file edits while Hub runs are unsupported; any manual edit/cleanup must first stop and verify the Hub process cannot execute, then use the same lock and read-only recovery classifier. Tests cover the final defense-in-depth recheck and recovery classification, not an impossible filesystem compare-and-swap guarantee.
- Controller role/site/site-name/controller mutations and exact-target registry membership/key/host/address mutations remain fenced while a redeemed/verifying/confirmation-pending request could still write. Fleet-token rotation, unrelated registry rows, later-Mac dispatch, and ordinary work remain unblocked.
- Permanent-code enrollment that would touch the exact live-claim target is fenced before code consumption or registry/profile/label mutation; an unrelated new machine remains enrollable. Registry reload can flag `registry_changed_pending` but cannot release the live fence.
- The coordinator's short mutation lock covers only state revalidation and local writes. Discover/refetch probes and fleet-sync remote update/verify I/O complete outside it; only the final local fleet-token commit is briefly serialized.
- Use no production network in tests. Inject resolvers, clocks, connected private transports, update probes, and status clients.
- Rendered UI QA is mandatory at 1440×900 and 390×844 through a loopback-only offline mock serving the shipped frontend. It must never start or contact a live Hub/fleet; inability to complete keyboard/layout/polling checks blocks the 2.9.0 commit.
- Because `AGENTS.md` requires every commit to be a versioned release, Tasks 1-13 end in task-scoped staged/review checkpoints, not commits. Task 14 creates exactly one reviewed `2.9.0` commit containing the plan and complete implementation. This repository rule overrides the generic skill preference for frequent commits.

## File Structure and Ownership

| File | Responsibility |
| --- | --- |
| `app/backend/enrollment_repair_store.py` | Controller-only SQLite schema, digest-only tickets, batch/request transitions, scheduling slot, per-target lock, and mutation fences |
| `app/backend/controller_settings_lock.py` | re-entrant in-process plus OS/file lock shared by all managed identity/parent writers |
| `app/backend/enrollment_repair_transport.py` | credential-free private-origin normalization and one-address pinned JSON connection used only by repair |
| `app/backend/enrollment_repair_executor.py` | target dispatch validation, journal, pinned callback, one-file apply, terminal status, and read-only restart classification |
| `app/backend/enrollment_repair.py` | identity snapshots, registry eligibility, canonical private transport, coordinator, bootstrap gates, status adoption, and reconciliation wake |
| `app/backend/auth.py` | exact-header service-route authentication helper; existing broad middleware stays unchanged |
| `app/backend/peers.py` | read-only current fleet-token accessor and fleet-sync local-commit seam; legacy generating accessor remains unchanged for non-repair callers |
| `app/backend/control_plane.py` | route every settings identity/parent save through the shared writer lock; expose exact cache reload after repair |
| `app/backend/enrollment.py` | outer re-entrant setup/join snapshot, side-effect, rollback, and cache-reload transaction; permanent-code semantics stay unchanged |
| `app/backend/registry.py` | exact machine host/address snapshot helper; registry data format stays unchanged |
| `app/backend/main.py` | Pydantic bodies, six routes, owner/service boundaries, lifecycle, enrollment/registry fences, reload notification, bounded mutation scopes, and reconciler wake wiring |
| `app/frontend/index.html` | Controller-only Remote controls, dialog, status polling, accessibility, responsive layout, and 2.9.0 release note |
| `app/tests/test_enrollment_repair_store.py` | schema, state machine, digest/redaction, idempotency, restart, scheduling, and fences |
| `app/tests/test_enrollment_repair_executor.py` | three-state journal, callback binding, single replacement, locking, crash, and preservation |
| `app/tests/test_enrollment_repair_coordinator.py` | eligibility, dispatch, bootstrap gates, batch continuation, adoption, and reconciliation wake |
| `app/tests/test_enrollment_repair_api.py` | owner auth and independent strict auth/source checks for all three service routes |
| `app/tests/test_peers.py` | absent/empty/read-only token behavior and fleet-sync local-commit ordering |
| `app/tests/test_frontend_enrollment_repair.py` | copy, Controller-only visibility, polling stability, a11y, and responsive structure |
| `app/tests/browser_fixtures/enrollment_repair_remote_fixture.py` | localhost-only mock API server that renders the shipped dashboard without starting a Hub or contacting a fleet |
| `app/tests/conftest.py` | isolated repair DB/journal/lock cleanup and coordinator reset only |
| `README.md` | implemented API/operator contract and explicit emergency-only delete/re-enroll warning |
| `VERSION`, `CHANGELOG.md` | truthful 2.9.0 implementation release metadata |

Files with shared ownership are serialized: finish store before coordinator; finish lock and read-only credential accessor before executor/coordinator; finish backend route contracts before frontend; reserve `main.py`, `peers.py`, `test_peers.py`, `conftest.py`, `index.html`, and release metadata to one active writer at a time.

## Shared Interfaces

Use these signatures consistently; fresh agents must not invent parallel state types.

```python
# app/backend/enrollment_repair_store.py
REQUEST_STATES = (
    "queued", "checking", "hub_update_required", "updating_hub",
    "ticket_issued", "dispatched", "redeemed", "verifying",
    "confirmation_pending", "complete", "retryable", "needs_review",
)
TICKET_STATES = ("issued", "redeemed", "expired")

@dataclass(frozen=True)
class ControllerIdentity:
    role: str
    site_id: str
    site_name: str
    controller_id: str

@dataclass(frozen=True)
class TargetIdentity:
    machine: str
    registry_host: str
    resolved_address: str
    controller_url: str

class RepairStore:
    def __init__(self, path: Path = enrollment.DB_FILE, *, clock: Callable[[], float] = time.time) -> None: ...
    def create_or_adopt_batch(self, machines: Sequence[str]) -> dict[str, Any]: ...
    def batch(self, batch_id: str) -> dict[str, Any] | None: ...
    def claim_next_dispatch(self) -> dict[str, Any] | None: ...
    def issue_ticket(self, request_id: str, *, target: TargetIdentity,
                     controller: ControllerIdentity, fleet_token_digest: str,
                     ticket_digest: str, redemption_expires_at: float) -> None: ...
    def mark_dispatched(self, request_id: str) -> None: ...
    def redeem(self, request_id: str, *, ticket: str, redemption_expires_at: float,
               target_machine: str, direct_source: str,
               observed_identity: Mapping[str, Any], registry_snapshot: TargetIdentity,
               controller: ControllerIdentity, fleet_token_digest: str) -> dict[str, Any]: ...
    def park(self, request_id: str, *, error_code: str = "confirmation_pending") -> None: ...
    def adopt_status(self, request_id: str, status: Mapping[str, Any], *, direct_source: str) -> dict[str, Any]: ...
    def fail_before_claim(self, request_id: str, *, state: str, error_code: str) -> None: ...
    def resolve_preclaim_review(self, request_id: str, *, evidence_code: str) -> None: ...
    def recover_scheduling_slot(self) -> int: ...
    def mutation_blocker(self, *, machine: str | None = None) -> dict[str, Any] | None: ...
```

```python
# app/backend/controller_settings_lock.py
class SettingsWriterBusy(RuntimeError): ...

@contextmanager
def settings_writer_lock(*, blocking: bool = False) -> Iterator[None]: ...

# app/backend/control_plane.py
def save_settings(values: dict, *, new_database_url: str | None = None,
                  clear_database_url: bool = False,
                  writer_blocking: bool = False) -> dict: ...
def reload_settings_cache() -> dict: ...
```

```python
# app/backend/enrollment_repair_transport.py
@dataclass(frozen=True)
class ResolvedOrigin:
    origin: str
    address: str
    host_header: str

def resolve_private_origin(value: str, *, resolver: Callable[..., Any] = socket.getaddrinfo) -> ResolvedOrigin: ...

class PinnedJSONConnection:
    direct_peer: str
    local_address: str
    async def request_json(self, method: str, path: str, *,
                           headers: Mapping[str, str], body: Mapping[str, Any] | None,
                           timeout: float) -> tuple[int, dict[str, Any]]: ...

def open_pinned_json(origin: ResolvedOrigin) -> AsyncContextManager[PinnedJSONConnection]: ...

# app/backend/enrollment_repair_executor.py
JOURNAL_FILE = DATA_DIR / ".enrollment_repair_journal.json"
AGENT_STATES = (
    "accepted", "redemption_attempted", "applying",
    "complete", "never_applied", "needs_review",
)

class RepairExecutor:
    def __init__(self, *, journal_path: Path = JOURNAL_FILE,
                 settings_path: Path = control_plane.SETTINGS_FILE,
                 clock: Callable[[], float] = time.time,
                 origin_resolver: Callable[[str], ResolvedOrigin] = resolve_private_origin,
                 connection_factory: Callable[[ResolvedOrigin], AsyncContextManager[PinnedJSONConnection]] = open_pinned_json) -> None: ...
    async def apply(self, payload: Mapping[str, Any], *, direct_source: str) -> dict[str, Any]: ...
    def status(self, request_id: str, *, direct_source: str) -> dict[str, Any]: ...
    def recover(self) -> dict[str, Any] | None: ...
```

```python
# app/backend/enrollment_repair.py
NEW_ISSUANCE_ENABLED = True  # a rollback release may set false; service recovery remains available

class EnrollmentRepairCoordinator:
    def eligibility(self) -> dict[str, Any]: ...
    def create_batch(self, machines: Sequence[str]) -> dict[str, Any]: ...
    def batch(self, batch_id: str) -> dict[str, Any] | None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def redeem(self, payload: Mapping[str, Any], *, direct_source: str) -> dict[str, Any]: ...
    def require_controller_identity_mutable(self) -> None: ...
    def require_target_registry_mutable(self, machine: str) -> None: ...
    def require_enrollment_registration_mutable(self, machine: str, host: str) -> None: ...
    def note_registry_reload(self, registry_rows: Sequence[Mapping[str, Any]]) -> int: ...
    def controller_mutation(self, *, identity: bool = False,
                            machine: str | None = None) -> ContextManager[None]: ...
```

```python
# app/backend/auth.py
def require_exact_fleet_service_request(
    request: Request, *, expected_source: str | None = None,
) -> str: ...

# app/backend/peers.py
def current_fleet_token() -> str | None: ...

async def sync_fleet_token(
    registry: list[dict], client: httpx.AsyncClient, new_token: str,
    *, local_commit: Callable[[str], None] = set_fleet_token,
) -> dict: ...

# app/backend/main.py lifecycle state
app.state.enrollment_repair_store: RepairStore
app.state.enrollment_repair_executor: RepairExecutor
app.state.enrollment_repair_coordinator: EnrollmentRepairCoordinator

class EnrollmentRepairCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machines: list[str] = Field(min_length=1, max_length=500)

class RepairControllerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: str = Field(min_length=1, max_length=100)
    site_name: str = Field(min_length=1, max_length=120)
    controller_id: str = Field(min_length=1, max_length=100)

class EnrollmentRepairApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema: Literal["studiohub.enrollment-repair-dispatch"]
    schema_version: Literal[1]
    request_id: str = Field(min_length=16, max_length=128)
    target_machine_id: str = Field(min_length=1, max_length=100)
    ticket: str = Field(min_length=43, max_length=256)
    redemption_expires_at: float
    controller_url: str = Field(min_length=1, max_length=500)
    controller: RepairControllerBody

class RepairObservedIdentityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(min_length=1, max_length=20)
    site_id: str = Field(min_length=1, max_length=100)
    site_name: str = Field(min_length=1, max_length=120)
    controller_id: str = Field(min_length=1, max_length=100)
    parent_controller_url: str | None = Field(default=None, max_length=500)

class EnrollmentRepairRedeemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema: Literal["studiohub.enrollment-repair-redemption"]
    schema_version: Literal[1]
    request_id: str = Field(min_length=16, max_length=128)
    target_machine_id: str = Field(min_length=1, max_length=100)
    ticket: str = Field(min_length=43, max_length=256)
    redemption_expires_at: float
    observed_identity: RepairObservedIdentityBody
```

`open_pinned_json` is a small standard-library private transport: normalize first, resolve exactly one private address, connect only to that address, retain the original host for `Host` and TLS SNI/certificate verification, disable proxies and redirects by construction, expose the actual socket peer/local address, cap body size, and close after one response. It must not become a general HTTP client.

---

### Task 1: Add the durable Controller ticket and batch schema

**Files:**
- Create: `app/backend/enrollment_repair_store.py`
- Create: `app/tests/test_enrollment_repair_store.py`
- Modify: `app/tests/conftest.py`

**Interfaces:**
- Consumes: `enrollment.DB_FILE`, `ControllerIdentity`, `TargetIdentity`, injected wall clock.
- Produces: `RepairStore.__init__`, `create_or_adopt_batch`, `batch`, digest-only batch/request rows, and reset isolation.

- [ ] **Step 1: Write failing schema and secret-storage tests**

```python
def test_create_batch_is_stable_ordered_and_contains_no_credentials(store):
    batch = store.create_or_adopt_batch(["mac-z", "mac-a"])
    assert [row["target_machine"] for row in batch["requests"]] == ["mac-a", "mac-z"]
    assert batch["state"] == "queued"
    assert "ticket" not in json.dumps(batch)

def test_issue_ticket_persists_only_ticket_and_fleet_digests(store):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    store.issue_ticket(request_id, target=TARGET_A, controller=CONTROLLER,
                       fleet_token_digest=sha256_text("fleet-secret"),
                       ticket_digest=sha256_text("repair-secret"),
                       redemption_expires_at=1120.0)
    raw = store.path.read_bytes()
    assert b"repair-secret" not in raw
    assert b"fleet-secret" not in raw
```

- [ ] **Step 2: Run the tests and observe RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_store.py -k 'create_batch or issue_ticket'`

Expected: FAIL at collection because `backend.enrollment_repair_store` does not exist.

- [ ] **Step 3: Implement the minimal schema**

Use `CREATE TABLE/INDEX IF NOT EXISTS` in the existing `setup_enrollment.db`. Add `enrollment_repair_batches` and `enrollment_repair_requests`; store ordered targets, snapshots, digests, state, ticket status, timestamps, bounded error code, and sanitized evidence. Add partial unique indexes for one fresh scheduling slot and one unresolved request per target. Do not alter `enrollment_codes` or `.enrollment_code`.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS one_repair_dispatch_slot
ON enrollment_repair_requests(dispatch_slot)
WHERE dispatch_slot = 1;

CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_repair_per_target
ON enrollment_repair_requests(target_machine)
WHERE state NOT IN ('complete', 'retryable', 'hub_update_required');
```

- [ ] **Step 4: Add test cleanup without touching live state**

Add the repair DB sidecars/journal/lock to the existing temp-data reset and reset only module caches/tasks. Preserve the pre-import `STUDIOHUB_DATA_DIR` isolation.

- [ ] **Step 5: Run the focused store tests**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_store.py app/tests/test_enrollment.py -k 'batch or ticket or enrollment'`

Expected: PASS; permanent-code creation/use-count/rotation tests remain unchanged.

- [ ] **Step 6: Task checkpoint**

Stage only the three files, inspect `git diff --cached`, then unstage. Do not commit.

### Task 2: Implement atomic ticket transitions, idempotency, restart, and fences

**Files:**
- Modify: `app/backend/enrollment_repair_store.py`
- Modify: `app/tests/test_enrollment_repair_store.py`

**Interfaces:**
- Consumes: Task 1 schema and exact snapshot dataclasses.
- Produces: remaining `RepairStore` methods in Shared Interfaces, one-use redemption, scheduling park/recovery, status adoption, and mutation blockers.

- [ ] **Step 1: Write failing transition tests**

Add these exact tests:

- `test_redeem_is_single_use_and_checks_absolute_expiry`: first redemption at `1119.999` returns the exact claim, replay fails, and a separate ticket at `1120.0` expires without a claim.
- `test_duplicate_owner_batch_adopts_unresolved_target_without_new_ticket`: repeated `machines=["mac-a"]` returns the original request ID and leaves one request row.
- `test_restart_parks_orphaned_dispatch_slot_and_next_machine_can_start`: recovery changes the orphan to `confirmation_pending`, clears its scheduling slot, and selects `mac-b`.
- `test_same_target_stays_locked_while_different_target_claims_dispatch_slot`: a second `mac-a` request conflicts while `mac-b` remains selectable.
- `test_terminal_agent_evidence_releases_identity_and_membership_fence`: exact authenticated `complete`, `never_applied`, and `needs_review` each release the live-write fence, while only the first two permit the target's normal completion/retry path.
- `test_registry_changed_flag_does_not_release_live_claim_fence`: Controller-observed ambiguity changes safe evidence only; `mutation_blocker(machine="mac-a")` remains non-null.
- `test_exact_revalidation_resolves_only_preclaim_review`: a request with no issued/redeemed ticket may move from review to retryable after exact registry revalidation, while Agent-terminal or live-claim review cannot.

- [ ] **Step 2: Run the transition slice and observe RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_store.py -k 'redeem or duplicate or restart or fence or registry_changed'`

Expected: FAIL because transitions and blockers are not implemented.

- [ ] **Step 3: Implement every transition under `BEGIN IMMEDIATE`**

Use conditional `UPDATE ... WHERE state = ?` row counts as the authority. `issued` becomes only `redeemed` or `expired`; only the first pre-expiry redemption returns a claim. `recover_scheduling_slot()` parks an orphaned dispatched/redeemed/verifying request before clearing its slot. Terminal `complete`, `never_applied`, or authenticated `needs_review` releases the live-write fence; Agent-terminal/live-claim `needs_review` remains non-retryable until explicit reviewed cleanup. A pre-ticket ambiguity may move to retryable only after the coordinator proves the registry is now exact and records bounded resolution evidence. A Controller-observed registry change sets `registry_changed_pending` evidence without creating terminal Agent evidence.

- [ ] **Step 4: Prove the 15-second wait is not durable authority**

Add a test that `park()` changes only request scheduling state/slot; no claim, journal, authority deadline, recovery deadline, or cross-machine apply gate column exists.

- [ ] **Step 5: Run store and permanent-enrollment regressions**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_store.py app/tests/test_enrollment.py`

Expected: PASS, including boundary `now == redemption_expires_at` rejection and digest constant-time comparison.

- [ ] **Step 6: Task checkpoint**

Review the SQLite schema and every transaction against the design state vocabulary. Do not commit.

### Task 3: Add the shared settings-writer lock to every managed identity path

**Files:**
- Create: `app/backend/controller_settings_lock.py`
- Create: `app/tests/test_controller_settings_lock.py`
- Modify: `app/backend/control_plane.py`
- Modify: `app/backend/enrollment.py`
- Modify: `app/backend/main.py`
- Modify: `app/tests/test_control_plane.py`
- Modify: `app/tests/test_enrollment.py`

**Interfaces:**
- Consumes: existing `control_plane.save_settings`, setup/controller, setup/join, and manual Controller settings routes.
- Produces: re-entrant `settings_writer_lock`, `SettingsWriterBusy`, `writer_blocking`, `reload_settings_cache`, outer setup transactions, and HTTP 423 `settings_writer_busy` behavior.

- [ ] **Step 1: Write failing lock and route tests**

Add these exact tests:

- `test_second_thread_gets_settings_writer_busy`: a held repair lock makes a nonblocking second writer raise `SettingsWriterBusy`.
- `test_controller_settings_route_is_busy_while_repair_lock_is_held`: the route returns 423 with code `settings_writer_busy` and settings bytes remain unchanged.
- `test_setup_controller_and_join_are_busy_while_repair_lock_is_held`: both setup routes return the same bounded busy outcome without enrollment, registry, profile, or token changes.
- `test_nested_nonwriting_settings_reads_remain_available`: `load_settings()` and `public_settings()` remain read-only and available while the writer lock is held.
- `test_join_rollback_keeps_repair_busy_until_restore_and_cache_reload`: pause an injected profile-write failure after identity/token side effects; a concurrent repair gets `SettingsWriterBusy`; release the failure, assert `_restore` and `reload_settings_cache` finish before the lock opens, then assert disk/cache/token/profile/label bytes equal the pre-snapshot.
- `test_new_controller_existing_code_failure_preserves_code_and_holds_outer_lock`: seed an active permanent code, record its exact database row plus code-file bytes/mode, then pause and fail from the credential-status check after settings/profile/token/label writes but before any code create/rotate call. Assert repair and a second settings writer stay busy through outer `_restore` plus cache reload, all setup preimages return, and the existing code row/file are unchanged without having been part of the outer snapshot.
- `test_new_controller_code_precommit_failure_holds_outer_lock_through_internal_rollback`: begin with no active permanent code and use a test-only connection proxy to raise after the new code row is inserted but before `connection.commit()`. Wrap the existing code-file `_restore(saved_file)` to pause after its real restore finishes; while paused, assert repair and a second settings writer are still busy. Release it, then assert the inner SQLite transaction has rolled back, the code-file preimage is restored, and only afterward the outer setup rollback/cache reload completes and releases the lock.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_controller_settings_lock.py app/tests/test_control_plane.py app/tests/test_enrollment.py -k 'writer_busy or repair_lock or rollback or precommit'`

Expected: FAIL because managed settings writes are not serialized by an OS/file lock.

- [ ] **Step 3: Implement one minimal shared lock**

Combine a process-local `threading.RLock` with `fcntl.flock(LOCK_NB | LOCK_EX)` on a mode-0600 `controller_settings.json.repair.lock`. Track same-thread nesting so one outer acquisition owns one file descriptor/flock until its last nested exit; another thread/process remains nonblocking-busy. Never keep identity or secrets in the lock file. `save_settings` takes it before reading the current identity and holds it through settings/database writes and cache update. Repair later uses the same context but performs its own single-file replacement rather than calling generic setup.

- [ ] **Step 4: Map all managed writers**

Wrap each entire `configure_new_controller` and `configure_joined_agent` local transaction in one outer re-entrant `settings_writer_lock`: acquire before `_snapshot(_configuration_paths())`; retain it through nested `save_settings`, database cleanup, fleet-token/profile/label work, the existing-code status check, and any call to `create_enrollment_code`; on exception retain it through `_restore(snapshot)` and `control_plane.reload_settings_cache()`; release only after success or rollback is fully visible. Keep `_configuration_paths()` unchanged: the outer setup snapshot must not add the enrollment database or `ENROLLMENT_CODE_FILE`. `create_enrollment_code()` continues to own its existing code-file snapshot and SQLite transaction; the outer lock merely remains held until that nested operation either commits successfully or finishes its own rollback. Do not require or implement outer setup rollback of an already committed code transaction. The already-completed `claim_remote` network request remains outside this local lock. Route `PUT /api/hub/controller`, `POST /api/hub/setup/controller`, and `POST /api/hub/setup/join` through these locked functions. Confirm by `rg` that no other Studio Hub-managed code writes `SETTINGS_FILE` directly; if one exists, move it under this same lock without broad refactoring.

- [ ] **Step 5: Run regressions**

Run: `conda_env/bin/python -m pytest -q app/tests/test_controller_settings_lock.py app/tests/test_control_plane.py app/tests/test_enrollment.py app/tests/test_api.py -k 'controller or setup or join or settings or rollback'`

Expected: PASS. Existing database-mode, environment-lock, permanent-code, fleet-token, hardware-profile, and machine-label behavior remains unchanged outside lock-busy cases.

- [ ] **Step 6: Task checkpoint**

Review lock acquisition order and prove the setup snapshot, local setup writes, nested enrollment-code transaction/its self-owned rollback, outer `_restore`, and cache reload are inside one outer lock while all network I/O is outside it. Confirm the outer snapshot still excludes the enrollment database/code file and no test expects it to reverse an already committed code transaction. Do not commit.

### Task 4: Add a non-mutating current fleet-token accessor

**Files:**
- Modify: `app/backend/peers.py`
- Modify: `app/tests/test_peers.py`

**Interfaces:**
- Consumes: existing `FLEET_TOKEN_FILE`, `STUDIOHUB_FLEET_TOKEN`, legacy `fleet_token()`, `set_fleet_token()`, and `sync_fleet_token()`.
- Produces: `current_fleet_token() -> str | None` and the keyword-only `local_commit` seam on `sync_fleet_token`; later repair code must use only the read-only accessor.

- [ ] **Step 1: Write failing read-only accessor tests**

Add these exact tests:

- `test_current_fleet_token_missing_returns_none_without_creating_files`: remove both token files, call twice, assert `None` and both paths remain absent.
- `test_empty_current_fleet_token_is_read_only`: seed exact empty/whitespace bytes and a known mode, call `current_fleet_token()` twice, and assert `None` plus unchanged bytes/stat mode and no newly created token path.
- `test_current_fleet_token_reads_existing_or_environment_value_without_side_effects`: assert the stripped current value and byte/stat equality for every token file.
- `test_legacy_fleet_token_generation_is_unchanged_for_nonrepair_callers`: retain current generation behavior only through the existing accessor.
- `test_sync_fleet_token_invokes_local_commit_once_after_network_work`: injected peer requests finish before the callback, and the callback receives the new value exactly once.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_peers.py -k 'current_fleet_token or local_commit'`

Expected: FAIL because the read-only accessor and sync seam do not exist.

- [ ] **Step 3: Implement the read-only accessor**

Read a non-empty `STUDIOHUB_FLEET_TOKEN` first; otherwise read `FLEET_TOKEN_FILE` if it exists and return its stripped non-empty value. Catch read errors as unavailable. Do not call `fleet_token`, `set_fleet_token`, `touch`, `chmod`, `write_text`, or access `SHARED_STUDIO_TOKEN_FILE`.

- [ ] **Step 4: Add the fleet-sync local commit seam**

Keep all existing remote synchronization/verification order. Replace only the final direct `set_fleet_token(new_token)` call with injected `local_commit(new_token)`; default it to `set_fleet_token` so every non-repair caller is unchanged. Task 11 will pass a callback that takes the coordinator's short mutation lock only for this final local write/cache clear.

- [ ] **Step 5: Run peer regressions**

Run: `conda_env/bin/python -m pytest -q app/tests/test_peers.py`

Expected: PASS; missing/empty reads create nothing and legacy generation/synchronization behavior remains covered.

- [ ] **Step 6: Task checkpoint**

Search repair modules and later tasks for `peers.fleet_token()`; the only permitted repair credential source is `peers.current_fleet_token()`. Do not commit.

### Task 5: Build target dispatch validation, durable redemption attempt, and strict callback transport

**Files:**
- Create: `app/backend/enrollment_repair_transport.py`
- Create: `app/backend/enrollment_repair_executor.py`
- Create: `app/tests/test_enrollment_repair_executor.py`
- Modify: `app/tests/conftest.py`

**Interfaces:**
- Consumes: Task 3 shared lock, Task 4 read-only current-token contract, and injected resolver/callback.
- Produces: `ResolvedOrigin`, `resolve_private_origin`, one-purpose pinned JSON transport, `RepairExecutor.apply`, accepted/redemption_attempted journal states, repeated-dispatch adoption, and callback validation.

- [ ] **Step 1: Write failing dispatch/callback tests**

Add these exact async tests:

- `test_wrong_role_and_missing_parent_are_repairable`: standalone and controller preimages with no parent reach the injected redeem callback when registry target and source bind.
- `test_wrong_saved_parent_is_optional_audit_evidence`: a stale saved origin appears only as sanitized mismatch evidence and the dispatch origin is used.
- `test_callback_origin_must_resolve_to_direct_dispatch_peer`: `100.70.0.1` succeeds only when it is also the direct dispatch peer.
- `test_public_multi_address_credentialed_path_query_and_fragment_origins_fail`: each invalid origin returns `callback_url_invalid` before callback or redemption.
- `test_redirect_and_proxy_environment_are_never_used`: a 302 is terminal and injected proxy environment is not consulted.
- `test_lost_response_before_claim_save_expires_to_never_applied`: the durable journal first says `redemption_attempted/outcome_unknown`; at the bound it contains no ticket and reports `never_applied`, with unchanged settings.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_executor.py -k 'parent or callback or redirect or lost_response'`

Expected: FAIL because the executor and journal do not exist.

- [ ] **Step 3: Implement bounded dispatch parsing and private-origin resolution**

Require schema `studiohub.enrollment-repair-dispatch` version 1, bounded IDs/names, exact target, private direct peer, absolute first-redemption expiry, no environment-locked identity, and current `database_mode == "off"`. Saved parent and current role are audit evidence only. A different unresolved request returns `request_conflict`; an identical request adopts the journal.

- [ ] **Step 4: Fsync uncertainty before callback**

Read/hash `controller_settings.json`, atomically save mode-0600 journal state `redemption_attempted` with `outcome_unknown`, plaintext ticket, absolute expiry, validated origin/address, request/target/controller snapshots, and preimage hash, then fsync file and directory. Scrub the ticket on durable claim acceptance or expiry. If no claim was durably saved, never stage settings and classify `never_applied` at expiry.

- [ ] **Step 5: Implement the one-purpose pinned transport**

Use a standard-library connection pinned to the one resolved private address, original host for `Host` and TLS SNI, `Connection: close`, explicit size/time bounds, and no redirect/proxy behavior. Expose direct socket peer/local address for the caller's source checks. Do not use request headers such as `Forwarded` or `X-Forwarded-For`.

- [ ] **Step 6: Run executor validation tests**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_executor.py -k 'dispatch or parent or role or callback or response or expiry'`

Expected: PASS. Journal mode is 0600 and captured logs/exceptions contain neither ticket nor claim.

- [ ] **Step 7: Task checkpoint**

Read-only security review may independently inspect origin normalization, socket binding, size bounds, and redaction now. It must report findings only and must not edit shared files while this task is active. Do not commit.

### Task 6: Implement the one-file apply and read-only crash recovery

**Files:**
- Modify: `app/backend/enrollment_repair_executor.py`
- Modify: `app/backend/control_plane.py`
- Modify: `app/tests/test_enrollment_repair_executor.py`
- Modify: `app/tests/test_control_plane.py`

**Interfaces:**
- Consumes: exact claim response and Task 3 lock.
- Produces: one live-process `os.replace`, preimage/staged hash journal, terminal status, `reload_settings_cache`, and restart classification.

- [ ] **Step 1: Write failing mutation/recovery tests**

Add these exact tests:

- `test_apply_changes_only_five_identity_parent_fields`: compare the parsed preimage and result, asserting the changed-key set equals the approved five and file mode is 0600.
- `test_fleet_token_files_are_byte_identical_after_every_outcome`: compare bytes for both token files after complete, never-applied, needs-review, and injected failures.
- `test_crash_before_replace_recovers_never_applied_without_write`: recovery observes the exact preimage and changes journal state only.
- `test_crash_after_verified_replace_recovers_complete_without_write`: recovery observes staged/exact bytes, reports complete, and never calls the injected write/replace functions.
- `test_third_state_before_replace_or_on_restart_is_needs_review_without_overwrite`: both supported third-state observations retain the third bytes and report review.
- `test_concurrent_settings_and_join_writers_return_busy`: pause a repair after durable claim acceptance and prove settings/setup/join writes fail busy until terminal.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_executor.py -k 'five_identity or fleet_token or crash or third_state or concurrent'`

Expected: FAIL because claim acceptance does not yet mutate or classify settings.

- [ ] **Step 3: Validate and durably accept only the exact claim**

Require schema `studiohub.enrollment-repair-claim` version 1, same request/target, `role="agent"`, Controller snapshot site fields, and `controller_id == target_machine_id`. Reject unknown fields with a strict Pydantic model or explicit exact-key set. Under the settings lock, re-read the preimage hash, durably save the validated claim with `state="applying"` and scrub the ticket, then keep the lock through the one replacement.

- [ ] **Step 4: Stage and replace one full settings file**

Copy the raw settings object, replace only the five approved keys, preserve every other key, write a mode-0600 temporary sibling, fsync it, journal preimage/staged hashes, re-read the current preimage as defense in depth, call one `os.replace`, fsync the directory, and verify exact bytes/hash from disk. Do not call `configure_joined_agent`, `save_settings`, registry, labels, profiles, database, enrollment, or fleet-token helpers.

- [ ] **Step 5: Implement restart classification only**

`recover()` never uses a saved claim to write. Staged hash plus exact read-back becomes `complete`; preimage hash becomes `never_applied`; every other hash becomes `needs_review`. Scrub ticket/claim material in terminal journal while retaining sanitized hashes/evidence/status indefinitely.

- [ ] **Step 6: Add failure-injection and paused-process semantics**

Inject failures at stage write, file fsync, pre-replace recheck, replace, directory fsync, and read-back. Simulate a live pause immediately before replace with an event: the lock remains held and resume may make the one write. Do not assert that an arbitrary external writer cannot win the microgap; test only a third state already visible at final recheck or at restart.

- [ ] **Step 7: Run executor and settings regressions**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_executor.py app/tests/test_control_plane.py app/tests/test_enrollment.py`

Expected: PASS, with no rollback write and no forward apply on restart.

- [ ] **Step 8: Task checkpoint**

Inspect the diff for any second settings-file mutation or fleet-token call. Do not commit.

### Task 7: Implement eligibility, ordered batches, and owner-visible ambiguity

**Files:**
- Create: `app/backend/enrollment_repair.py`
- Create: `app/tests/test_enrollment_repair_coordinator.py`
- Modify: `app/backend/registry.py`
- Modify: `app/tests/test_registry.py`

**Interfaces:**
- Consumes: registry rows, peer cache, Task 2 store, Task 5 `resolve_private_origin`, exact target snapshots, and injected clock.
- Produces: `EnrollmentRepairCoordinator.eligibility`, `create_batch`, and `batch`.

- [ ] **Step 1: Write failing identity-resolution tests**

Add these exact tests:

- `test_eligibility_requires_one_remote_machine_one_host_one_private_address`: only one stable remote key with one host resolving to one private address is eligible.
- `test_duplicate_host_and_multi_host_machine_are_needs_review_not_guessed`: both shapes return explicit stable codes and issue no ticket.
- `test_local_unknown_and_duplicate_machine_ids_are_excluded`: local and missing IDs are rejected and duplicate requested IDs collapse to one.
- `test_missing_or_empty_current_fleet_token_is_ineligible_without_creating_one`: both conditions return `fleet_token_missing`, issue no request/ticket, and preserve absent/exact token-file bytes and modes.
- `test_batch_re_evaluates_server_side_and_orders_by_stable_machine_id`: a stale client eligible list cannot bypass a changed registry, and accepted targets sort by stable ID.
- `test_double_click_adopts_same_unresolved_request`: repeated creation returns the same request ID and ticket count stays zero before execution.
- `test_resolved_preclaim_ambiguity_can_retry_without_unlocking_agent_review`: exact current registry evidence resolves only a never-issued ambiguity; a live/Agent review stays locked.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py -k 'eligibility or duplicate or orders or double_click'`

Expected: FAIL because coordinator eligibility does not exist.

- [ ] **Step 3: Add a credential-free registry snapshot helper**

Return exact machine key, one registry host, Studio endpoint identities, and one resolved private address. Reject local, missing, public, ambiguous, multi-address, shared-host, changed-address, and conflicting stable-ID cases with stable sanitized codes. Do not rename, merge, re-key, probe Studios, or mutate registry.

- [ ] **Step 4: Implement owner-visible eligibility and idempotent batches**

Return top-level `issuance_enabled` plus every registered remote machine with `eligible`, stable code, safe detail, display label, host, and current request state. Read transport authority only through `peers.current_fleet_token()`; absent/empty returns `fleet_token_missing` without filesystem mutation. Server-side creation de-duplicates requested IDs, sorts by stable ID, adopts unresolved same-target rows, and creates no ticket until execution-time validation succeeds. If an earlier never-issued review is now disproved by exact unambiguous registry evidence, record that resolution and create the retry; never use this path for an issued, redeemed, live, or Agent-terminal review.

- [ ] **Step 5: Run registry/coordinator tests**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py app/tests/test_registry.py`

Expected: PASS. Registry bytes and labels/profiles/flags are unchanged by eligibility and batch creation.

- [ ] **Step 6: Task checkpoint**

Read-only identity-resolution review may compare every ambiguity path with the approved design. Report only; do not edit shared files concurrently. Do not commit.

### Task 8: Mint, dispatch, and atomically redeem a target-bound ticket

**Files:**
- Modify: `app/backend/enrollment_repair.py`
- Modify: `app/backend/enrollment_repair_store.py`
- Modify: `app/tests/test_enrollment_repair_coordinator.py`
- Modify: `app/tests/test_enrollment_repair_store.py`

**Interfaces:**
- Consumes: Tasks 2 and 4-7; injected connected private transport and exact registry/Controller/token snapshots.
- Produces: one committed digest-only issuance before dispatch and atomic `EnrollmentRepairCoordinator.redeem`.

- [ ] **Step 1: Write failing issuance/redemption tests**

Add these exact async tests:

- `test_controller_stores_digest_before_dispatch_and_plaintext_only_in_memory`: inspect the DB inside the injected dispatch callback, prove snapshots/digest are committed and plaintext is absent.
- `test_dispatch_uses_same_socket_that_selected_controller_origin`: the recorded Controller origin matches the connection's actual local address and the target direct peer matches its issued snapshot.
- `test_redeem_rejects_each_changed_request_target_registry_source_controller_and_token_binding`: each parameterized mismatch returns no claim and leaves the ticket unredeemed or safely failed as specified.
- `test_redeem_returns_exact_claim_once_before_expiry`: assert the exact eight-key claim, no fleet token/timer/URL, one digest consumption, replay failure, and equality-bound expiry failure.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py -k 'digest_before_dispatch or same_socket or redeem'`

Expected: FAIL because connected dispatch and Controller redemption are not implemented.

- [ ] **Step 3: Implement connected-socket issuance**

Read the exact existing credential through `peers.current_fleet_token()` and fail `fleet_token_missing` without creating/chmodding a file when absent. Open the target connection and read its exact direct peer plus Controller-side local socket address. Under the coordinator's short foreground lock, re-read that same current credential, select the matching canonical Controller origin, create a 256-bit URL-safe ticket, and—before writing request bytes on that same connection—commit its digest, snapshots, and absolute `now + 120` first-redemption bound. Hold plaintext only in stack memory and redact the whole body/headers.

- [ ] **Step 4: Implement atomic Controller redemption**

Under the same short foreground lock, re-read the credential only through `peers.current_fleet_token()` and fail without changing ticket state if it is absent/empty. Re-resolve and compare the unchanged target registry snapshot, exact direct source, Controller role/site/name/ID snapshot, current fleet-token digest, request/target/expiry, ticket digest/state, and no competing completed request inside `BEGIN IMMEDIATE`. Return only the exact eight claim keys in the design and never replay it. Registry and final local fleet-token mutations take this lock briefly, so none can interleave with the issue/redeem snapshot transaction.

- [ ] **Step 5: Run issuance/redemption tests**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py app/tests/test_enrollment_repair_store.py -k 'dispatch or redeem or ticket'`

Expected: PASS, including source mismatch, registry ambiguity/change, Controller snapshot change, token mismatch, expiry, replay, and redaction.

- [ ] **Step 6: Task checkpoint**

Review the exact dispatch/claim field sets against the design. Do not commit.

### Task 9: Park uncertainty, continue later Macs, adopt status, and wake reconciliation

**Files:**
- Modify: `app/backend/enrollment_repair.py`
- Modify: `app/backend/enrollment_repair_store.py`
- Modify: `app/tests/test_enrollment_repair_coordinator.py`
- Modify: `app/tests/test_enrollment_repair_store.py`

**Interfaces:**
- Consumes: Task 8 dispatch, exact target status, `peers.invalidate`, and `ReleaseReconciler.wake_peer`.
- Produces: coordinator `start`/`stop`, one fresh scheduling slot, 15-second park, restart adoption, and exact terminal wake.

- [ ] **Step 1: Write failing scheduling/adoption tests**

Add these exact async tests:

- `test_foreground_wait_parks_uncertain_mac_then_dispatches_next`: advance the fake clock 15 seconds, assert `mac-a` parks and `mac-b` dispatches.
- `test_unresolved_same_target_gets_no_second_ticket_while_later_mac_proceeds`: compare ticket-generator call count and request IDs across overlapping batches.
- `test_status_adoption_confirms_exact_identity_then_wakes_existing_release_once`: exact complete status invalidates the peer and calls `wake_peer("mac-a")` once; mismatched identity/source does neither.
- `test_restart_parks_issued_or_inflight_request_before_continuing`: a new coordinator adopts durable rows, reconstructs no ticket, parks uncertainty, and advances order.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py -k 'parks or unresolved_same_target or status_adoption or restart'`

Expected: FAIL because scheduling lifecycle and adoption are not implemented.

- [ ] **Step 3: Implement the fresh-dispatch slot and bounded park**

Select only one queued request under the partial unique slot. End foreground scheduling on exact terminal response or after 15 seconds by calling `park()`, clearing only that scheduling slot, and considering the next stable machine. The wait never enters the claim/journal and never revokes Agent write authority.

- [ ] **Step 4: Implement restart and background status adoption**

On start, park orphaned issued/dispatched/redeemed/verifying slots without reconstructing a ticket, resume queued order, and probe target-bound status for parked requests. Require request, direct source, registry host, and four identity fields. `complete` persists once, invalidates peer cache, and wakes the same machine once; `never_applied` becomes retryable; authenticated `needs_review` releases live-write fencing but remains review-only.

- [ ] **Step 5: Run scheduling/adoption regressions**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py app/tests/test_enrollment_repair_store.py app/tests/test_release_reconciliation.py -k 'repair or wake_peer or registry'`

Expected: PASS, including no second same-target ticket, no global recovery gate, and exactly one reconciliation wake after exact confirmation.

- [ ] **Step 6: Task checkpoint**

Review all scheduling language and tests: one fresh dispatch at a time, not one global active recovery; no recovery deadline, apply timer, or day-long gate. Do not commit.

### Task 10: Add safe older-Hub bootstrap classification and narrow mutation fences

**Files:**
- Modify: `app/backend/enrollment_repair.py`
- Modify: `app/tests/test_enrollment_repair_coordinator.py`

**Interfaces:**
- Consumes: injected read-only capability/status gates, mocked existing ordinary Hub updater, and Task 2 `mutation_blocker`.
- Produces: `hub_update_required`/safe bootstrap classification and Controller/exact-target fence guards.

- [ ] **Step 1: Write failing bootstrap tests**

Add `test_older_hub_bootstrap_requires_every_safe_gate`, parameterized so each missing gate prevents the mocked updater call; add `test_safe_older_hub_bootstrap_updates_only_hub_once_then_reprobes`; add `test_active_or_degraded_managed_release_never_uses_moving_main_bootstrap`.

- [ ] **Step 2: Write failing fence/pause tests**

Add these exact async tests:

- `test_paused_before_replace_blocks_controller_role_site_name_and_id_changes`: each bound identity mutation returns busy while paused and succeeds after authenticated terminal evidence.
- `test_paused_before_replace_blocks_target_delete_rekey_host_and_address_change`: each exact-target mutation returns busy; resume writes the still-current claim and terminal status releases the fence.
- `test_unrelated_registry_change_and_fleet_token_rotation_are_not_fenced`: unrelated mutation and token rotation proceed while the bound target is paused; an old token may make later status adoption require manual credential recovery but cannot stale the unchanged identity claim.
- `test_registry_ambiguity_flags_but_does_not_release_live_claim_fence`: ambiguity is visible but bound identity/target mutation stays busy.
- `test_registry_reload_marks_exact_live_target_changed_without_releasing_fence`: feed a host/address/key change to `note_registry_reload`, assert `registry_changed_pending` sanitized evidence is durable, request remains nonterminal, and both identity/exact-target blockers remain.
- `test_registry_reload_for_unrelated_machine_does_not_flag_live_target`: unrelated membership changes leave the bound snapshot/fence evidence unchanged.
- `test_cleanup_cannot_release_fence_until_target_process_stop_is_verified`: false/unknown verifier results keep the fence; verified stop plus shared-lock classification releases it.

- [ ] **Step 3: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py -k 'older_hub or bootstrap or paused or fence or cleanup'`

Expected: FAIL because bootstrap classification and coordinator fence guards are not implemented.

- [ ] **Step 4: Implement source-only older-Hub bootstrap with fakes**

A capability 404/schema miss may call the existing ordinary Agent-Hub update path only when all design gates are independently true: exact unambiguous target/token, supported update/restart verification, idle Hub/Studios, enabled machine, no active/degraded managed release containing it, no conflicting maintenance/update, and published Hub version known to contain repair schema 1. Re-probe once. Any missing gate returns `hub_update_required`; never update Image/Voice or use moving-main inside an active/degraded immutable release. This plan executes only mocked tests, never a live update.

- [ ] **Step 5: Implement narrow mutation fences**

Guard Controller role/site/site-name/controller changes and exact-target delete/re-key/host/address paths. `require_enrollment_registration_mutable(machine, host)` resolves all existing registry rows whose exact key or bound host/address the proposed registration could touch; it rejects when any has a live claim and allows a genuinely unrelated new machine. `note_registry_reload()` compares every live accepted-claim target with its durable key/host/address snapshot; a change records only `registry_changed_pending` bounded evidence and never synthesizes terminal Agent evidence or releases a fence. Keep the fence through redeemed/verifying/confirmation_pending until authenticated `complete`, `never_applied`, or `needs_review`, or an API-free recovery hook receives verified process-stop plus shared-lock classification evidence. Do not fence fleet-token rotation, unrelated registry rows, ordinary work, or later-machine repairs.

- [ ] **Step 6: Run bootstrap/fence tests**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_coordinator.py app/tests/test_release_reconciliation.py -k 'bootstrap or fence or paused or registry_reload or managed_release'`

Expected: PASS, including pause/resume valid unchanged claim, later Mac continuation, and Controller mutations proceeding only after terminal evidence.

- [ ] **Step 7: Task checkpoint**

Confirm the test suite used only injected fakes and made no live update, restart, enrollment, repair, or fleet call. Do not commit.

### Task 11: Expose strict owner and three-service APIs and wire lifecycle

**Files:**
- Modify: `app/backend/auth.py`
- Modify: `app/backend/main.py`
- Create: `app/tests/test_enrollment_repair_api.py`
- Modify: `app/tests/test_auth.py`
- Modify: `app/tests/test_api.py`
- Modify: `app/tests/test_peers.py`
- Modify: `app/tests/conftest.py`

**Interfaces:**
- Consumes: coordinator/executor shared interfaces and existing browser session, loopback, middleware, monitor, and reconciler.
- Produces: three owner routes, three service routes, exact request models, independent strict auth/source checks, lifespan start/stop/recovery, fenced mutation routes, and `_reload_registry_and_note_repair() -> None`.

- [ ] **Step 1: Write failing owner-route tests**

Add these exact tests:

- `test_controller_loopback_and_valid_owner_session_can_create_repair`: each approved owner context gets 202 and the same sanitized batch shape.
- `test_hub_token_fleet_token_agent_session_missing_session_and_noncontroller_cannot_create_repair`: parameterized substitutes fail before a batch row exists.
- `test_cross_origin_owner_write_is_rejected_before_batch_creation`: a valid session with a foreign Origin fails existing same-origin protection and creates no row.
- `test_eligibility_static_route_precedes_dynamic_batch_route`: the literal path returns eligibility rather than a batch lookup.
- `test_owner_batch_get_is_sanitized`: recursively assert no ticket/token/claim/credential key or value appears.

- [ ] **Step 2: Write the strict three-route credential matrix**

Parameterize `POST /api/hub/enrollment-repair/apply`, `POST /api/hub/enrollment-repair-tickets/redeem`, and `GET /api/hub/enrollment-repair/status/request-a`. For each, prove exact current fleet header plus expected private direct source succeeds, while missing/wrong fleet header, unique Hub token, owner/Agent cookie, Authorization bearer, query credential, permanent code, public peer, wrong peer, and forwarded-host spoof fail before state read/mutation.

Add `test_all_three_service_routes_fail_closed_when_current_fleet_token_is_absent_or_empty_without_creating_it`: parameterize absent and exact empty/whitespace file states, call all three routes, assert 503 `fleet_token_unavailable` before journal/DB access, then assert token paths/bytes/modes are identical. A present but wrong header remains 401.

- [ ] **Step 3: Write failing enrollment-claim, reload, and lock-scope tests**

Add these exact tests:

- `test_paused_claim_blocks_same_target_enrollment_before_permanent_code_consume`: seed a valid permanent code and live target claim, POST enrollment for that machine/host, assert 423, unchanged use count/last-used time, and byte/semantic equality for registry, labels, profile, endpoints, and code state.
- `test_paused_claim_allows_unrelated_new_machine_enrollment`: with `mac-a` paused, enroll `mac-b` at a distinct private host and assert normal code use/registration while `mac-a` remains fenced.
- `test_enrollment_claim_revalidates_target_inside_lock_before_code_consume`: inject a registry change between initial body parsing and lock entry; assert revalidation blocks before code use or metadata mutation.
- `test_every_main_registry_reload_notifies_repair_coordinator`: exercise add/discover/remove/refetch registration paths and assert the post-reload snapshot is passed once to `note_registry_reload`.
- `test_discover_and_refetch_network_io_precedes_short_controller_mutation_lock`: record fake probe events and lock enter/exit, asserting every await/network event completes before local revalidation/add/reload/profile work enters the lock.
- `test_fleet_sync_network_io_precedes_short_local_token_commit_lock`: record remote sync/verify events before the injected local commit takes `controller_mutation`; assert the durable live-claim fence does not reject fleet rotation.

- [ ] **Step 4: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_api.py app/tests/test_auth.py app/tests/test_api.py app/tests/test_peers.py -k 'repair or exact_fleet or paused_claim or controller_mutation_lock or registry_reload'`

Expected: FAIL because routes, strict helper, enrollment pre-consume fence, reload notification, and bounded lock scopes do not exist.

- [ ] **Step 5: Add strict models and route-local authentication**

Use Pydantic models with `extra="forbid"` and bounded fields. Owner initiation accepts only loopback or the Controller's valid browser session and relies on existing same-origin write protection; unlike `_can_manage_enrollment`, it never accepts `HUB_TOKEN`. `require_exact_fleet_service_request` reads only `peers.current_fleet_token()`, fails closed when absent/empty, then checks only `X-Hub-Token`, exact current fleet value, private `request.client.host`, expected source, and absence of cookies/Authorization/query credentials. Do not call generating `peers.fleet_token()`, trust broad middleware, or trust forwarded headers.

- [ ] **Step 6: Register routes in unambiguous order**

Register `GET /api/hub/enrollment-repairs/eligibility` before `GET /api/hub/enrollment-repairs/{batch_id}`. Owner POST returns 202 when issuance is enabled and 503 `repair_issuance_disabled` when a rollback release disables new work. Map stable service errors to bounded HTTP 400/401/403/409/410/423/503 responses while keeping durable rows/journals authoritative.

- [ ] **Step 7: Wire lifecycle and existing reconciler**

Instantiate store/executor/coordinator on `app.state`; call executor read-only `recover()` and coordinator `start()` in lifespan after the reconciler exists; call coordinator `stop()` before reconciler shutdown. Tests without lifespan inject these objects explicitly. Do not create a second release reconciler or managed-release state file.

- [ ] **Step 8: Fence enrollment before code consumption and notify every registry reload**

For `POST /api/hub/enrollment/claim` with `body.machine`, parse and validate the proposed host/machine/profile/modalities without mutation. Enter `controller_mutation(machine=machine)`, re-read registry, call `require_enrollment_registration_mutable(machine, host)`, and only then call `claim_enrollment_code`, `add_user_entries`, reload/notify, reconcile, assign profile, and set label. A busy or changed exact target therefore consumes no permanent code and mutates no registry/metadata. A no-machine claim and a distinct new machine remain allowed. Replace main's registry-reload call sites with one `_reload_registry_and_note_repair()` helper that reloads, then calls `note_registry_reload(monitor.registry)`; detection only sets `registry_changed_pending` and never releases a fence.

- [ ] **Step 9: Bound the coordinator lock to revalidation and local writes**

For Controller settings and purely local registry paths, enter `controller_mutation()` only immediately before re-reading bound state and retain it through the local mutation/reload notification. For discover/refetch, finish every probe/request first, then enter for revalidation plus local add/reload/profile changes; never hold it across `await` or network I/O. For fleet sync, perform every remote update/verify first and pass a `local_commit` callback that briefly enters `controller_mutation()` only around final local `set_fleet_token` and cache clear. Inside local identity/target mutations, call `require_controller_identity_mutable()` or `require_target_registry_mutable(machine)` as applicable. Rename, profile, Off flags, unrelated machines, and fleet-token rotation do not take the durable live-claim fence.

- [ ] **Step 10: Run API/auth/lifecycle regressions**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_api.py app/tests/test_auth.py app/tests/test_api.py app/tests/test_control_plane.py app/tests/test_enrollment.py app/tests/test_registry.py app/tests/test_peers.py`

Expected: PASS; every service route rejects browser credentials independently, owner routes reject transport credentials, same-target enrollment is fenced before code use, unrelated enrollment proceeds, reload changes only flag live requests, and network operations run outside short locks.

- [ ] **Step 11: Task checkpoint**

A read-only security reviewer may now inspect the complete owner/service boundary and redaction. Keep review output separate; no concurrent edits to `auth.py` or `main.py`. Do not commit.

### Task 12: Add Controller-only Remote UI, accessible dialog, responsive rows, and stable polling

Required UI skill before editing: `impeccable`. Required rendered-interaction skill in Steps 8-10: `browser:control-in-app-browser`.

**Files:**
- Modify: `app/frontend/index.html`
- Create: `app/tests/test_frontend_enrollment_repair.py`
- Create: `app/tests/browser_fixtures/enrollment_repair_remote_fixture.py`
- Modify: `app/tests/test_frontend_typography.py` only if its structural assertions need the new card

**Interfaces:**
- Consumes: owner eligibility/create/get endpoints and existing `loadMachines`, `renderMachines`, `applySummary`, SSE/poll fallback.
- Produces: per-row Repair enrollment, Repair eligible Macs, confirmation dialog, batch summary, status polling, retry/review states, and accessibility/responsive behavior.

- [ ] **Step 1: Write failing static UI contract tests**

Add these exact tests:

- `test_repair_controls_are_controller_only_and_use_stable_machine_ids`: inspect role guard, payload construction, and accessible name.
- `test_confirmation_copy_lists_preserved_state_and_token_not_changed`: assert every approved preservation noun and explicit fleet-token verification/no-change copy.
- `test_batch_copy_says_uncertain_mac_is_parked_while_later_macs_continue`: assert exact nonblocking statement and stable/excluded counts.
- `test_repair_dialog_has_focus_trap_return_focus_and_keyboard_close`: inspect dialog role, Tab cycling, Escape, and saved invoker focus.
- `test_repair_status_has_polite_live_region_busy_state_and_persistent_result`: assert `aria-live="polite"`, `aria-busy`, and separate durable result node.
- `test_repair_rows_stack_on_narrow_screens_and_actions_are_44px`: inspect the repair class rules inside a narrow-screen media query.
- `test_repair_polling_preserves_focus_expansion_and_scroll`: inspect keyed updates and absence of whole-list replacement while polling.
- `test_browser_fixture_is_loopback_only_and_serves_the_shipped_frontend`: instantiate on port 0, assert `127.0.0.1`, actual `app/frontend/index.html` bytes, deterministic mocked Controller/eligibility/batch responses, and rejection of unexpected/outbound paths.

- [ ] **Step 2: Verify RED**

Run: `conda_env/bin/python -m pytest -q app/tests/test_frontend_enrollment_repair.py`

Expected: FAIL because no repair UI exists.

- [ ] **Step 3: Add the smallest UI state model**

Keep `repairEligibility`, `repairBatches`, `repairPollTimer`, and the invoking element. Fetch eligibility only while Remote is visible and role is Controller. Poll active batches at a bounded interval; merge rows by stable ID without replacing a focused control or open `<details>`, and stop polling when every row is terminal or the tab is hidden.

- [ ] **Step 4: Add exact controls and copy**

Per remote row show **Repair enrollment** only when Controller-owned eligibility is available and `issuance_enabled` is true; show reason text and disabled explanation otherwise. The section action is **Repair eligible Macs**. Use the design's one-machine/batch confirmation copy and exact state messages, including `confirmation_pending`, retryable versus needs-review, token mismatch, expiry, writer busy, and settings changed. Never display tickets, tokens, claim bodies, full journal evidence, or guessed identity.

Render the exact aggregate form **{repaired} repaired · {pending} parked · {retryable} can retry · {review} need review · {remaining} remaining**. Offer Retry only for retryable rows; parked rows poll, and needs-review rows expose sanitized conflicting evidence without a guessed fix.

Keep fleet-token rotation available. Its existing confirmation must warn that an in-flight repair may become confirmation-pending and need manual credential recovery for status; it must not claim that rotation can change the identity values in an already accepted claim.

- [ ] **Step 5: Implement accessible dialog and responsive behavior**

Move focus into the dialog, trap Tab/Shift+Tab, support Escape/cancel, return focus to the invoker, do not steal focus on poll, set `aria-busy`, use one polite live region for concise transitions, and keep final text in the row. Include display name and full stable ID in accessible button names; use icon plus text; set 44px minimum targets; stack identity/status/action under the existing narrow-screen media query.

- [ ] **Step 6: Create the offline rendered-browser fixture**

Implement `create_fixture_server(frontend_path: Path, *, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer` plus a CLI that prints its one loopback URL. Serve the shipped `index.html`, mock only the dashboard APIs needed to render Remote, and provide deterministic controls to advance a repair batch from eligible to dialog to repairing to confirmation-pending to complete. Bind loopback only, make no outbound request, accept no credential, and never import/start `backend.main` or a Studio Hub lifespan.

- [ ] **Step 7: Run UI and broad frontend regressions**

Run: `conda_env/bin/python -m pytest -q app/tests/test_frontend_enrollment_repair.py app/tests/test_frontend_typography.py app/tests/test_frontend_release_reconciliation.py`

Expected: PASS. Existing Remote sort/filter, expanded rows, machine toggles, Studio toggles, removal, refetch, and SSE fallback remain intact; fixture tests prove loopback-only/offline behavior.

- [ ] **Step 8: Start only the localhost mock fixture**

Run: `conda_env/bin/python app/tests/browser_fixtures/enrollment_repair_remote_fixture.py --port 0`

Expected: one loopback URL containing the actual ephemeral port and an explicit `offline mock; no Hub/fleet connection` banner. Keep this bounded test process only for Steps 9-10; never start `start.js`, uvicorn, the Hub lifespan, or any fleet service.

- [ ] **Step 9: Perform mandatory rendered desktop and narrow QA**

Required sub-skill: `browser:control-in-app-browser`. Open only the fixture URL. At 1440×900 and 390×844, capture rendered evidence and verify all of the following:

- Controller-only per-machine and batch actions are visible, readable, and not clipped; primary action and durable result remain in view.
- Open the dialog by keyboard, Tab and Shift+Tab wrap inside it, Escape closes it, and focus returns to the exact invoking button; repeat for confirm/cancel.
- With a machine `<details>` expanded, focus a repair control and record `scrollY`; advance the mock polling state, then prove the same element remains focused, expansion remains open, and scroll position does not jump.
- Computed width and height for every repair/dialog action are each at least 44 CSS pixels.
- `document.documentElement.scrollWidth <= document.documentElement.clientWidth` at both widths; long stable IDs wrap/truncate visually while their accessible names retain the full ID.
- `aria-busy`, polite live status, parked/retry/review distinctions, batch summary, and final persistent success/failure text are visible and conveyed by text/icon rather than color alone.

There is no repository Playwright/axe dependency or standard rendered detector to reuse. Do not add one. Use the installed Browser skill's DOM inspection, keyboard control, viewport resize, and screenshots against the offline fixture.

- [ ] **Step 10: Treat rendered QA failure or inability as a release blocker**

If the fixture cannot render, the Browser skill is unavailable, viewport/keyboard inspection cannot be completed, or any desktop/narrow assertion fails, stop before Task 14's release commit and record the exact blocker. Source/static tests cannot waive this gate. After success, stop the fixture process and verify no listener remains on its ephemeral port.

- [ ] **Step 11: Task checkpoint**

A read-only UI reviewer inspects the desktop/narrow fixture evidence and keyboard contract independently of backend review. No browser connects to a live Hub and no edits occur while `index.html` has another writer. Do not commit.

### Task 13: Close the integrated preservation, race, expiry, and rollback matrix

**Files:**
- Modify: `app/tests/test_enrollment_repair_store.py`
- Modify: `app/tests/test_enrollment_repair_executor.py`
- Modify: `app/tests/test_enrollment_repair_coordinator.py`
- Modify: `app/tests/test_enrollment_repair_api.py`
- Modify: `app/tests/test_frontend_enrollment_repair.py`
- Modify: `app/tests/test_enrollment.py`
- Modify: `app/tests/test_api.py`
- Modify: `app/tests/test_peers.py`
- Modify production files from Tasks 1-12 only when a new RED test proves a contract gap

**Interfaces:**
- Consumes: the complete local implementation.
- Produces: the approved design acceptance matrix as deterministic local tests and a feature-disable/rollback contract.

- [ ] **Step 1: Add cross-component restart and overlap regressions**

Cover lost dispatch response, repeated apply, repeated redeem/status, Controller restart with issued/in-flight request, absolute expiry boundary, later Mac continuation, same-target lock, overlapping batches, and terminal adoption. Race registry and the final local fleet-token commit against issue/redeem and prove the short foreground lock serializes their snapshots while discover/refetch/fleet-sync network awaits remain outside it. Explicitly prove a parked live target does not create a cross-machine apply gate.

- [ ] **Step 2: Add pause and fence regressions**

Pause immediately before `os.replace`; assert Controller role, site ID/name, Controller ID, target delete/re-key/host/address change, and same-target permanent-code enrollment are busy before code/registry/metadata mutation; assert unrelated enrollment, later Mac, fleet rotation, and unrelated registry work proceed. Inject setup/join failure and prove repair remains busy until `_restore` plus cache reload complete. Resume and confirm one valid unchanged-claim write; then assert the previously fenced changes may proceed. Cleanup must remain blocked until a fake process-stop verifier and shared-lock classifier both succeed.

- [ ] **Step 3: Add preservation digests**

Seed owner password/session, Hub token, both fleet-token files, permanent code/use count, registry/endpoints, labels/profile, Off flags, SQLite jobs/artifacts, models/caches, updater state/history, and managed-release state. Compare byte or semantic digests after complete, never-applied, needs-review, expiry, token mismatch, missing/empty current token, same-target enrollment refusal, existing-code setup failure before code creation/rotation, pre-commit code-creation failure after its self-owned rollback, crash-before, and crash-after. Only the five settings keys may differ on complete; read-only token failures preserve absence or exact bytes/modes. Do not add a scenario that asks outer setup rollback to reverse a committed enrollment-code transaction.

- [ ] **Step 4: Add strict scope and rollback tests**

Assert capability schema remains v3 and managed-release vocabularies unchanged. With `NEW_ISSUANCE_ENABLED=False`, owner creation and UI initiation are unavailable, already-issued first redemption works only before its bound, status remains read-only indefinitely, and restart recovery writes nothing. Assert no import/call to permanent enrollment, registry re-key, Image/Voice, model, generation, launcher, or GenStudio paths.

- [ ] **Step 5: Run the complete repair matrix**

Run: `conda_env/bin/python -m pytest -q app/tests/test_enrollment_repair_store.py app/tests/test_controller_settings_lock.py app/tests/test_enrollment_repair_executor.py app/tests/test_enrollment_repair_coordinator.py app/tests/test_enrollment_repair_api.py app/tests/test_frontend_enrollment_repair.py app/tests/test_enrollment.py app/tests/test_api.py app/tests/test_peers.py`

Expected: PASS with no warning, leaked background task, real DNS lookup, or network access.

- [ ] **Step 6: Review checkpoints**

Perform three read-only reviews against the approved spec: security/auth/redaction; durability/restart/races/fences; UI/a11y/preservation/scope. Resolve findings sequentially with a new failing test first. Do not let reviewers write overlapping shared files.

- [ ] **Step 7: Task checkpoint**

Run `git diff --check` and inspect `git status --short`. Do not commit.

### Task 14: Document the shipped API, release Studio Hub 2.9.0, verify locally, and hand off

**Files:**
- Modify: `README.md`
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html` (`RELEASE_NOTES` plus already-reviewed UI)
- Include: `docs/superpowers/plans/2026-08-16-controller-managed-enrollment-repair.md`
- Verify only: all implementation/test files from Tasks 1-13

**Interfaces:**
- Consumes: complete passing implementation and repository release policy.
- Produces: truthful 2.9.0 docs/metadata, one local versioned commit, and a sanitized owner handoff; no push or live operation.

- [ ] **Step 1: Write the implemented README contract**

Document the six endpoints, owner versus service auth, exact five-key mutation, ticket expiry/single use, batch parking/continuation, terminal status adoption, safe older-Hub bootstrap gates, preserved permanent code/fleet tokens/release state, and emergency-only delete/re-enroll warning. State that active degraded-release durable-row cleanup is separate and mandatory before emergency re-enrollment.

- [ ] **Step 2: Bump 2.8.2 to 2.9.0 truthfully**

Set `VERSION` to `2.9.0`. Add `## [2.9.0] — 2026-08-16` to `CHANGELOG.md` and the first matching frontend release note. Say the repair APIs/UI/store/executor are implemented; do not claim a live canary, fleet rollout, automatic repair performed, or GenStudio change.

- [ ] **Step 3: Run focused metadata and scope tests**

Run: `conda_env/bin/python -m pytest -q app/tests/test_release_metadata.py app/tests/test_capabilities.py -k 'release or schema'`

Expected: PASS with VERSION/changelog/What's New synchronized and capability schema still v3.

- [ ] **Step 4: Scan for placeholders, secrets, and forbidden scope**

Run:

```bash
scan_pattern='TO''DO|TB''D|FI''XME|XX''X|authority_''not_after|apply_''window_seconds|24.?''hour'
rg -n "$scan_pattern" \
  app/backend/enrollment_repair*.py app/tests/test_enrollment_repair*.py \
  docs/superpowers/plans/2026-08-16-controller-managed-enrollment-repair.md README.md
rg -n 'configure_joined_agent\(' app/backend/enrollment_repair*.py
rg -n 'peers\.fleet_token\(' app/backend/enrollment_repair*.py app/backend/auth.py
rg -n 'ticket|fleet_token|authorization|cookie' app/backend/enrollment_repair*.py
git diff --name-only b1dc1df
```

Expected: the first three searches have no matches; credential references in the fourth search are digest/check/redaction logic only; changed paths contain no dependency manifest, launcher, capability implementation, sibling Studio, Image/Voice, model, generation, or GenStudio file.

- [ ] **Step 5: Run full local verification**

Run:

```bash
conda_env/bin/python -m pytest -q
conda_env/bin/python -m compileall -q app
conda_env/bin/python -m pip check
git diff --check
```

Expected: all commands exit 0. Record the exact pytest count and any expected skips.

- [ ] **Step 6: Perform the final spec-to-diff and rendered-evidence review**

Required sub-skill: `superpowers:verification-before-completion`. Answer every Implementation Review Gate in the approved design with a test/file reference. Re-read `AGENTS.md`. Require the completed Task 12 desktop/narrow Browser evidence; missing or failed rendered QA blocks release regardless of static/full-suite results. Confirm its mock listener was stopped, no launcher was touched, destination/example/PINOKIO launcher checks remain not applicable, and no live Hub/service/fleet process was contacted or changed.

- [ ] **Step 7: Create the single versioned implementation commit**

```bash
git add app/backend/auth.py app/backend/control_plane.py app/backend/controller_settings_lock.py \
  app/backend/enrollment.py app/backend/enrollment_repair.py \
  app/backend/enrollment_repair_executor.py app/backend/enrollment_repair_store.py \
  app/backend/enrollment_repair_transport.py \
  app/backend/main.py app/backend/peers.py app/backend/registry.py app/frontend/index.html \
  app/tests/conftest.py app/tests/test_auth.py app/tests/test_api.py \
  app/tests/test_control_plane.py app/tests/test_controller_settings_lock.py \
  app/tests/test_enrollment.py app/tests/test_enrollment_repair_api.py \
  app/tests/test_enrollment_repair_coordinator.py app/tests/test_enrollment_repair_executor.py \
  app/tests/test_enrollment_repair_store.py app/tests/test_frontend_enrollment_repair.py \
  app/tests/browser_fixtures/enrollment_repair_remote_fixture.py \
  app/tests/test_frontend_typography.py app/tests/test_peers.py app/tests/test_registry.py \
  README.md VERSION CHANGELOG.md \
  docs/superpowers/plans/2026-08-16-controller-managed-enrollment-repair.md
git diff --cached --check
git commit -m "feat: add controller-managed enrollment repair"
```

Before committing, remove any listed unchanged path from the `git add` command and add any legitimately changed repair test named by `git status`. Never stage unrelated work. Do not amend `b1dc1df`, push, tag, deploy, or start a service.

- [ ] **Step 8: Verify the exact committed release**

Run:

```bash
git status --short
git show --stat --oneline --decorate HEAD
git diff HEAD^ HEAD --check
conda_env/bin/python -m pytest -q app/tests/test_release_metadata.py \
  app/tests/test_enrollment_repair_store.py app/tests/test_enrollment_repair_executor.py \
  app/tests/test_enrollment_repair_coordinator.py app/tests/test_enrollment_repair_api.py \
  app/tests/test_frontend_enrollment_repair.py app/tests/test_enrollment.py \
  app/tests/test_api.py app/tests/test_peers.py
```

Expected: clean worktree, one new 2.9.0 implementation commit after intact `b1dc1df`, diff check passes, and focused committed tests pass.

- [ ] **Step 9: Prepare a read-only owner handoff**

Report commit SHA, changed files, exact verification results, preserved boundaries, and unresolved concerns. State: **No live canary or fleet action was run. Canary and rollout remain owner-performed outside this plan through a separately authorized manual or overnight update.** A checklist may name evidence the owner should observe later—exact identity, preservation digests, wake, uncertain-Mac parking, later-Mac continuation—but must contain no credential, host, live endpoint call, restart/update command, enrollment/repair action, or scheduling instruction.

## Coverage and Execution Order

- Durable ticket/batch store, digest-only secrets, ticket TTL, idempotency, restart, and race semantics: Tasks 1-2.
- Shared writer isolation, read-only current fleet credential, target executor, optional saved-parent evidence, wrong-role repair, strict callback, journal, one-file mutation, crash classification, and token preservation: Tasks 3-6.
- Controller eligibility, ambiguity, ordered nonblocking batches, dispatch/redeem/status adoption, reconciliation wake, bootstrap gates, and identity/membership fences: Tasks 7-10.
- Owner and three-service route boundary plus lifecycle integration: Task 11.
- Remote UI copy, a11y, responsive behavior, polling stability, and mandatory rendered mock QA: Task 12.
- Preservation, rollback, security, overlap, paused-process, and scope acceptance matrix: Task 13.
- README, 2.9.0 release truth, full verification, one versioned commit, and local handoff: Task 14.

Independent work is deliberately read-only: origin/security review after Task 5, identity-resolution review after Task 7, rendered UI review after Task 12, and final three-lane review after Task 13. No two agents may edit `main.py`, `conftest.py`, `index.html`, a repair module, or release metadata concurrently.

The plan ends at a clean local 2.9.0 commit and handoff. Live canary, fleet update, service restart, enrollment, repair, rollout, push, and deployment are outside scope and require later owner authorization.
