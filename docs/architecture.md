# Architecture

## Boundary

The control plane owns control flow and auditability. Planners propose plans; capability providers
perform domain work. Neither can mutate run state directly.

## Run lifecycle

Run state transitions are centrally validated in `state_machine.py`. Node state transitions are
validated independently. Invalid transitions are errors, not suggestions to the model.

A plan is a validated DAG. Every terminal node must contribute to an explicit success criterion.
A plan may be revised only through a new version; superseded versions remain in the event stream.
Completed nodes are preserved only when their structural signature is unchanged. An invalidated
node invalidates its downstream dependency closure before replanning.

## Event model

SQLite stores immutable events with:

- unique event ID;
- run ID;
- monotonically increasing per-run sequence;
- event type and independent schema version;
- UTC timestamp;
- actor plus optional causation/correlation IDs;
- JSON payload.

`project_run()` reconstructs current state from genesis and rejects gaps, duplicate IDs, mixed run
IDs, missing schema versions, and non-UTC timestamps. Mutable projection state is never the source
of truth.

## Deterministic vs probabilistic components

Deterministic runtime:

- state transitions;
- DAG validation;
- policy enforcement;
- budget accounting;
- action intent/receipt lifecycle;
- checkpoint validation;
- event persistence/replay;
- verifier result handling.

Replaceable/probabilistic boundary:

- plan creation/revision;
- capability implementations;
- optional model-based verifier implementations;
- persistent-memory providers.

This separation is deliberate: a model can recommend a transition but cannot manufacture one.
