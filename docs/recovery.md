# Recovery and side-effect semantics

## Action ledger

Every externally visible mutation gets an immutable `ActionIntent` before invocation. Its digest
binds authorization to the plan version, node, capability and arguments. An approval cannot be
reused after any of those change.

After execution the runtime writes an `ActionReceipt` containing observed effects, verification,
and an optional provider rollback reference. Providers should use the action ID as an idempotency
key when their API supports it.

## Ambiguous effects

A capability error after a side-effecting invocation is ambiguous: the remote effect may have
happened. The runtime never silently retries it. It marks the node blocked, checkpoints and pauses.
An operator must reconcile the action via `resolve_ambiguous_action()` before replanning/resume.

## Checkpoints

A checkpoint records the event sequence and SHA-256 digest of the replayed run projection, plan
version, budget state, unresolved action intents and optional external resource versions.
`resume_run()` validates the latest checkpoint against event replay. A mismatched/corrupt
checkpoint fails closed.

## Process restart

`recover_run()`:

- returns stable/terminal states unchanged;
- pauses if an action is pending or a mutation is ambiguous;
- replans pure computation interrupted in `EXECUTING`;
- reruns verification over the persisted output when interrupted in `VERIFYING`.

## Cancellation

Cancellation stops new work, walks completed reversible actions in reverse order, invokes the
configured `Compensator`, records every attempt/result, and finishes as either `cancelled` or
`cancelled_with_compensation_failures`.
