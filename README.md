# Agent Control Plane

`agent-control-plane` is a host-neutral, event-sourced runtime for bounded, recoverable,
goal-directed AI-agent execution.

It does **control semantics**, not domain work. A planner may propose what to do, and capability
providers may perform analytics, research, routing, retrieval, or mutations; the control plane
owns the run lifecycle, admissible state transitions, budgets, policy gates, side-effect records,
verification, recovery, and audit history.

> The LLM may propose decisions; the runtime owns state transitions, policy enforcement,
> budgets, action accounting, persistence, and verification semantics.

The implementation follows [Issue #1](https://github.com/kurtvalcorza/agent-control-plane/issues/1).

## What is implemented

- explicit `Goal`, `Plan`, and typed DAG `PlanNode` contracts;
- deterministic run and node state machines;
- append-only SQLite event store with schema versions and replay validation;
- provider-neutral capability registry with in-process, subprocess, MCP-boundary, and
  `agent-router` reference adapters;
- hard run budgets for cost, elapsed time, model calls, tool calls, retries, and replans;
- declarative policy engine with fail-closed parsing and runtime policy decision events;
- immutable side-effect intents, authorization gates, receipts, idempotency-friendly action IDs,
  stale-approval protection, ambiguous-effect recovery, and compensation hooks;
- deterministic/human verifier registry with fail-closed missing-verifier behavior;
- checkpoint digests validated against replayed event state before resume;
- bounded transient retry, observation taxonomy, dependency-aware invalidation and replanning;
- L0/L1 working memory with expiry, supersession, provenance, relevance selection, and hard
  context-size budgets plus a provider-neutral persistent-memory interface;
- run evaluation, failure clustering, regression-case extraction, drift signals, and a
  human-reviewed policy/skill improvement proposal workflow with **no autonomous self-modification**;
- operator CLI for run/gate/event/action/checkpoint/capability/policy inspection and control.

## Architecture

```text
                   host / harness
       Codex · Claude Code · Antigravity · etc.
                         |
                         v
              +-----------------------+
              |  agent-control-plane  |
              |-----------------------|
              | goal + plan DAG       |
              | deterministic FSM     |
              | capability registry   |
              | policy + budgets      |
              | action ledger         |
              | verification          |
              | checkpoints/recovery  |
              | replanning + memory   |
              +-----------+-----------+
                          |
                  capability contracts
            +-------------+-------------+
            v             v             v
      agent-router   agentic-analytics  agentic-research
                                      
                          v
                   memory providers
                  e.g. agentic-vault
```

The core has no dependency on a particular LLM provider, agent host, MCP SDK, analytics engine,
knowledge store, or routing policy.

## Quick start

Requires Python 3.12+.

```bash
python -m pip install -e '.[dev]'
pytest
```

A deterministic in-process example is available at `examples/deterministic_run.py`.

```bash
python examples/deterministic_run.py
```

Create a run from a JSON goal document:

```json
{
  "objective": "Produce a verified result",
  "success_criteria": ["result verified"],
  "constraints": ["no destructive writes"],
  "risk_level": "low",
  "budget": {"max_tool_calls": 10, "max_replans": 2}
}
```

```bash
agent-control-plane --db control.sqlite3 run create goal.json
agent-control-plane --db control.sqlite3 run inspect RUN_ID
agent-control-plane --db control.sqlite3 event list RUN_ID
```

The standalone CLI deliberately does not invent an LLM or capability configuration. Planning and
execution are performed through the Python API or a host integration that registers a `Planner`
and capability providers.

## Runtime model

```text
CREATED -> PLANNING -> READY -> EXECUTING -> VERIFYING
               ^         |          |            |
               |         |          | retry      +-> COMPLETED
               |         |          +------------+
               |         +---------------- replan
               |
active state -> PAUSING -> PAUSED -> READY/PLANNING
active state -> CANCELLING -> CANCELLED
active state -----------------------> FAILED
```

A run reaches `COMPLETED` only when all current plan nodes have passed their required verification.
`BLOCKED` or `INCONCLUSIVE` verification never counts as success.

## Safety model

Side-effecting capabilities are anything beyond `NONE`/`READ_ONLY`. They must declare their
side-effect class and reversibility. The runtime creates an `ActionIntent` before invocation and
an `ActionReceipt` after a verified effect. High-impact classes (destructive, external
communication, financial, privileged) require explicit human approval under the default policy.

If a process fails after a side-effecting invocation and the runtime cannot prove whether the
external effect occurred, it does **not** retry. The run checkpoints and pauses for operator
resolution. Resume is refused while unresolved ambiguous side effects remain.

## Integration boundary

Existing systems remain authoritative for their own domain semantics:

- **agent-router** selects an eligible executor/model; the control plane owns global lifecycle and
  resource envelopes.
- **agentic-analytics** remains authoritative for analytical execution, artifacts, evidence and
  domain validation; control-plane events reference provider-native IDs rather than duplicating
  that provenance.
- **agentic-research** exposes high-level research capabilities and retains its human methodology
  gates.
- **agentic-vault** can act as a future durable-memory provider; the control plane owns active
  working-context selection.

See `docs/integrations.md` and `docs/providers.md`.

## Non-goals

This project is not:

- an all-purpose agent framework;
- an LLM SDK abstraction;
- a vector database;
- an analytics or research implementation;
- a host-specific plugin;
- a replacement for `agent-router`;
- a distributed workflow engine;
- an autonomous self-modifying agent.

## Development

```bash
ruff check .
mypy src
pytest
```

CI runs the same gates on Python 3.12 and 3.13.

## Documentation

- `docs/architecture.md` — control model, state machine and events
- `docs/providers.md` — capability and planner authoring guide
- `docs/policy.md` — policy format and examples
- `docs/recovery.md` — intents, receipts, checkpoints, restart and cancellation semantics
- `docs/cli.md` — operator CLI
- `docs/security.md` — trust boundaries and fail-closed behavior
- `docs/integrations.md` — sibling-project integration contracts
- `docs/versioning.md` — independent schema/API versioning policy

## License

MIT
