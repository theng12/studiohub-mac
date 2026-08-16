# Controller-Managed Enrollment Repair Design

**Status:** owner-approved design contract; no repair behavior is implemented by
this document or Studio Hub 2.8.2
**Owner:** StudioFleet / Studio Hub
**Implementation target:** a future versioned Studio Hub release

## Outcome

An owner signs in only to the location Controller and repairs a registered Mac's
Agent enrollment from **Remote > Registered Machines**. The Controller sends a
short-lived, one-machine repair ticket over the existing private fleet transport.
The Agent redeems that ticket by calling its already-saved parent Controller URL,
applies only the exact location identity returned by that Controller, confirms
the resulting identity, and wakes existing managed-release reconciliation.

The common path does not delete or re-enroll the machine, does not change its
Controller registry key, and does not use or reveal the permanent enrollment
code. A bad or uncertain identity is shown to the owner and never guessed.
Offline, ambiguous, duplicated, expired, host-mismatched, or token-mismatched
targets fail closed without stopping later Macs in a batch.

This is a design checkpoint, not a claim that the endpoints, ticket store,
dashboard controls, or repair worker already exist.

## Existing boundaries this design preserves

The implementation must build on these observed Studio Hub interfaces:

- `controller_settings.json` holds the local `role`, `site_id`, `site_name`,
  stable local `controller_id`, and Agent-only `parent_controller_url`.
- `.fleet_token` is the current shared Hub-to-Hub transport credential;
  `.hub_token`, `.hub_password.json`, and `.hub_sessions.json` are separate
  owner/recovery credentials.
- `studios.json` uses the Controller's machine key for routing. Machine labels
  and hardware profiles are separate metadata and do not define identity.
- current Agent enrollment uses the permanent Controller-owned credential to
  create a new membership and register Studio endpoints. Repair must not call
  that flow or `configure_joined_agent()` because repair is not new enrollment.
- `release_reconciliation.json` and its existing state names own durable managed
  release convergence. Repair may wake that service but must not rewrite its
  desired release, jobs, child operation IDs, attempts, or evidence.
- fleet-token middleware is intentionally broad for historical routes. Every
  new repair route therefore performs its own stricter credential, role,
  source-host, request, target, and callback validation.

The chosen approach is a Controller-minted callback ticket. Reusing the
permanent enrollment code would expand a long-lived new-machine secret into a
repair credential. Letting the Controller directly overwrite an Agent would
make the shared fleet token identity authority. Asking the owner to sign in to
each Agent would preserve neither centralized operation nor reliable batches.
The callback ticket keeps owner authority at the Controller while requiring the
target Agent to prove possession of its saved parent relationship.

## Scope

### In scope

- one-machine **Repair enrollment** and sequential **Repair eligible Macs**;
- strict owner initiation on the location Controller;
- durable, digest-only Controller ticket records with approximately 120-second
  bearer lifetimes;
- existing private LAN/Tailscale Hub transport plus route-specific checks;
- exact Agent-local identity repair, confirmation, audit state, retry, and
  managed-reconciliation wake-up;
- an optional safe ordinary Hub update for an older Agent that does not yet
  advertise the repair endpoint;
- clear UI states for every non-success outcome and ambiguity.

### Explicit non-goals

- general Controller-to-Agent dashboard SSO, delegated owner sessions, or
  opening an Agent dashboard as the Controller owner;
- broad hardening or replacement of fleet-token authorization on existing
  routes; that remains separate security debt;
- registration-code rotation, revocation, transport, or use during repair;
- Controller registry re-keying, machine merging, endpoint discovery, hostname
  inference, automatic duplicate resolution, or cosmetic renaming;
- deletion/re-enrollment as the normal recovery path;
- changes to dependencies, launchers, Image Studio, Voice Studio, model
  catalogs, generation behavior, capability schemas, or GenStudio;
- changes to `studiohub.site-capabilities` schema version 3 or to existing
  managed-release state names and meanings;
- fleet/service/network actions from this design checkpoint.

GenStudio remains independent. It neither creates repair tickets nor waits for
this feature to manage its own release intent.

## Roles and authorization boundary

| Actor or credential | May initiate | May transport | May redeem | May change identity |
| --- | --- | --- | --- | --- |
| Controller owner session or Controller loopback | yes | no | no | only by creating a bound request |
| Controller unique Hub token | no | no | no | no |
| Shared current fleet token alone | no | yes | no | no |
| Bound unexpired repair ticket alone | no | no | no | no |
| Fleet transport + valid ticket + exact source/target/callback checks | no | yes | yes | only the exact returned claim |
| Permanent enrollment code | new enrollment only | no | no | never used for repair |

Remote owner initiation requires a valid remembered owner browser session on
the Controller. It does not accept a Hub token, fleet token, recovery cookie,
or an owner session copied from another Hub. Loopback retains the existing
local-owner convention. Existing same-origin write protection remains in
force.

The two service-to-service routes require the current fleet token in the
`X-Hub-Token` header. They do not accept bearer query values, cookies, owner
sessions, the unique Hub token, or the permanent enrollment code. Passing this
transport check never authorizes an identity write: the Agent cannot apply a
claim until the Controller also accepts the ticket callback.

## Components and durable records

### Controller repair coordinator

A single coordinator owns eligibility, batch order, ticket issuance, dispatch,
restart adoption, status probing, confirmation, and reconciliation wake-up. It
uses a Controller-only table added to `setup_enrollment.db`; it does not put
repair state in history-only `fleet_versions.json` or managed-release state.

`enrollment_repair_batches` contains:

- random `batch_id`, owner initiation time, stable ordered target list, and
  aggregate counts;
- state `queued`, `running`, `complete`, or `complete_with_attention`;
- no credentials, tickets, claims, host headers, or customer data.

`enrollment_repair_requests` contains:

- random `request_id`, `batch_id`, ordinal, attempt, and target registry machine
  key;
- target host and resolved private-address snapshot;
- Controller `site_id`, `site_name`, and `controller_id` snapshots;
- current fleet-token digest snapshot, ticket SHA-256 digest, issue time, and
  expiry time;
- request state, ticket status, bounded stable error code, timestamps, and
  sanitized audit evidence;
- no plaintext ticket, fleet token, permanent enrollment code, Hub token,
  password/session value, model/job payload, or customer data.

Request states are `queued`, `checking`, `hub_update_required`, `updating_hub`,
`ticket_issued`, `dispatched`, `redeemed`, `verifying`, `complete`, `retryable`,
and `needs_review`. These names belong only to enrollment repair and do not
alter managed-release states.

Ticket status is a separate field in the same request transaction, not a
reusable credential row: `issued` may become `redeemed`, `expired`, or
`superseded`. A partial unique index permits only one nonterminal request per
target machine. A second identical owner action adopts and returns that request;
it never creates a second active ticket.

### Agent repair executor

The Agent adds a small mode-0600 repair journal under `DATA_DIR`. It persists
the request ID, target, Controller snapshot, ticket digest, state, identity
before/after digests, and timestamps. It may hold the redeemed exact claim only
while applying or recovering a crash. That private transient record is scrubbed
to non-secret evidence immediately after success or rollback.

Agent states are `accepted`, `redeeming`, `applying`, `complete`, and
`rolled_back`. A repeated identical dispatch adopts the journal. A different
request while one is applying returns conflict and changes nothing.

### Existing reconciliation service

After exact confirmation, the Controller calls the existing peer/registry wake
path for that stable machine. The same `release_id`, machine row, child
`operation_id`, component evidence, retry counters, and state vocabulary remain
authoritative. Repair never creates a replacement managed-release row.

## Exact API contract

All examples show logical fields, not a released implementation.

### Owner-facing Controller API

| Method | Endpoint | Contract |
| --- | --- | --- |
| `POST` | `/api/hub/enrollment-repairs` | owner/loopback only; accepts `{"machines":["stable-id"]}`; validates and queues one ordered batch; `202` |
| `GET` | `/api/hub/enrollment-repairs/{batch_id}` | owner/loopback only; returns sanitized batch and per-machine status |
| `GET` | `/api/hub/enrollment-repairs/eligibility` | owner/loopback only; read-only reason per registered remote machine |

The per-machine button sends a one-element `machines` list. The batch button
sends the stable eligible IDs currently displayed, but the server re-evaluates
every target at execution time. Client eligibility is never authority. The
static `eligibility` route is registered before the dynamic batch route.

### Controller-to-Agent dispatch

```http
POST http://<registry-host>:47873/api/hub/enrollment-repair/apply
X-Hub-Token: <current-fleet-token>
Content-Type: application/json

{
  "schema": "studiohub.enrollment-repair-dispatch",
  "schema_version": 1,
  "request_id": "<opaque-request-id>",
  "target_machine_id": "<exact-controller-registry-key>",
  "ticket": "<random-url-safe-value>",
  "controller": {
    "site_id": "<snapshot>",
    "site_name": "<snapshot>",
    "controller_id": "<snapshot>"
  }
}
```

The ticket is at least 256 random bits, URL-safe, never placed in a URL, and
expires 120 seconds after issuance. The Controller commits its digest and all
bindings before sending it, holds plaintext only in request memory, and
redacts the complete body and headers from logs.

Before callback, the Agent requires all of the following:

1. exact fleet-token header authentication and a private socket peer;
2. no query credential, browser credential, redirect, or forwarded-host trust;
3. a supported schema and bounded field lengths;
4. a locally saved, syntactically valid, private `parent_controller_url`;
5. the dispatch socket peer to match the private addresses resolved from that
   saved parent URL;
6. no different active request and no environment-locked identity fields;
7. local database mode already `off`; an Agent with shadow/global database
   configuration is `needs_review`, never silently converted.

The dispatch does not contain a callback URL. The Agent normalizes and uses its
own already-saved parent URL with redirects and environment proxies disabled.

### Agent-to-Controller redemption

```http
POST <saved-parent-controller-url>/api/hub/enrollment-repair-tickets/redeem
X-Hub-Token: <agent-current-fleet-token>
Content-Type: application/json

{
  "schema": "studiohub.enrollment-repair-redemption",
  "schema_version": 1,
  "request_id": "<same-request-id>",
  "target_machine_id": "<same-registry-key>",
  "ticket": "<same-ticket>",
  "observed_identity": {
    "role": "<current-local-role>",
    "site_id": "<current-local-site>",
    "site_name": "<current-local-site-name>",
    "controller_id": "<current-local-stable-id>"
  }
}
```

In one `BEGIN IMMEDIATE` transaction, the Controller requires:

- exact request ID and ticket digest, request state `ticket_issued` or adopted
  `redeemed`, corresponding ticket status `issued` or `redeemed`, and
  `now < expires_at`;
- exact target machine binding and a still-existing remote registry machine;
- one unambiguous registry host for that key and no other key using that host;
- direct socket source address equal to the issued target-address snapshot;
- registry host and address snapshot still unchanged;
- current Controller role and exact site/site-name/controller snapshots;
- current fleet-token digest equal to the issued snapshot and exact fleet-token
  header authentication;
- no completed repair under a different request and no concurrent ticket.

The route reads the direct socket peer. It ignores `Forwarded`,
`X-Forwarded-For`, browser cookies, and request-supplied host claims. A future
trusted-proxy deployment needs a separate reviewed source-identity contract;
until then an obscured source fails closed.

The first valid transaction marks the ticket redeemed. An identical retry from
the same source, request, target, and digest within the original TTL adopts the
same redemption; it is idempotent recovery, not a second authorization. Every
other replay fails. After expiry, even an identical callback receives
`ticket_expired`; the owner can create a new request.

The successful response is only this exact repair claim:

```json
{
  "schema": "studiohub.enrollment-repair-claim",
  "schema_version": 1,
  "request_id": "<same-request-id>",
  "target_machine_id": "<exact-controller-registry-key>",
  "role": "agent",
  "site_id": "<controller-site-id>",
  "site_name": "<controller-site-name>",
  "controller_id": "<exact-controller-registry-key>",
  "fleet_token": "<current-controller-fleet-token>"
}
```

It contains no registration entry, endpoint list, hardware profile, display
name, permanent enrollment code, Controller Hub token, owner/session value,
database credential, update setting, job, model, cache, release intent, or
redirect/callback URL.

### Agent application and confirmation

The Agent binds the response to its journaled request and validates every
field. Under one repair lock it stages mode-0600 replacements and applies only:

- `role = agent`;
- Controller-provided `site_id` and `site_name`;
- `controller_id = target_machine_id`, the stable existing Controller registry
  key;
- the normalized saved parent Controller URL it actually called;
- the Controller-provided current fleet token in the existing local token files.

No generic setup or enrollment helper may add side effects. All unlisted keys
in `controller_settings.json` are retained. The preflight requirement that
database mode is already `off` means repair does not change database mode or
database credentials.

The Agent reads the result back from disk, reloads the identity/token caches,
and returns the following sanitized result on the still-open dispatch request:

```json
{
  "request_id": "<same-request-id>",
  "state": "complete",
  "identity": {
    "role": "agent",
    "site_id": "<exact-site-id>",
    "site_name": "<exact-site-name>",
    "controller_id": "<exact-target-machine-id>"
  },
  "applied_at": "<UTC timestamp>"
}
```

`GET /api/hub/enrollment-repair/status/{request_id}` exposes that same
sanitized local result only to the exact current fleet-token header and a
private source matching the saved parent Controller. It exists so a Controller
restart or lost dispatch response can adopt confirmation without another
identity write.

The Controller marks `complete` only after the returned or adopted status has
the exact role, site ID, site name, stable ID, request ID, registry host, and
source host. It then invalidates the peer cache and wakes managed reconciliation
for the same registry key. A transport timeout after a locally confirmed apply
does not cause the Agent to roll back; the Controller recovers through the
status endpoint and never re-keys the registry.

## Eligibility and identity resolution

A Mac is automatically eligible only when all of these are true:

- it is a remote, currently registered stable machine key, not `local`;
- all its registered Studio rows resolve to one identical host;
- no second registered machine key uses that host or resolved private address;
- the host resolves only to allowed private LAN or Tailscale addresses and the
  direct peer remains observable;
- the Controller is in `controller` role with a current fleet token;
- the Agent accepts that exact current token and either advertises repair
  schema 1 or qualifies for the controlled older-Hub bootstrap below;
- the Agent has a saved private parent Controller URL, no environment-locked
  identity, and database mode `off`;
- there is no other active repair for that stable key.

The stable registry machine key selected by the owner is the only repaired
Agent `controller_id`. Hostnames, display names, hardware profiles, reported
names, MAC-derived fingerprints, and endpoint titles are evidence shown to the
owner, never alternate identity authorities. Conflicting evidence produces
`needs_review` with both sanitized values. The service does not choose the
"closest" value and does not create or rename a registry key.

## Durability, idempotency, restart, race, and expiry semantics

### Controller restart

- queued and checking rows resume in stable ordinal order;
- plaintext tickets are never reconstructed from the database;
- an issued row whose dispatch may be in flight remains adoptable by the Agent
  until expiry, while the batch parks it and continues to the next Mac;
- after expiry, an unredeemed row becomes `retryable`; an owner retry creates a
  fresh ticket and digest under a new attempt;
- a redeemed/verifying row probes the Agent status endpoint and confirms the
  exact identity or remains retryable/needs-review;
- no restart converts uncertainty into success or blocks later targets.

### Agent restart and partial writes

- before redemption, a restart loses no authority; the Controller can retry or
  issue a new ticket after expiry;
- after redemption, the Agent fsyncs the private claim journal before changing
  identity files;
- staged replacements are written and fsynced before atomic renames;
- on startup, an `applying` journal either completes the same exact forward
  repair and verifies it, or restores every preimage and records `rolled_back`;
- the Agent never requests a different claim and never falls back to permanent
  enrollment.

### Races

- one target has at most one nonterminal request and one Agent repair lock;
- identical dispatches and status probes adopt; different request IDs conflict;
- site/controller identity, registry host, or fleet-token rotation between
  issue and redeem invalidates the ticket;
- a registry removal, duplicate host appearing, or host/address change produces
  `needs_review`; the coordinator does not follow the moving target;
- expiry is checked inside the same transaction that consumes the ticket, so a
  callback at the boundary either wins once or fails without a claim;
- batch execution has at most one actively dispatching/applying Mac. A bounded
  offline probe or parked ticket yields to the next ordinal, so one machine can
  never stall the rest.

`retryable` means the same unambiguous machine may be attempted again after a
transient condition. `needs_review` means identity or authority evidence is
ambiguous or unsafe and requires owner action before another ticket is minted.

## Older-Agent bootstrap

The Controller first performs a read-only repair-capability probe. A 404 or
missing schema means **Hub update required**, not permission to improvise.

The Controller may run the existing ordinary Agent-Hub self-update once, then
re-probe repair capability, only when all of the following are proven:

- the registry target and private host are unambiguous;
- the Agent accepts the exact current fleet token;
- the ordinary self-update endpoint and restart verification are already
  supported by that Agent version;
- Hub and Studio work on that Mac is idle and the machine is not disabled;
- no active or degraded immutable managed release includes that Mac;
- the update coordinator reports no conflicting update or maintenance job;
- the published Hub version is known to contain the repair endpoint.

This bootstrap is ordinary Hub update behavior only. It cannot consume a repair
ticket, claim repaired identity, update Image/Voice, or alter models. If any
gate is absent, the row says **Hub update required — update this Mac manually,
then retry**. A legacy moving-`main` update is never used while an active
degraded immutable release would make its target or durable row ambiguous.

## UI and operator experience

The controls live only in **Remote > Registered Machines** on a Controller.
Each remote row has **Repair enrollment**. The section action is **Repair
eligible Macs**. Standalone and Agent dashboards do not show either action.

Before a one-machine action, the dialog says:

> Repair enrollment for **{display name}** (`{stable machine ID}`)? This keeps
> its jobs, models, Studio registrations, names, settings, and credentials. It
> changes only this Agent's location identity, saved parent Controller address,
> and current fleet token.

Before a batch, the dialog shows the exact eligible count, stable order, and
excluded count. It states that Macs run one at a time and that attention on one
Mac will not stop the rest.

Per-machine states and copy are:

| State | Owner-facing copy |
| --- | --- |
| checking | **Checking repair safety…** |
| updating_hub | **Updating this Mac's Studio Hub before repair…** |
| hub_update_required | **Hub update required — update this Mac manually, then retry.** |
| dispatched/redeemed/applying | **Repairing enrollment…** |
| complete | **Enrollment repaired. Identity confirmed; managed release reconciliation is awake.** |
| offline/retryable | **Mac is offline. Nothing changed. Try again when it is reachable.** |
| ambiguous duplicate | **Needs review: this address matches more than one registered Mac. No identity was changed.** |
| host mismatch | **Needs review: the Mac answered from a different private address. No identity was changed.** |
| token mismatch | **Fleet credential mismatch. Repair was not authorized; use manual recovery.** |
| expired | **The repair ticket expired before use. Nothing changed; retry when the Mac is reachable.** |

The batch summary is **{repaired} repaired · {retryable} can retry · {review}
need review · {remaining} remaining** and always advances after a bounded
result. A retry button is offered only for `retryable`; `needs_review` links to
the conflicting evidence without suggesting a guessed identity.

Accessibility requirements:

- row buttons have an accessible name including the machine display name and
  stable ID; batch and dialog controls work by keyboard;
- focus moves into the confirmation dialog, is trapped while open, returns to
  the invoking button, and is never stolen by polling;
- progress uses `aria-busy`; concise transitions use a polite `aria-live`
  region; final failure and success text remains visible outside live regions;
- state is conveyed by icon and text, never color alone; disabled buttons state
  why; targets remain at least 44 by 44 CSS pixels;
- on narrow screens, machine identity and status stack above the action instead
  of forcing a wide table; long stable IDs wrap or truncate visually while the
  accessible name retains the full value;
- polling preserves expanded rows, focus, and scroll position.

## Preservation contract

Repair changes only the five Agent-local identity values listed above and the
current fleet token. In particular it preserves:

- the Controller registry key and all registered Studio IDs, hosts, ports,
  modalities, titles, and endpoint mappings;
- owner password, password hash, remembered browser sessions, unique Hub token,
  and recovery access;
- permanent Controller enrollment code, including its stable reusable value,
  use count, and rotate/revoke lifecycle;
- machine display name, labels, hardware-profile assignment, custom hardware
  profiles, and local hardware evidence;
- whole-machine and per-Studio Off flags;
- local SQLite jobs, execution identities, artifacts, transcription/chat jobs,
  uploads, shared voices, model exposure, model baselines, model/cache files,
  catalog observations, and download state;
- ordinary Hub/Studio update modes, schedules, idle settings, job history, and
  fleet version history;
- `release_reconciliation.json`, desired release intent, activation/job IDs,
  stable machine row, child operation/job IDs, retry counters, component
  states, catalog evidence, and lock/lease semantics;
- `studiohub.site-capabilities` version 3 and all generation/capability data.

The Controller takes no registry or fleet-setting write while issuing or
redeeming a ticket. On the Agent, a validation error occurs before staging and
changes nothing. An apply error restores exact preimages before reporting
`rolled_back`. After a verified apply, lack of a Controller response is treated
as confirmation uncertainty, not a reason to undo correct local identity; the
status probe adopts it.

There is no dashboard **Undo** because restoring an unknown bad identity would
be unsafe. The private preimage exists only for in-flight crash recovery and is
scrubbed when the operation reaches a verified terminal state.

Deleting and re-enrolling is an emergency procedure only. It is especially
unsafe while an active managed release is degraded: removing the registry key
does not by itself remove the old durable release row, child operation, or
retry evidence. Emergency re-enrollment must be accompanied by a separately
reviewed cleanup/migration of those durable rows; this repair design never
performs that cleanup implicitly.

The current permanent enrollment code remains stable and reusable for enrolling
new Macs until the owner explicitly rotates or revokes it.

## Security, privacy, redaction, and threat model

### Secret handling

- ticket entropy is at least 256 bits and lifetime is 120 seconds;
- only SHA-256 ticket and fleet-token digests are durable on the Controller;
- tickets and tokens appear only in HTTPS/Tailscale/private request bodies and
  headers, never URLs, UI, analytics, errors, audit JSON, or ordinary logs;
- the exact claim is held transiently in a mode-0600 Agent journal only for
  crash-safe apply and is scrubbed afterward;
- request and batch IDs are non-secret and safe to display; observed identity
  and host evidence are bounded and sanitized.

### Threats and controls

| Threat | Required control |
| --- | --- |
| stolen fleet token | cannot initiate owner request or repair identity without a live bound ticket and exact source/callback checks |
| stolen ticket | cannot pass fleet transport, target/source, Controller snapshot, or saved-parent callback checks; expires quickly |
| replay | atomic digest consumption; only exact in-TTL adoption is allowed |
| wrong registered machine | ticket target, registry key, source address, and returned stable ID must all match |
| duplicated endpoint | owner-visible `needs_review`; no ticket issued |
| DNS rebinding or moved host | issue-time private address snapshot plus redemption-time direct socket comparison; no redirect |
| malicious callback URL | dispatch has none; Agent uses only its saved, private, normalized parent URL |
| browser CSRF/session confusion | same-origin middleware plus Controller owner-session/loopback initiation; Agent browser sessions are irrelevant |
| Controller identity or token rotation in flight | snapshot mismatch invalidates the ticket and returns no claim |
| log/exception leakage | structured stable codes; centralized redaction of ticket, claim, authorization headers, tokens, and private journal content |
| crash during multi-file apply | fsynced private forward/rollback journal, staged replacements, startup recovery, exact read-back verification |

Private reachability narrows exposure but is not authorization. Tailscale/LAN
membership, a fleet token, and a valid ticket are each insufficient alone.

## Failure model

Stable error codes and outcomes are:

| Code | Outcome | Batch behavior |
| --- | --- | --- |
| `offline` / `transport_unavailable` | retryable, no claim | continue |
| `repair_endpoint_unavailable` | bootstrap or owner-visible Hub update required | continue |
| `ambiguous_registry_host` / `duplicate_host` | needs review, no ticket | continue |
| `source_host_mismatch` / `registry_changed` | needs review, no claim | continue |
| `fleet_token_mismatch` | needs review/manual recovery, no claim | continue |
| `controller_snapshot_changed` | retryable only after fresh owner-visible evaluation | continue |
| `ticket_expired` / `ticket_superseded` | retryable, no claim | continue |
| `request_conflict` | adopt identical request or needs review | continue |
| `environment_locked` / `database_mode_unsafe` | needs review/manual recovery | continue |
| `apply_failed_rolled_back` | retryable after exact rollback | continue |
| `confirmation_pending` | status-probe adoption; never duplicate apply | continue |

Errors never include ticket/token material or a full claim. HTTP status is not
the durable truth; the request row and Agent journal are.

## Migration, bootstrap, and controlled rollout

1. Add the Controller tables with `CREATE TABLE/INDEX IF NOT EXISTS`; do not
   rewrite permanent enrollment rows or registry data.
2. Add the Agent repair journal reader. No journal means no pending repair; old
   state remains valid without migration.
3. Release the Controller owner API, strict machine routes, Agent executor, and
   UI together behind advertised repair schema 1. Do not change capability
   schema v3.
4. Update location Controllers first. Keep repair actions unavailable until the
   Controller confirms its migration and local route schema.
5. On one idle, healthy, unambiguous Agent, run a manual canary and prove every
   preservation digest plus reconciliation wake-up.
6. Exercise one controlled older-Agent bootstrap only outside an active/degraded
   managed release. If its safe-update gates are not all observed, use the
   manual Hub update message instead.
7. Enable sequential batches one location at a time. Review every
   `needs_review` row; never convert it to a guess or an automatic delete.
8. Roll back the feature by hiding owner initiation and rejecting new ticket
   issuance. Allow already-redeemed Agents to finish/status-adopt until their
   120-second window closes. Preserve sanitized audit rows for diagnosis.

No fleet rollout begins from this design release.

## Acceptance test matrix

| Area | Required evidence |
| --- | --- |
| owner auth | Controller loopback and valid owner session can initiate; Hub token, fleet token, Agent session, missing session, cross-origin write, and non-Controller role cannot |
| route auth | both service routes require exact current fleet header; unique Hub token, cookie, bearer/query ticket, permanent code, and missing token fail |
| ticket storage | cryptographically random ticket; Controller DB contains digest only; 120-second TTL; body/header/log/UI redaction |
| exact binding | wrong request, target, source host, registry host, site, site name, Controller ID, token snapshot, or schema returns no claim |
| single use | first redemption succeeds once; exact in-TTL retry adopts; different replay and post-expiry replay fail under concurrency |
| callback | Agent ignores dispatch callback values because none exist; only saved normalized private parent URL is used; redirects, public resolution, proxy env, and source mismatch fail |
| ambiguity | duplicate host, multi-host machine rows, changed DNS/address, missing registry key, and conflicting stable ID are owner-visible and never guessed |
| idempotency | double-click, repeated POST, lost dispatch response, repeated Agent apply, and repeated status poll produce one request and one local write |
| Controller restart | queued order resumes; issued plaintext is not reconstructed; parked/expired rows do not block later Macs; redeemed row adopts Agent status |
| Agent restart | restart before redeem changes nothing; restart after claim resumes exact forward apply or restores all preimages; journal secret is scrubbed |
| races | concurrent target requests, token rotation, Controller identity change, registry edit/removal, expiry boundary, and overlapping batch all fail/adopt exactly as specified |
| exact mutation | only role, site ID/name, stable controller ID, normalized saved parent URL, and current fleet token differ after success |
| preservation | byte/digest or semantic equality for owner password/sessions, Hub token, permanent code, registry/endpoints, labels/profile, Off flags, jobs/artifacts, models/caches, update settings/history, and managed-release keys/state |
| rollback | injected failure at every staged rename restores exact preimages and returns `apply_failed_rolled_back`; no partial success is reported |
| confirmation | Controller requires exact returned/adopted identity before complete, invalidates peer cache, and wakes the existing stable managed-release row once |
| batch | stable machine ordering, maximum one active apply, and continuation after offline, expiry, mismatch, needs-review, bootstrap failure, and rollback |
| older Hub | safe idle/no-active-release self-update can add endpoint and re-probe; every missing gate yields manual Hub update required; no Image/Voice/model update |
| UI | exact copy/states, retry vs review distinction, focus/dialog behavior, live-region behavior, keyboard operation, non-color cues, 44px targets, and narrow-screen stacking |
| schema/scope | capability snapshot remains `studiohub.site-capabilities` v3; existing managed-release state names unchanged; no dependency, launcher, sibling Studio, model, generation, or GenStudio diff |
| release truth | implementation release will add focused unit/integration/UI/restart tests, full suite and syntax checks, dependency audit, `git diff --check`, synchronized release metadata, review, commit, and controlled canary before fleet use |

## Implementation review gates

Before code is accepted, reviewers must answer yes to all of these:

- Can a fleet token without an owner-created ticket cause any identity write?
- Does every successful claim identify exactly one registry machine and direct
  private source under unchanged Controller/token snapshots?
- Can Controller and Agent restarts converge without persisting a plaintext
  ticket on the Controller or applying twice?
- Does a failed or ambiguous Mac always leave later batch Macs runnable?
- Are the permanent code, Controller registry key, owner credentials, machine
  metadata, workloads, models, settings, and durable release evidence unchanged?
- Is every ambiguity visible to the owner with no inference path?
- Is delete/re-enroll clearly an emergency with separate durable-row cleanup?
- Do release notes say precisely what is implemented, without treating this
  design-only 2.8.2 checkpoint as shipped repair behavior?
