---
kind: studiohub.shared-contract
schema_version: 1
contract_name: replace-with-contract-name
contract_version: 1
status: proposed
producer: Studio Hub or exact sibling
consumers:
  - exact consumer
owner: controller
task_id: TASK-000
---

# Purpose and boundary

Describe what crosses the boundary and what explicitly remains owned by
GenStudio, Studio Hub, or the sibling worker.

## Producer and consumers

| Role | Component | Responsibility |
|---|---|---|
| Producer | Exact service | Replace |
| Consumer | Exact service | Replace |

GenStudio never dispatches directly to a sibling. Studio Hub remains the site
controller and sibling Studios remain execution workers.

## Authentication boundary

- Authenticated caller:
- Credential or identity handle, never secret value:
- Authorization decision owner:
- Replay protection:
- Fields that must never be logged or reported:

## Capability identity

- Exact internal model or capability ID:
- Immutable model/runtime revision:
- Adapter identity and revision:
- Operation:
- Contract hash or canonicalization rule:
- Hardware and concurrency requirements:

An observed sibling candidate is not approval. State the deliberate approval
and desired-state gate separately.

## Request payload

```json
{
  "schema": "replace.request",
  "schema_version": 1
}
```

Document required, optional, forbidden, bounded, and private fields.

## Response payload

```json
{
  "schema": "replace.response",
  "schema_version": 1
}
```

Document sanitized public errors separately from private worker evidence.

## State machine

| State | Meaning | May retry? | Terminal? | Required evidence |
|---|---|---|---|---|
| accepted | Worker or controller accepted ownership | No automatic duplicate retry | No | Durable ID and fencing |
| rejected | No ownership accepted | Only when explicitly safe | Yes | Stable reason code |
| uncertain | Acceptance cannot be proven or disproven | Never automatically | No | Recovery query or controller decision |
| succeeded | Verified result is durable | No | Yes | Artifact/result validation |
| failed | Assigned attempt ended unsuccessfully | Policy-specific | Yes | Sanitized stable code |

Add queued, running, cancelled, expired, superseded, or other states as needed.

## Idempotency, fencing, and retry behavior

- Idempotency key scope and lifetime:
- Duplicate request behavior:
- Attempt or lease fencing:
- Safe rejected-request retry:
- Accepted-job retry prohibition:
- Uncertain-outcome recovery:
- Terminal replay behavior:

## Timeouts, polling, and callbacks

- Connection timeout:
- Accepted-job execution timeout:
- Poll interval and backoff:
- Callback or webhook authentication:
- Expiry and retention:
- Last-good cache behavior during outage:

## Backward compatibility

- Previous contract versions accepted:
- Additive fields and unknown-field behavior:
- Breaking changes:
- Feature negotiation or capability gate:
- Deprecation window:

If backward compatibility is impossible, attach the explicit coordinated
rollout plan and state why.

## Studio Hub rollout order

1. Persist and read old/new contract safely.
2. Add fake-worker and compatibility coverage.
3. Upgrade controller behavior without advertising unavailable capability.
4. Upgrade and verify required siblings.
5. Enable desired state only after evidence is current.
6. Roll out GenStudio consumer behavior last unless the contract proves another
   safe order.

Replace this example with the exact approved order.

## Required sibling changes

Create one separate task per sibling repository. List exact repository,
version, paths, capability fields, adapter behavior, and verification. Do not
edit a sibling from a Studio Hub task.

## Tests

### Fake-worker tests

- Accepted, rejected, uncertain, and terminal paths.
- Idempotent replay and fencing.
- Timeouts, stale state, incompatible revision, and sanitized errors.
- No direct GenStudio-to-sibling request.

### Integration tests

- Version negotiation across controller and worker.
- Last-good persistence across restart or outage.
- Eligibility, concurrency, and capacity evidence.
- Recovery after partial rollout and rollback.

## Recovery and rollback

- Safe rollback version/order:
- Persistence compatibility:
- Treatment of queued and accepted jobs:
- Last-good desired state:
- Partial-download or artifact preservation:
- Operator evidence and stop conditions:

## Security and privacy

- Data classification and retention:
- Credential boundary:
- Customer asset handling:
- Log and report redaction:
- Internal endpoint protection:
- Threats and mitigations:

Never include real tokens, credentials, private endpoints, customer content, or
raw worker errors in this contract file.

## Controller approval

- Reviewer:
- Decision: pending/approved/rejected
- Reviewed contract version and hash:
- Approved rollout plan:
- Date:
