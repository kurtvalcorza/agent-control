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
gate reject RUN_ID GATE_ID --reason TEXT

event list RUN_ID
action list RUN_ID
checkpoint list RUN_ID
capability list
policy check POLICY.yaml
```

`--json` emits compact machine-readable JSON; otherwise JSON is pretty-printed for operator use.

The standalone CLI intentionally has no hidden default planner/model/capability configuration.
`run create` and all inspection/lifecycle commands work directly. Plan/step/execute require a host
integration or Python API runtime with providers registered; absence is reported explicitly.
