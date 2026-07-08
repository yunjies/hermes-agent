# Approval Service Landing Spec

Status: first-delivery implementation spec
Scope: Approval Service only

## Goal

Approval Service is the runtime service for approval decision, policy checks,
audit, and preflight validation.

It owns:

- Approval request creation, lookup, and state transitions.
- Policy-based approver resolution.
- Effective decision recording.
- Append-only audit events.
- Preflight validation before a producer applies an approved artifact.
- Callback result recording after a producer attempts apply.

It does not own:

- Business artifact generation.
- Business artifact persistence.
- Business apply, promotion, rollback, retry, or recovery.
- Business-specific lifecycle state beyond the approval request.

Core rule:

```text
Approval Service owns decision and audit.
Producer subsystem owns artifact and apply.
```

Approval Service is profile-scoped runtime infrastructure. It is not a
developer-machine task board, a Codex-specific workflow, or a private work
directory convention. Runtime state lives under the active Hermes profile home.
The default profile is identified as `default`; profile display names or soul
names are not used as storage owners.

## Runtime Boundary

The standard integration flow is:

```text
producer creates artifact
  -> producer calls ensure_request_for_artifact(...)
  -> Approval Service creates or reuses approval_request
  -> Approval Service evaluates policy gates
  -> approver records decision through Approval Service
  -> producer calls preflight(...) before apply
  -> producer applies artifact
  -> producer reports callback_result
```

Approval and application are separate facts:

```text
approved != applied
rejected != artifact deleted
expired != artifact removed
callback_failed != approval revoked
```

Approval Service must never call producer apply code directly. It only records a
decision and exposes a callback contract the producer can consume.

## Core Schema

### ApprovalRequest

```yaml
approval_request:
  id: req_...
  request_type:
    domain: write | skill | memory | workflow | action | system
    operation: propose | write | update | enable | disable | delete | execute | accept | reject
    subtype: string | null

  source:
    profile_id: string | null
    session_id: string | null
    requested_by: user | profile | system
    request_message_id: string | null

  owner:
    profile_id: string | null
    scope: string | null

  target:
    ref: string
    artifact_id: string
    artifact_type: string
    artifact_hash: string
    summary: string
    affected_paths:
      - string

  risk:
    level: L0 | L1 | L2 | L3 | L4 | L5
    external_side_effect: boolean
    secret_access: boolean
    deletion: boolean
    cost_or_purchase: boolean
    cross_scope: boolean
    authority_change: boolean

  evidence:
    reason: string
    diff_ref: string | null
    source_refs:
      - string
    verification_plan: string | null
    rollback_plan: string | null

  policy:
    selected_rule: string | null
    required_approver: string | null
    # Derived by Approval Service policy evaluation. Producers must not set this.
    allowed_decision_paths:
      - user
      - owner
      - delegated
      - runtime_auto

  callback:
    producer: string
    operation: string
    target_id: string
    idempotency_key: string | null

  status: pending | approved | rejected | expired | revoked | escalated

  decision:
    result: approved | rejected | escalated | null
    decided_by:
      type: user | profile | system | runtime_auto
      id: string
      display_name: string | null
    reason: string | null
    policy_rule: string | null
    decided_at: string | null

  # Producer-reported result only. This is not approval state and does not make
  # Approval Service the owner of the producer's business lifecycle.
  callback_result:
    result: not_started | applied | apply_failed | skipped
    reason: string | null
    applied_ref: string | null
    recorded_at: string | null

  timestamps:
    created_at: string
    updated_at: string
    expires_at: string | null
```

The approval request id and business artifact id are always distinct:

```text
approval_request_id = req_...
artifact_id = producer-owned id
```

Approval state, producer result, and audit facts are separate layers:

```text
approval status = pending/approved/rejected/expired/revoked/escalated
callback_result = not_started/applied/apply_failed/skipped
audit event = append-only facts
```

## State Machine

Approval Service request states:

```text
pending
  Waiting for an effective approval decision.

approved
  Approved. Producer still needs preflight before apply.

rejected
  Rejected. Producer must not apply.

expired
  Expired. Producer must not apply.

revoked
  Previously valid approval was revoked. Producer must not apply.

escalated
  Policy could not complete approval at the current authority level.
```

Allowed transitions:

```text
pending -> approved
pending -> rejected
pending -> expired
pending -> escalated

approved -> revoked
approved -> expired

escalated -> approved
escalated -> rejected
escalated -> expired
```

Disallowed transitions:

```text
rejected -> approved
expired -> approved
revoked -> approved
revoked -> pending
```

If a rejected, expired, or revoked artifact should be reconsidered, the producer
must create a new approval request.

## Policy Gates

### Prompt Gate

Conversation text is not an effective approval decision. A model response that
says "approved" or "can execute" is advisory only.

Only a structured Approval Service decision is effective:

```yaml
status: approved
decision:
  result: approved
  decided_at: "..."
```

Producer subsystems must not apply artifacts based on natural-language approval
text alone.

### Owner Gate

Every approval request must declare the profile that owns the decision scope
unless a policy explicitly allows ownerless requests for that request type.
Subsystem names such as `memory` and `skills` are producer domains, not
`owner.profile_id` values.

Missing owner without an allow rule:

```text
pending -> escalated
```

### Scope Gate

The approver must match the request scope.

Example rule:

```yaml
rule_id: low_risk_write_owner_approval
match:
  domain: write
  max_risk: L2
require:
  approver: owner
deny:
  deletion: true
  secret_access: true
  authority_change: true
```

Scope mismatch escalates the request.

### Risk Gate

These request properties require escalation unless an explicit higher-authority
rule allows them:

```text
deletion = true
secret_access = true
cost_or_purchase = true
authority_change = true
external_side_effect = true
risk.level >= L4
```

### Artifact Hash Gate

Every approval request must bind the artifact hash being approved. If the hash
changes after approval, preflight fails and the producer must create a new
approval request.

### Rollback Gate

Risk level `L3` and above requires a rollback plan unless the policy rule
explicitly marks rollback as not applicable.

Missing rollback plan escalates the request.

### Verification Gate

Requests that will be applied by a producer must include a verification plan,
unless the policy rule explicitly marks verification as not applicable.

Missing verification plan escalates the request.

### Expiry Gate

Expired requests cannot be approved or applied:

```text
now > expires_at -> expired
```

### Revoke Gate

Revoked requests cannot be applied:

```text
status = revoked -> preflight fail
```

## Commands

Generic approval commands:

```text
/approval list
/approval list --status pending
/approval show <request_id>
/approval status <request_id>
/approval approve <request_id> --reason "<reason>"
/approval reject <request_id> --reason "<reason>"
/approval revoke <request_id> --reason "<reason>"
/approval expire <request_id>
/approval examples list
/approval mode
```

Compatibility command:

```text
/approve <id>
```

Resolution behavior:

```text
if id starts with req_:
  treat id as approval_request_id
else:
  treat id as a producer artifact id
  resolve artifact through registered producer resolvers
  call ensure_request_for_artifact(...)
  show the resolved approval request summary
  record a decision only if the command is an explicit approval action
```

Resolving a producer artifact id must not approve by itself. Resolution only
binds the command to a request. The decision is recorded only after request
uniqueness, expiry, artifact hash, and policy gates pass.

Known write-approval artifact ids use:

```text
memory:<pending_id>
skills:<pending_id>
```

No-id approval is allowed only when all conditions are true:

```text
current session has exactly one pending approval request
that request was recently displayed in the session
the user expression is explicit
the request is not expired
the artifact hash is unchanged
```

If multiple pending requests exist:

```text
Approval result: AMBIGUOUS
Reason: current session has multiple pending approval requests.
Next: use /approval approve <request_id>.
```

If no pending request exists:

```text
Approval result: NO_PENDING_REQUEST
Reason: current session has no pending approval request to bind.
```

## Producer Contract

### ensure_request_for_artifact

Producer subsystems call this after creating an artifact:

```python
ensure_request_for_artifact(
    request_type={...},
    source={...},
    owner={...},
    target={...},
    risk={...},
    evidence={...},
    callback={...},
)
```

Requirements:

- Same artifact id and same artifact hash reuse an existing non-terminal
  request (`pending`, `approved`, or `escalated`) when it is not expired or
  revoked.
- Same artifact id and different artifact hash create a new request.
- Terminal requests (`rejected`, `expired`, `revoked`) are not reopened. A
  reconsidered artifact must create a new request.
- Request id and artifact id remain separate.
- Approval Service reads only the contract fields, not producer internals.

### get_decision

Producer subsystems query effective approval state:

```python
get_decision(request_id)
```

Example return:

```yaml
request_id: req_...
status: approved
decision:
  result: approved
  decided_by:
    type: user
    id: user
  reason: "Approved by explicit command."
  policy_rule: explicit_user_approval
```

Producer apply is allowed only when:

```text
status == approved
preflight(...) passes
```

### preflight

Producer subsystems must call preflight immediately before apply:

```python
preflight(request_id, artifact_hash, executor)
```

Checks:

```text
request.status == approved
request not expired
request not revoked
artifact_hash unchanged
risk fields unchanged
target unchanged
owner unchanged
rollback_plan still present when required
verification_plan still present when required
callback producer matches caller
executor matches the registered producer identity
request has not already been applied unless idempotent
```

Approval Service does not decide producer-specific apply permission. Producers
must enforce their own business permissions after Approval Service preflight
passes.

Return:

```yaml
preflight:
  result: pass | fail
  request_id: req_...
  reason: string | null
  failed_gates:
    - artifact_hash
    - expiry
```

If preflight fails, the producer must not apply.

### record_callback_result

Producer subsystems report apply outcome:

```python
record_callback_result(
    request_id,
    result="applied" | "apply_failed" | "skipped",
    reason="...",
    applied_ref="...",
)
```

Callback failure does not erase the approval decision. It records a separate
fact:

```text
decision.result = approved
callback_result.result = apply_failed
```

`record_callback_result()` must not change `status`. It only updates
`callback_result` and appends a callback audit event.

## Audit

Every state change must append an audit event.

Audit event schema:

```yaml
audit_event:
  event_id: evt_...
  request_id: req_...
  event_type:
    request_created
    policy_evaluated
    decision_recorded
    request_expired
    request_revoked
    preflight_passed
    preflight_failed
    preflight_rejected
    callback_recorded
    callback_rejected
    request_update_failed

  actor:
    type: user | profile | system | runtime_auto
    id: string
    display_name: string | null

  request_snapshot:
    request_type: string
    artifact_id: string
    artifact_hash: string
    owner: string | null
    risk_level: string

  decision:
    result: approved | rejected | escalated | null
    policy_rule: string | null
    reason: string | null

  constraints_checked:
    owner: pass | fail | skipped
    scope: pass | fail | skipped
    risk: pass | fail | skipped
    rollback: pass | fail | skipped
    verification: pass | fail | skipped
    expiry: pass | fail | skipped
    artifact_hash: pass | fail | skipped

  callback:
    producer: string | null
    operation: string | null
    result: applied | apply_failed | skipped | null

  timestamp: string
```

Audit is append-only. Request records may be updated for current state, but
every update must have a corresponding audit event.

## Decision Examples

Manual approval decisions are durable examples. Approval Service should retain
enough structured data to support later review of repeated human decisions.

This is not automatic policy learning. Approval Service records examples and
can export candidates; it must not rewrite policy rules by itself.

### Example Record

Each manual decision should preserve:

```yaml
decision_example:
  request_id: req_...
  request_type:
    domain: string
    operation: string
    subtype: string | null
  target:
    artifact_type: string
    summary: string
    affected_paths:
      - string
  owner:
    profile_id: string | null
    scope: string | null
  risk:
    level: string
    external_side_effect: boolean
    secret_access: boolean
    deletion: boolean
    cost_or_purchase: boolean
    cross_scope: boolean
    authority_change: boolean
  decision:
    result: approved | rejected
    decided_by:
      type: user | profile | system | runtime_auto
      id: string
    reason: string
    decided_at: string
  policy:
    rule: string | null
    decision_path: string | null
  outcome:
    callback_result: applied | apply_failed | skipped | not_started
    verification_result: pass | fail | unknown
    rollback_used: boolean
  tags:
    - string
```

### Candidate Export

Approval Service may expose a read-only export for repeated decision patterns:

```text
/approval examples list
/approval examples show <request_id>
```

Example listing and display must redact sensitive fields by default. Redaction
must cover secrets, raw diff content, private message ids, and sensitive
absolute paths in `reason`, `summary`, `affected_paths`, and `source_refs`.

Policy candidate export is outside first delivery. If added later, exported
candidates must be advisory:

```yaml
policy_candidate:
  source: decision_examples
  matched_examples:
    - req_...
  proposed_rule:
    match: {...}
    require: {...}
    deny: {...}
  confidence:
    support_count: integer
    conflict_count: integer
  status: proposed
```

Approval Service does not accept or activate `policy_candidate`. A separate
policy-management path must review, approve, and write policy changes.

## Session Output

Every approval action must print a structured summary.

Approved:

```text
Approval result: APPROVED
Request: req_001
Type: write.update
Target: artifact_123
Owner: owner_id

Decision path: user
Decision by: user
Policy: explicit_user_approval
Reason: User approved the pending request.

Constraints checked:
- owner: pass
- scope: pass
- risk: pass
- rollback: present
- verification: present
- expiry: pass
- artifact_hash: pass

Result: approved, waiting for producer callback.
```

Rejected:

```text
Approval result: REJECTED
Request: req_002
Type: workflow.enable
Target: artifact_456

Decision path: user
Decision by: user
Policy: explicit_user_rejection
Reason: Requested operation should not be applied.

Result: rejected, producer apply is blocked.
```

Escalated:

```text
Approval result: ESCALATED
Request: req_003
Type: action.execute
Target: artifact_789

Policy: high_risk_requires_user
Reason: Request includes external side effect and missing rollback plan.

Constraints checked:
- risk: fail
- rollback: fail
- verification: pass

Result: pending higher approval.
```

## Storage

First-phase storage uses JSON snapshots plus an append-only audit log and small
indexes:

```text
<active-profile-hermes-home>/approvals/
  requests.json
  audit.jsonl
  decision_examples.jsonl
  indexes/
    by_status.json
    by_artifact.json
    by_owner.json
```

Requirements:

- `audit.jsonl` is append-only.
- `requests.json` records current request snapshots.
- Request updates must write audit events in the same logical operation.
- Request update plus audit append must be protected by a file lock.
- Writers must use temporary files plus atomic rename for index updates.
- Crash recovery must detect request/audit mismatch and orphaned partial
  updates.
- Duplicate request creation must be protected by the same lock.
- Artifact id to request id lookup is indexed.
- Queries by status, owner, and artifact are supported.
- Duplicate request creation is prevented for the same artifact id and hash.

SQLite can replace JSONL later without changing producer contracts.

Approval Service evaluates policy but does not author, mutate, or activate
policy rules. If a policy file is added later, it is read-only from the
service's perspective.

## Rollout Plan

### Phase 1: Core Service

- Add ApprovalRequest schema.
- Add ApprovalStore.
- Add audit log writer.
- Add policy evaluator.
- Add `/approval list`, `/approval show`, and `/approval status`.

### Phase 2: Decision Commands

- Add `/approval approve`.
- Add `/approval reject`.
- Add `/approval revoke`.
- Add `/approval expire`.
- Add structured session output.

### Phase 3: Compatibility

- Support `/approve req_*`.
- Support `/approval approve <artifact_id>` through producer resolvers.
- Support no-id approval for one unambiguous pending request.
- Return `AMBIGUOUS` for multiple pending requests.
- Return `NO_PENDING_REQUEST` when no request can be bound.

### Phase 4: Producer API

- Add `ensure_request_for_artifact()`.
- Add `get_decision()`.
- Add `preflight()`.
- Add `record_callback_result()`.
- Document producer resolver registration.

### Phase 5: Preflight Enforcement

- Require producers to call preflight before apply.
- Fail if request is expired or revoked.
- Fail if artifact hash changed.
- Fail if target, owner, or risk changed.
- Require callback result after apply attempt.

### Phase 6: Runtime Mode

Add runtime mode:

```text
HERMES_APPROVAL_SERVICE_MODE=disabled|shadow|enforce
```

Mode behavior:

```text
disabled
  Approval Service does not create or consume requests.

shadow
  Approval Service creates request and audit data, but producer apply paths are
  not blocked by Approval Service decisions.

enforce
  Producer apply requires Approval Service approval and passing preflight.
```

Historical request and audit data are never deleted by mode changes.

### Phase 7: Shared Surfaces

- CLI and gateway expose `/approval`.
- Dashboard/API exposes `/api/approval`.
- `/approve` remains reserved for dangerous-command approval on gateway
  surfaces.

## Test Plan

### Schema and Store

- Create request.
- Load request.
- Update status.
- Append audit.
- Keep request and audit ordering consistent.
- Query by artifact id.
- Reuse request for same artifact id and hash.
- Create new request for same artifact id and different hash.

### Policy Gates

- Missing owner escalates.
- Scope mismatch escalates.
- High risk escalates.
- Deletion escalates.
- Secret access escalates.
- Missing rollback escalates when required.
- Missing verification escalates when required.
- Expired request cannot be approved.
- Revoked request fails preflight.

### Commands

- `/approval list`
- `/approval show req_x`
- `/approval status req_x`
- `/approval approve req_x`
- `/approval reject req_x`
- `/approval revoke req_x`
- `/approve req_x`
- `/approve old_artifact_id`
- `/approve` with one pending request.
- `/approve` with multiple pending requests returns `AMBIGUOUS`.
- `/approve` with no pending request returns `NO_PENDING_REQUEST`.

### Preflight

- Approved and unchanged artifact passes.
- Pending request fails.
- Rejected request fails.
- Expired request fails.
- Revoked request fails.
- Changed artifact hash fails.
- Changed target fails.
- Missing rollback after approval fails when rollback is required.
- Duplicate apply without idempotency fails.

### Callback

- Apply success records `callback_result.result=applied`.
- Apply failure records `callback_result.result=apply_failed`.
- Apply failure does not erase approval decision.
- Producer mismatch is rejected.
- Target mismatch is rejected.

### Audit

- `request_created` event exists.
- `policy_evaluated` event exists.
- `decision_recorded` event exists.
- `preflight_passed` or `preflight_failed` event exists.
- `callback_recorded` event exists.
- Every event includes actor, request id, policy, reason, and timestamp.
- Audit remains append-only.

## First Delivery Scope

First delivery includes:

- ApprovalRequest schema.
- ApprovalStore.
- Audit log.
- Policy evaluator.
- `/approval` read commands.
- `/approval` decision commands.
- `/approval approve req_*` compatibility.
- Gateway `/approval`.
- Dashboard `/api/approval`.
- Producer API stubs and contracts.
- Preflight function.
- Callback result recording.
- Structured approval output.
- Runtime mode.
- Unit tests and smoke tests.
- This engineering spec.

First delivery excludes:

- Business apply implementation.
- Business artifact generation.
- High-risk automatic approval.
- Authority expansion approval.
- Approval reuse across changed artifacts.
- Non-approval error handling.
- Policy candidate export from decision examples.

## Acceptance Criteria

Approval Service first delivery is complete when:

```text
1. Every approval request has a request id.
2. Approval request id and producer artifact id are distinct.
3. Effective decisions can only be recorded by Approval Service.
4. Every decision appends an audit event.
5. Every approval command emits structured session output.
6. At least one producer apply path is enforced by preflight, with tests proving
   apply is blocked without valid approval.
7. Approval Service does not call producer apply code.
8. Callback result is recorded separately from approval decision.
9. Approval Service never derives business lifecycle state from callback_result.
10. /approval approve req_* works.
11. Runtime mode can run in disabled, shadow, and enforce modes.
12. CLI, gateway, and API can inspect the same request store.
```
