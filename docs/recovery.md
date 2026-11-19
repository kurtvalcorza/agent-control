# Recovery and side-effect semantics

## Action ledger

Every externally visible mutation gets an immutable `ActionIntent` before invocation. The intent
binds authorization to the run, plan version, node, capability, argument digest, side-effect class,
reversibility and optional compensation metadata.

`ActionStarted` is emitted immediately before invocation. A verified effect produces an
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
`resume_run()` asks the registered `CapabilitySource` to re-derive the current identity and records
a `CapabilitySourceChecked` event:

- `MATCH` (identity re-derived and equal) — resume may continue;
- `MISMATCH` — `INPUT_CHANGED` observation, resume blocks; replan to adopt the new identity;
- `UNAVAILABLE`, a checker error, a MATCH without a re-derived identity, or **no registered
  source at all** — resume blocks.

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

The preferred mechanism gives the verifier the observed state directly. An `ExternalObservation`
with `state=None` (reference only) is re-fetched by the host-registered `ExternalObservationResolver`
(`ControlPlane(observation_resolver=...)`, RFC #6 Phase B1), which receives only the reference and
is separate from the observer. No resolver, a resolver that is the observer, a resolver error, or no
resolved state is `BLOCKED` before any verifier sees the observation. On the run path the attempt's
receipt then carries a `BLOCKED` verification, never PASS; in reconciliation it is the
"either | BLOCKED" row above (`RunBlocked`, no receipt). A resolved state follows the table above,
including the contradiction rows.

## Process restart

SQLite events are authoritative. Reconstruct a `ControlPlane` over the same store and call
`get_run()` / `inspect_run()` to rebuild state from genesis. If the recovered run is paused,
`resume_run()` applies the checkpoint and ambiguity checks above before any new action can start.

## Cancellation

Cancellation stops new work, walks verified reversible effects in reverse order, invokes the
configured `Compensator`, records every compensation attempt/result, releases unused reservations,
and transitions through `CANCELLING` to `CANCELLED`. Compensation failures remain visible in the
terminal outcome and event history.
