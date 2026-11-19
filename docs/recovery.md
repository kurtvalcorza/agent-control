# Recovery and side-effect semantics

## Action ledger

Every externally visible mutation gets an immutable `ActionIntent` before invocation. The intent
binds authorization to the run, plan version, node, capability, argument digest, side-effect class,
reversibility and optional compensation metadata.

`ActionStarted` is emitted immediately before the side effect may begin: before provider
invocation on the provider route, and when the handoff is dispatched (gate approval) on the actor
route (RFC #6 B3). A verified effect produces an
`ActionReceipt` with the actual action-start timestamp, completion timestamp, observed effects,
verification result, precondition references, artifact references and optional rollback reference.

Approvals cannot be reused after the plan version changes. Providers should use the intent ID as an
idempotency key when their API supports it.

## Budget reservations

Each executable step reserves its estimated cost/time/model-call/tool-call envelope before
execution. A reservation has exactly one terminal accounting outcome:

- `BudgetConsumed` after a successful invocation;
- `BudgetReleased` if invocation never begins;
- unresolved while a started side effect is ambiguous.

Idempotent failed attempts are conservatively charged before retry. An ambiguous reservation can
be reconciled explicitly with the operator API/CLI.

## Ambiguous effects

A failure after a non-idempotent side-effecting invocation is ambiguous because the remote effect
may have happened. The runtime does not retry it. It blocks the node, checkpoints and pauses.

The operator uses `reconcile_action()` or `action reconcile` to record either:

- effect occurred — write a verified receipt, record observed effects/rollback/artifact references,
  consume the reconciled budget and mark the node complete; or
- no effect — write a reconciliation receipt, release the reservation and return the node to
  pending execution.

Resume remains blocked until every started mutation is reconciled.

## Checkpoints

A checkpoint records:

- event sequence and SHA-256 digest of the replayed projection;
- plan version and budget state;
- unresolved action intents and budget reservations;
- working-memory snapshot reference;
- external-resource version references;
- `capability_source_versions`: the latest `ResolvedCapabilityProvenance` per capability.

When working memory is configured, its snapshot is recorded as an event and addressed by content
digest. `resume_run()` verifies the checkpoint digest, restores the recorded working-memory
snapshot and asks the configured resource-version provider to validate external dependencies.
Changed external resources emit `INPUT_CHANGED` and block resume so the run can be replanned.

Corrupt checkpoints, missing memory snapshots, started-but-unreceipted actions, and ambiguous
budget use fail closed.

### Capability source revalidation

Capability source provenance is control-plane-owned state, so its resume check does **not**
depend on the optional `ExternalResourceVersionProvider`. It runs on **every** resume, with or
without a checkpoint (a human-gate pause writes none), over the union of the provenance recorded
in the run's history and the latest checkpoint's `capability_source_versions`. For each one,
`resume_run()` asks the registered source to re-derive the current identity and records a
`CapabilitySourceChecked` event. A source with `derive(locator)` is asked blinded: it gets the
capability, source ref and revision but not the recorded digest, and the runtime compares what it
returns (`method: "derive"`). A source with only `check(recorded)` is asked under the documented
adapter obligation (`method: "check"`); see `providers.md`.

- `MATCH` (identity re-derived and equal) — resume may continue;
- `MISMATCH` — `INPUT_CHANGED` observation, resume blocks; replan to adopt the new identity;
- `UNAVAILABLE`, a checker or `derive()` error, a MATCH without a re-derived identity, a
  `derive()` that returns nothing or an identity for another source, or **no registered source at
  all** — resume blocks.

## Delegated commits

When pre-resolution returns `DELEGATION_REQUIRED`, a side-effecting node is a delegated commit:

- its `ActionIntent` is created in the dispatching run before invocation, with `delegated: true`;
- its verification must include `VerificationKind.EXTERNAL_STATE`, otherwise the node is blocked
  before invocation;
- the `EXTERNAL_STATE` verifier's subject is an `ExternalObservation` that the host-supplied
  `ExternalStateObserver` (`ControlPlane(external_observer=...)`) acquires from run/node/intent
  context. The executor's output is never passed to it. Other configured kinds still evaluate the
  executor's output, and the result aggregates fail-closed;
- no observer, no observation, or a non-PASS external-state result means no authoritative PASS
  receipt.

`reconcile_action()` keeps its existing behaviour for local actions. For a delegated intent the
operator's statement is recorded as an acknowledgement only; the runtime acts on independent
evidence. The `EXTERNAL_STATE` verifier contract is **PASS = the expected effect is observed,
FAIL = it is observed absent**; any other status is no evidence either way:

| Operator `occurred` | Independent result | Outcome |
| --- | --- | --- |
| `True` | PASS | authoritative receipt; node completes |
| `False` | FAIL (absent) | not-occurred reconciliation receipt; node returns to pending |
| `True` | FAIL | contradiction: `RunBlocked`, no receipt, node stays blocked |
| `False` | PASS | contradiction: `RunBlocked`, no receipt (prevents a duplicate re-execution) |
| either | no observer/observation, BLOCKED, INCONCLUSIVE | `RunBlocked`, no receipt |

**The RFC #6 §14 reading (owner ruling 2026-11-19, recorded in Phase B4).** A delegated
reconciliation without independent evidence writes **no receipt at all**: neither a PASS receipt
nor a receipt carrying a `BLOCKED` verification. The operator's statement is kept only as an
`operator_acknowledged` observation, the intent stays open and resume stays blocked, so later
evidence can still reconcile it. This differs on purpose from the run path, where a
provider-route attempt whose execution was observed (`CapabilityInvoked`) keeps its receipt
with a `BLOCKED` verification. The actor route observes no execution and so writes no receipt
until evidence passes. The B1 no-receipt ruling therefore extends to every attempt where no
execution was observed (no `CapabilityInvoked`), which includes every actor-route attempt. Covered by
`tests/test_delegated_verification.py::test_scenario_q_reconciliation_cannot_mint_delegated_pass_without_evidence`,
`tests/test_observation_resolver.py::test_reconcile_unresolved_reference_blocks_without_receipt`
and
`tests/test_actor_route.py::test_actor_reconcile_without_evidence_writes_no_receipt_then_evidence_passes`.

The preferred mechanism gives the verifier the observed state directly. An `ExternalObservation`
with `state=None` (reference only) is re-fetched by the host-registered `ExternalObservationResolver`
(`ControlPlane(observation_resolver=...)`, RFC #6 Phase B1), which receives only the reference and
is separate from the observer. No resolver, a resolver that is the observer, a resolver error, or no
resolved state is `BLOCKED` before any verifier sees the observation. On the run path the attempt's
receipt then carries a `BLOCKED` verification, never PASS; in reconciliation it is the
"either | BLOCKED" row above (`RunBlocked`, no receipt). A resolved state follows the table above,
including the contradiction rows.

### Actor-routed delegation (Phase B3)

A delegated capability whose contract declares `DelegationRoute.ACTOR` (see
`docs/providers.md`) never reaches a provider:

1. policy is evaluated on node-level inputs (the node's side-effect class, the run risk and
   `reversible=False`) and records `PolicyEvaluated`; DENY, justification and required
   verification block exactly as on the provider route, before any intent or gate exists;
2. the `ActionIntent` is recorded, with `delegated: true`, `delegation_route: "actor"`,
   `authorization_state: pending` and `reversible: false` (there is no provider descriptor, so
   cancellation never compensates it);
3. a `HumanGate` opens for that intent, the node is `BLOCKED` and the run pauses;
4. **approval dispatches the handoff.** `approve_gate()` records `ActionAuthorized` and
   `ActionStarted` together: the other agent or session (for example an Agent Relay handoff) may
   act from this moment. The run stays paused and the node blocked;
5. when the handoff returns, `complete_actor_handoff(run_id, intent_id)` verifies it only through
   the independent `EXTERNAL_STATE` path above (no `CapabilityResolved` or `CapabilityInvoked`):
   - PASS: an authoritative receipt whose `started_at` is the dispatch time; the node completes;
   - FAIL (the effect is observed absent): the run fails with `VERIFICATION_FAILED`, no receipt;
   - no observer, no observation, BLOCKED or INCONCLUSIVE: no receipt; the node stays `BLOCKED`
     and the run pauses. Resume stays blocked on the started action until `reconcile_action()`
     finds independent evidence, under the reconciliation table above.

**Route binding.** A delegated intent records the route it was created for
(`ActionIntent.delegation_route`), and the runtime never executes an open intent on another route:
it blocks the node with a reason naming the bound route. A dispatched actor handoff is verified by
its binding, not by the host's current pre-resolution. A restart on a host that does have the
missing facility therefore never invokes a provider for it.

Rejection and an unanswered gate before dispatch behave exactly as for any other `HumanGate`:
rejection records `ActionRejected` and a `human_rejected` observation and leaves the node blocked,
with no `ActionStarted`, because nothing was dispatched. The runtime has no gate timeout, so an
undecided gate keeps the run paused until a decision or cancellation. After dispatch the gate is
resolved and cannot be rejected; the effect stays started, so it is recorded through
`complete_actor_handoff()` or `reconcile_action()`. Cancelling a run with a dispatched,
unreturned handoff records an `unknown_failure` observation and leaves the intent started and
reconcilable.

Guards around an outstanding handoff (B3 review round 2):

- `approve_gate()` on an actor gate refuses with "run is not paused" before it records anything,
  so a handoff is never dispatched from a running, cancelled or finished run;
- `cancel_run()` closes every pending gate (`HumanGateResolved` with `status: rejected`,
  `decided_by: runtime:cancel_run`, plus `ActionRejected` for its undispatched intent), so a
  cancelled run has no approvable gate. This applies to provider gates too;
- `request_replan()` and `invalidate_and_replan()` refuse while a dispatched actor handoff has no
  receipt. A replan would bump the plan version, orphan that intent and let a new gate dispatch
  the same effect again; record the return first. Refusing is the smaller safe option, because
  carrying the intent across plan versions would need every plan-version check on intents to learn
  about it;
- a checkpoint taken while a handoff is outstanding (for example `resume_run()` then
  `pause_run()`) lists the started intent as unresolved. A plain `resume_run()` still refuses it,
  but `complete_actor_handoff()` for that same intent is admitted, because its return is verified
  in the same call. Every other started intent in the checkpoint still blocks.

## Process restart

SQLite events are authoritative. Reconstruct a `ControlPlane` over the same store and call
`get_run()` / `inspect_run()` to rebuild state from genesis. If the recovered run is paused,
`resume_run()` applies the checkpoint and ambiguity checks above before any new action can start.

## Cancellation

Cancellation stops new work, walks verified reversible effects in reverse order, invokes the
configured `Compensator`, records every compensation attempt/result, releases unused reservations,
and transitions through `CANCELLING` to `CANCELLED`. Compensation failures remain visible in the
terminal outcome and event history.
