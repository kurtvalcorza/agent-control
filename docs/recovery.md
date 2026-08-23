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
- external-resource version references.

When working memory is configured, its snapshot is recorded as an event and addressed by content
digest. `resume_run()` verifies the checkpoint digest, restores the recorded working-memory
snapshot and asks the configured resource-version provider to validate external dependencies.
Changed external resources emit `INPUT_CHANGED` and block resume so the run can be replanned.

Corrupt checkpoints, missing memory snapshots, started-but-unreceipted actions, and ambiguous
budget use fail closed.

## Process restart

SQLite events are authoritative. Reconstruct a `ControlPlane` over the same store and call
`get_run()` / `inspect_run()` to rebuild state from genesis. If the recovered run is paused,
`resume_run()` applies the checkpoint and ambiguity checks above before any new action can start.

## Cancellation

Cancellation stops new work, walks verified reversible effects in reverse order, invokes the
configured `Compensator`, records every compensation attempt/result, releases unused reservations,
and transitions through `CANCELLING` to `CANCELLED`. Compensation failures remain visible in the
terminal outcome and event history.
