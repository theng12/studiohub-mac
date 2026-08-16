# Controller-Managed Enrollment Repair Design

**Status:** owner-approved design contract; no repair behavior is implemented by
this document or Studio Hub 2.8.2
**Owner:** StudioFleet / Studio Hub
**Implementation target:** a future versioned Studio Hub release

## Outcome

An owner signs in only to the location Controller and repairs a registered Mac's
Agent enrollment from **Remote > Registered Machines**. The Controller sends a
short-lived, one-machine repair ticket over the existing private fleet transport.
The dispatch includes the Controller's canonical private URL. The registered
target executor accepts that URL only when its normalized private resolution is
exactly the direct dispatch socket peer, redeems the ticket there, persists that
validated URL, applies only the exact location identity returned by that
Controller, confirms the result, and wakes existing managed-release
reconciliation.

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
- existing settings behavior clears `parent_controller_url` whenever the saved
  role is not `agent`; a missing or wrong saved parent and a wrong current role
  are therefore approved repair inputs, not proof that the target is ineligible.
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
target executor to prove that the supplied private callback origin is the same
Controller process that directly dispatched the bound request.

## Scope

### In scope

- one-machine **Repair enrollment** and sequential **Repair eligible Macs**;
- strict owner initiation on the location Controller;
- durable, digest-only Controller ticket records with approximately 120-second
  first-redemption lifetimes;
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
| Bound unredeemed repair ticket alone | no | no | no | no |
| Fleet transport + valid ticket + exact source/target/callback checks | no | yes | yes | only the exact returned claim |
| Permanent enrollment code | new enrollment only | no | no | never used for repair |

Remote owner initiation requires a valid remembered owner browser session on
the Controller. It does not accept a Hub token, fleet token, recovery cookie,
or an owner session copied from another Hub. Loopback retains the existing
local-owner convention. Existing same-origin write protection remains in
force.

The three service routes—target-executor `apply`, Controller `redeem`, and
target-executor `status`—each require the exact current fleet token in the
`X-Hub-Token` header, a private direct socket peer, and their route-specific
source binding.
All three reject Authorization bearer substitutes, query values, cookies,
owner sessions, the unique Hub token, and the permanent enrollment code.
Passing this transport check never authorizes an identity write: the executor
cannot apply a claim until the Controller also accepts the bound ticket
redemption.

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

`enrollment_repair_coordinator` is a singleton row containing only the current
`active_request_id` and lease/update timestamps. Every issue, dispatch,
redemption transition, terminal adoption, and authority-free expiry updates
that row in the same immediate transaction. Queued requests may exist in many
batches, but only the referenced request can hold dispatch/apply authority.

`enrollment_repair_requests` contains:

- random `request_id`, `batch_id`, ordinal, attempt, and target registry machine
  key;
- target host and resolved private-address snapshot;
- Controller `site_id`, `site_name`, and `controller_id` snapshots plus the
  target-specific canonical private Controller URL used for callback;
- current fleet-token digest snapshot, ticket SHA-256 digest, issue time, and
  new-redemption expiry time; a redeemed row also records its direct source,
  redemption time, and bounded recovery deadline;
- request state, ticket status, bounded stable error code, timestamps, and
  sanitized audit evidence;
- no plaintext ticket, fleet token, permanent enrollment code, Hub token,
  password/session value, model/job payload, or customer data.

Request states are `queued`, `checking`, `hub_update_required`, `updating_hub`,
`ticket_issued`, `dispatched`, `redeemed`, `verifying`, `complete`, `retryable`,
and `needs_review`. These names belong only to enrollment repair and do not
alter managed-release states. `complete`, `retryable`, and `needs_review` are
authority-terminal for that attempt, but after ticket issuance
`retryable`/`needs_review` may be set only after atomic unredeemed expiry or
authenticated executor terminal evidence, or after the redeemed claim's
bounded recovery deadline itself proves that no forward apply may begin/resume.
Pre-ticket validation may terminate immediately.

Ticket status is a separate field in the same request transaction, not a
reusable credential row: `issued` may become only `redeemed` or `expired`.
A partial unique index permits only one nonterminal request per
target machine. An uncertain issued request remains nonterminal until atomic
expiry proves that it never redeemed. A redeemed request remains nonterminal
until confirmation or bounded audited abandonment proves that no forward-apply
authority remains. A second identical owner action adopts and returns the
predecessor; it never creates a second active ticket.

### Target repair executor

The target executor adds a small mode-0600 repair journal under `DATA_DIR`. It
persists the request ID, target, Controller snapshot, validated canonical
Controller URL, state, identity before/after digests, and timestamps. It holds
the plaintext ticket only while first redemption or adopted-redemption recovery
is possible, and may hold the exact claim only while applying or recovering a
crash. These private transient values are scrubbed to non-secret evidence
immediately after success, rollback, or audited abandonment. The Controller
still stores only the ticket digest.

Agent states are `accepted`, `redeeming`, `applying`, `complete`, and
`rolled_back`, plus terminal `abandoned` when the bounded recovery authority is
scrubbed without a successful apply. A repeated identical dispatch adopts the
journal. A different request while one is nonterminal returns conflict and
changes nothing. The executor route does not require the current local role to
be `agent`: registered target binding, not the damaged role value, identifies
the intended Mac.

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

### Service routes

| Direction | Method and endpoint | Strict source binding |
| --- | --- | --- |
| Controller to target executor | `POST /api/hub/enrollment-repair/apply` | direct private socket peer must equal the single private address resolved from the supplied canonical Controller URL |
| Target executor to Controller | `POST /api/hub/enrollment-repair-tickets/redeem` | direct private socket peer must equal the target registry address captured for the request |
| Controller to target executor | `GET /api/hub/enrollment-repair/status/{request_id}` | direct private socket peer must equal the journaled validated Controller address |

Each route independently enforces the exact-current `X-Hub-Token` fleet header
and its source binding before reading or changing repair state. The broad
middleware result is never sufficient.

### Controller-to-target dispatch

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
  "controller_url": "<canonical-private-controller-origin>",
  "controller": {
    "site_id": "<snapshot>",
    "site_name": "<snapshot>",
    "controller_id": "<snapshot>"
  }
}
```

The ticket is at least 256 random bits, URL-safe, never placed in a URL, and
becomes ineligible for first redemption 120 seconds after issuance. The
Controller commits its digest and all bindings before sending it, holds
plaintext only in request memory, and redacts the complete body and headers
from logs.

For each target, the Controller selects the credential-free private origin
whose single resolved address is the local interface address used by the
outbound dispatch socket. It snapshots that normalized origin in the request
before dispatch. A multi-address hostname, public address, path, query,
fragment, embedded credential, redirect-dependent origin, or origin that does
not match the actual dispatch source is not canonical and is not sent.

Before callback, the target executor requires all of the following:

1. exact fleet-token header authentication and a private socket peer;
2. no query credential, browser credential, redirect, or forwarded-host trust;
3. a supported schema and bounded field lengths;
4. a supplied `controller_url` that normalizes to a credential-free origin,
   resolves to exactly one private address, and exactly equals the direct
   dispatch socket peer;
5. redirects and environment proxies disabled for validation and callback;
6. no different nonterminal request and no environment-locked identity fields;
7. local database mode already `off`; a target with shadow/global database
   configuration is `needs_review`, never silently converted.

The locally saved `parent_controller_url` is optional evidence. If present, its
normalized value and whether it matches the validated dispatch origin are
recorded in sanitized audit evidence, but missing, stale, malformed, or wrong
saved-parent data does not reject an otherwise exact request. The executor uses
only the supplied-and-socket-validated Controller origin for redemption. The
callback client pins the one validated address for connection (while retaining
the normalized origin for HTTP Host/TLS verification), permits no second DNS
choice, and persists that origin as the repaired parent URL. Its current role
may be `agent`, `standalone`, or `controller`; role mismatch is repair scope, while
environment-locked identity or non-`off` database mode still fails closed.

### Target-to-Controller redemption

```http
POST <validated-controller-url>/api/hub/enrollment-repair-tickets/redeem
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
    "controller_id": "<current-local-stable-id>",
    "parent_controller_url": "<current-local-origin-or-null>"
  }
}
```

`observed_identity` is bounded pre-repair audit evidence, never target
authority. Wrong role, site, stable ID, or missing/wrong parent evidence may
explain why repair is needed and does not override the request's registry,
source, ticket, Controller, or token bindings.

In one `BEGIN IMMEDIATE` transaction, the Controller requires:

- exact request ID and ticket digest plus request/ticket state
  `ticket_issued`/`issued` for first redemption or `redeemed`/`redeemed` for an
  exact adoption;
- `now < redemption_expires_at` for the first `issued` to `redeemed`
  transition; the 120-second deadline does not apply again after that atomic
  transition;
- for adopted redemption, the exact originally bound source and a still-open
  recovery record that has not reached terminal confirmation or audited
  abandonment;
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

The first valid transaction before the 120-second deadline marks the ticket
redeemed, fixes its source binding, and records a 24-hour
`apply_recovery_deadline`. The deadline prevents a *new* redemption only. An
identical retry from the same source, request, target, and digest may adopt the
already-redeemed claim after the ticket TTL, through the bounded recovery
deadline; this is recovery of one consumed authorization, not a second use.
Every different replay fails. An unredeemed ticket presented after expiry gets
`ticket_expired` and can never yield a claim.

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
  "fleet_token": "<current-controller-fleet-token>",
  "apply_recovery_deadline": "<redeemed-at-plus-24-hours>"
}
```

It contains no registration entry, endpoint list, hardware profile, display
name, permanent enrollment code, Controller Hub token, owner/session value,
database credential, update setting, job, model, cache, release intent, or
redirect/callback URL. The recovery deadline is authority metadata, not a new
credential.

### Target application and confirmation

The target executor binds the response to its journaled request and validates
every field. Under one repair lock it stages mode-0600 replacements and applies
only:

- `role = agent`;
- Controller-provided `site_id` and `site_name`;
- `controller_id = target_machine_id`, the stable existing Controller registry
  key;
- the normalized, direct-source-validated Controller URL it actually called;
- the Controller-provided current fleet token in the existing local token files.

No generic setup or enrollment helper may add side effects. All unlisted keys
in `controller_settings.json` are retained. The preflight requirement that
database mode is already `off` means repair does not change database mode or
database credentials.

The target executor reads the result back from disk, reloads the identity/token
caches, and returns the following sanitized result on the still-open dispatch
request:

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
private direct source matching the journaled validated Controller address (and,
after apply, the persisted repaired parent). It rejects every browser, cookie,
owner-session, unique-Hub-token, bearer, and query-credential substitute. The
status route remains adoptable after the 120-second redemption TTL because it
cannot authorize a new repair; it exists so a Controller restart or lost
dispatch response can confirm the already-bound operation without another
identity write.

The Controller marks `complete` only after the returned or adopted status has
the exact role, site ID, site name, stable ID, request ID, registry host, and
source host. It then invalidates the peer cache and wakes managed reconciliation
for the same registry key. A transport timeout after a locally confirmed apply
does not cause the Agent to roll back; the Controller recovers through the
status endpoint and never re-keys the registry.

The 24-hour post-redemption recovery window is independent of ticket issuance
TTL. A journal with a durably saved claim may resume exact forward apply during
that window even when the 120-second deadline has passed. If the claim response
was lost before it was saved, the journaled ticket may retrieve only the exact
already-redeemed claim during that window. At the recovery deadline, a
nonterminal executor may not begin or resume forward apply: it restores any
partial preimages, scrubs ticket/claim authority, and retains terminal sanitized
`abandoned` evidence for authenticated status adoption. A forward apply already
verified as complete remains complete. The Controller does not issue another
ticket or dispatch another Mac until it adopts a terminal outcome or, at the
24-hour boundary, atomically records audited `recovery_abandoned` and releases
the singleton gate because the expired claim has no remaining forward-apply
authority. That cleanup lets later Macs continue, but the unresolved original
target stays quarantined from a new ticket until authenticated status reports
`complete`, `rolled_back`, or `abandoned`.

## Eligibility and identity resolution

A Mac is automatically eligible only when all of these are true:

- it is a remote, currently registered stable machine key, not `local`;
- all its registered Studio rows resolve to one identical host;
- no second registered machine key uses that host or resolved private address;
- the host resolves only to allowed private LAN or Tailscale addresses and the
  direct peer remains observable;
- the Controller is in `controller` role with a current fleet token;
- the target executor accepts that exact current token and either advertises repair
  schema 1 or qualifies for the controlled older-Hub bootstrap below;
- the Controller can select a canonical private URL whose single resolved
  address will equal its direct dispatch source;
- the target has no environment-locked identity and database mode is `off`;
- there is no other active repair for that stable key.

The target's current role and saved parent URL are diagnostic evidence, not
eligibility gates. A registered executor in `standalone` or `controller` role,
or with a missing/wrong `parent_controller_url`, remains repairable when every
cryptographic, registry, source, token, environment, and database check passes.

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
- an issued row whose dispatch may be in flight remains the sole dispatch/apply
  candidate. The coordinator may perform read-only checks on later Macs, but it
  issues or dispatches no later ticket while the predecessor might still
  redeem or apply;
- at the 120-second boundary, one atomic transaction either observes redemption
  or changes a still-`issued` ticket to `expired`. Only the latter proves that
  no claim was returned and no forward-apply authority remains, allowing the
  next dispatch or a fresh later attempt;
- a redeemed/verifying row remains the sole apply candidate and is recovered
  through exact redemption adoption or the Agent status route after restart,
  including after ticket TTL;
- if it does not confirm, the coordinator waits through the bounded recovery
  deadline, then either adopts authenticated terminal evidence or atomically
  records audited cleanup and releases the global gate. The original target
  remains quarantined from re-ticketing until authenticated terminal status;
- no restart converts uncertainty into success, overlaps two applies, or
  permanently strands later targets; offline/read-only results may be collected
  while the bounded predecessor gate remains.

### Agent restart and partial writes

- before redemption, the executor's private journal retains the ticket only
  through its 120-second new-redemption deadline, so restart may resume the same
  request but never a different one;
- after redemption, the executor fsyncs the private claim journal before
  changing identity files; if the response was lost, the same journaled ticket
  may adopt only the already-redeemed claim after TTL;
- staged replacements are written and fsynced before atomic renames;
- on startup, an `applying` journal either completes the same exact forward
  repair and verifies it within the 24-hour recovery window, or restores every
  preimage and records `rolled_back`/`abandoned`;
- a restart after the 120-second deadline but before the recovery deadline is
  explicitly allowed to finish a previously redeemed repair and expose status;
- the executor never requests a different claim and never falls back to
  permanent enrollment.

### Races

- one target has at most one nonterminal request, and the coordinator has at
  most one dispatch/apply authority across the sequential batch;
- identical dispatches and status probes adopt; different request IDs conflict;
- site/controller identity, registry host, or fleet-token rotation between
  issue and redeem invalidates the ticket;
- a registry removal, duplicate host appearing, or host/address change produces
  `needs_review`; the coordinator does not follow the moving target;
- expiry is checked inside the same transaction that consumes the ticket, so a
  callback at the boundary either wins once or fails without a claim;
- an uncertain issued/in-flight predecessor prevents another issuance or
  dispatch until status adoption/terminal evidence or atomic unredeemed expiry;
- a redeemed predecessor prevents another dispatch until confirmation or its
  bounded, audited, authority-scrubbing terminal cleanup. Read-only eligibility
  and reachability checks may continue, and the batch advances immediately when
  the predecessor gate resolves.

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
| waiting_predecessor | **Waiting for the previous Mac's repair authority to finish. Safety checks continue.** |
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

- ticket entropy is at least 256 bits and its first-redemption lifetime is 120
  seconds;
- only SHA-256 ticket and fleet-token digests are durable on the Controller;
- tickets and tokens appear only in HTTPS/Tailscale/private request bodies,
  headers, and the target's mode-0600 transient recovery journal—never URLs,
  UI, analytics, errors, audit JSON, or ordinary logs;
- the plaintext ticket and exact claim are held in that journal only for
  first-redemption/adoption and crash-safe apply, then scrubbed after success,
  rollback, or bounded abandonment;
- request and batch IDs are non-secret and safe to display; observed identity
  and host evidence are bounded and sanitized.

### Threats and controls

| Threat | Required control |
| --- | --- |
| stolen fleet token | cannot initiate owner request or repair identity without a live bound ticket and exact source/callback checks |
| stolen ticket | cannot pass fleet transport, target/source, Controller snapshot, or validated-callback checks; first-redemption authority expires quickly |
| replay | atomic digest consumption; no new redemption after TTL; only the exact already-redeemed binding may recover through its bounded deadline |
| wrong registered machine | ticket target, registry key, source address, and returned stable ID must all match |
| duplicated endpoint | owner-visible `needs_review`; no ticket issued |
| DNS rebinding or moved host | issue-time private address snapshot plus redemption-time direct socket comparison; no redirect |
| malicious callback URL | target rejects credentials/paths/queries/fragments, redirects, proxy env, public/multiple resolution, and any address unequal to the direct dispatch peer |
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
| `callback_url_invalid` / `callback_source_mismatch` | needs review, no callback and no identity write | continue |
| `fleet_token_mismatch` | needs review/manual recovery, no claim | continue |
| `controller_snapshot_changed` | retryable only after fresh owner-visible evaluation | continue |
| `ticket_expired` | retryable, no claim | continue |
| `request_conflict` | adopt identical request or needs review | continue |
| `environment_locked` / `database_mode_unsafe` | needs review/manual recovery | continue |
| `apply_failed_rolled_back` | retryable after exact rollback | continue |
| `confirmation_pending` | hold dispatch gate; status/adopt exact predecessor | read-only checks only |
| `recovery_abandoned` | terminal audited authority scrub after deadline | continue |

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
   issuance. Keep all three service routes available to exact-current fleet
   authentication and source binding so already-redeemed executors can recover
   apply and Controllers can adopt status past the 120-second ticket TTL until
   terminal confirmation or the 24-hour bounded audited cleanup. Preserve
   sanitized terminal rows for diagnosis.

No fleet rollout begins from this design release.

## Acceptance test matrix

| Area | Required evidence |
| --- | --- |
| owner auth | Controller loopback and valid owner session can initiate; Hub token, fleet token, Agent session, missing session, cross-origin write, and non-Controller role cannot |
| route auth | all three service routes (`apply`, `redeem`, `status`) require exact-current fleet header plus private/source binding; unique Hub token, owner/Agent session, cookie, Authorization bearer, query credential, permanent code, and missing/wrong fleet token fail on each route |
| ticket storage | cryptographically random ticket; Controller DB contains digest only; 120-second new-redemption TTL; mode-0600 target recovery journal and body/header/log/UI redaction |
| exact binding | wrong request, target, source host, registry host, site, site name, Controller ID, token snapshot, or schema returns no claim |
| single use | first redemption succeeds once before TTL; a different or never-redeemed post-expiry request fails; only the exact already-redeemed source/request/target/digest adopts through the bounded recovery deadline |
| callback | supplied canonical URL succeeds only when credential-free private normalization resolves to exactly the direct dispatch socket peer; missing/wrong saved parent is accepted as evidence and replaced; malicious/wrong/public/multi-address callback, redirect, proxy env, and source mismatch fail without redemption |
| damaged local identity | registered target executor in wrong `standalone`/`controller` role and with missing or wrong parent is repairable; environment-locked identity and non-`off` database mode remain needs-review |
| ambiguity | duplicate host, multi-host machine rows, changed DNS/address, missing registry key, and conflicting stable ID are owner-visible and never guessed |
| idempotency | double-click, repeated POST, lost dispatch response, repeated Agent apply, and repeated status poll produce one request and one local write |
| Controller restart | queued order resumes; issued plaintext is not reconstructed; uncertain issued/in-flight predecessor blocks later issuance/dispatch while read-only checks continue; atomic unredeemed expiry, exact terminal/status adoption, or bounded audited post-redemption cleanup opens the global gate |
| Agent restart | restart before first redemption resumes only within TTL; restart after redemption—including after ticket TTL—adopts/saves the exact claim and resumes forward apply or restores/abandons preimages; journal secrets are scrubbed at terminal state |
| post-TTL recovery | lose the first redemption response, restart Controller and target after 120 seconds, adopt the exact redeemed claim, complete once, and status-adopt; a never-redeemed expired ticket still fails |
| races and overlap | concurrent target requests, token rotation, Controller identity change, registry edit/removal, expiry boundary, overlapping batches, and restart during in-flight dispatch prove maximum one apply authority and no second ticket before predecessor terminal or authority-free expiry |
| exact mutation | only role, site ID/name, stable controller ID, direct-source-validated persisted parent URL, and current fleet token differ after success |
| preservation | byte/digest or semantic equality for owner password/sessions, Hub token, permanent code, registry/endpoints, labels/profile, Off flags, jobs/artifacts, models/caches, update settings/history, and managed-release keys/state |
| rollback | injected failure at every staged rename restores exact preimages and returns `apply_failed_rolled_back`; no partial success is reported |
| confirmation | Controller requires exact returned/adopted identity before complete, invalidates peer cache, and wakes the existing stable managed-release row once |
| batch | stable machine ordering, maximum one forward-apply authority, read-only later checks behind an uncertain predecessor, continuation after authority-free expiry/terminal/audited cleanup and after offline/mismatch/needs-review/bootstrap/rollback outcomes, plus same-target quarantine until authenticated terminal status |
| older Hub | safe idle/no-active-release self-update can add endpoint and re-probe; every missing gate yields manual Hub update required; no Image/Voice/model update |
| UI | exact copy/states, retry vs review distinction, focus/dialog behavior, live-region behavior, keyboard operation, non-color cues, 44px targets, and narrow-screen stacking |
| schema/scope | capability snapshot remains `studiohub.site-capabilities` v3; existing managed-release state names unchanged; no dependency, launcher, sibling Studio, model, generation, or GenStudio diff |
| feature rollback | new issuance is disabled while all three strict service routes remain for redeemed apply/status recovery past TTL until confirmation or bounded audited cleanup |
| release truth | implementation release will add focused unit/integration/UI/restart tests, full suite and syntax checks, dependency audit, `git diff --check`, synchronized release metadata, review, commit, and controlled canary before fleet use |

## Implementation review gates

Before code is accepted, reviewers must answer yes to all of these:

- Can a fleet token without an owner-created ticket cause any identity write?
- Does every successful claim identify exactly one registry machine and direct
  private source under unchanged Controller/token snapshots?
- Can Controller and Agent restarts converge without persisting a plaintext
  ticket on the Controller or applying twice?
- Can a wrong-role registered executor with a missing/wrong saved parent accept
  only a callback origin that exactly matches the direct Controller source?
- Do all three service routes reject every credential form except the exact
  current fleet header and enforce their private/source binding independently?
- Does the singleton coordinator prevent a second ticket/dispatch while an
  issued or redeemed predecessor retains any possible apply authority?
- Can an already-redeemed repair recover and status-adopt after ticket TTL while
  an unredeemed expired ticket remains permanently unable to obtain a claim?
- Does a failed or ambiguous Mac always leave later batch Macs runnable?
- Are the permanent code, Controller registry key, owner credentials, machine
  metadata, workloads, models, settings, and durable release evidence unchanged?
- Is every ambiguity visible to the owner with no inference path?
- Is delete/re-enroll clearly an emergency with separate durable-row cleanup?
- Do release notes say precisely what is implemented, without treating this
  design-only 2.8.2 checkpoint as shipped repair behavior?
