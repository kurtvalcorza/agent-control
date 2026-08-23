# CLI

Global options:

```bash
agent-control-plane --db control.sqlite3 --json ...
```

Commands:

```text
run create GOAL.json
run inspect RUN_ID
run plan RUN_ID
run step RUN_ID
run execute RUN_ID
run pause RUN_ID
run resume RUN_ID
run cancel RUN_ID

gate list RUN_ID
gate approve RUN_ID GATE_ID
gate reject RUN_ID GATE_ID

event list RUN_ID

action list RUN_ID
action reconcile RUN_ID INTENT_ID --occurred|--no-effect \
  [--cost USD] [--effect TEXT] [--rollback-ref REF] \
  [--artifact-ref REF] [--precondition-ref REF]

verification retry RUN_ID NODE_ID

checkpoint list RUN_ID

budget list RUN_ID
budget consume RUN_ID RESERVATION_ID [--cost USD]
budget release RUN_ID RESERVATION_ID

capability list
capability health [NAME]

policy check POLICY.yaml
```

`--json` emits compact machine-readable JSON; otherwise JSON is pretty-printed for operator use.

`run inspect` is the primary operator view. It includes the replayed run projection, ready nodes,
pending gates, actions/receipts, recent observations, checkpoints, active budget reservations,
provider-selection rationale, verification results, policy decisions, remaining budget,
working-memory state and capability health metadata.

The standalone CLI intentionally has no hidden planner, model or capability configuration. It can
create/inspect/control persisted runs and validate policy documents. Planning and execution require
a host integration or Python API runtime that registers a planner and capability providers.

Ambiguous side effects are never resolved implicitly. Use `action reconcile` to state whether the
external effect occurred; use the budget commands only when an operator must explicitly reconcile
resource use independent of an action receipt.

When execution succeeded but verification blocked because a verifier was unavailable, register the
verifier in the host runtime and use `verification retry RUN_ID NODE_ID`. Re-verification uses the
persisted node output and does **not** invoke the capability again.
