# Agent Control Plane

`agent-control-plane` is a host-neutral, event-sourced runtime for bounded, recoverable,
goal-directed AI-agent execution.

It does **control semantics**, not domain work. Planners may propose what to do and capability
providers may perform analytics, research, routing, retrieval, or mutations; the control plane
owns lifecycle, admissible transitions, budgets, policy, authorization, side-effect accounting,
verification, recovery, and audit history.

> The LLM may propose decisions; the runtime owns state transitions, policy enforcement,
> budgets, action accounting, persistence, and verification semantics.

The implementation follows [Issue #1](https://github.com/kurtvalcorza/agent-control-plane/issues/1).
The executable stable-milestone mapping is documented in `docs/acceptance.md`.

## Implemented stable milestone

- explicit `Goal`, `Plan`, and typed DAG `PlanNode` contracts;
- completion only when **all** success criteria and requested outputs are represented and every
  current plan node has passed required verification;
- deterministic run/node state machines;
- append-only SQLite event store with monotonic sequences, explicit supported schema versions,
  replay validation, and fail-closed corrupt/out-of-order handling;
- provider-neutral capability registry with in-process, subprocess, MCP-boundary, and
  `agent-router` reference adapters;
- host-controlled capability permission and risk envelopes plus provider health metadata;
- hard run budgets for cost, elapsed time, model calls, tool calls, retries, and replans;
- reserve -> consume/release accounting, including conservative accounting for idempotent failed
  attempts and explicit reconciliation of ambiguous budget use;
- versioned declarative policy rules for allow/deny/human approval, verification requirements,
  reversibility, justification, capability risk/permissions, step-cost ceilings, and tighter run
  budget ceilings;
- immutable side-effect intents created before invocation, attributable authorization gates,
  receipts with real action-start/completion timestamps, preconditions/artifacts/rollback data,
  stale-approval protection, compensation hooks, and operator reconciliation;
- deterministic/human verifier registry with fail-closed missing-verifier behavior;
- checkpoint digests validated against replayed state before resume;
- working-memory checkpoint snapshots plus external-resource version capture/revalidation;
- bounded transient retries, complete observation taxonomy, configurable observation->control
  mapping, structural dependency invalidation, explicit plan diffs, and bounded replanning;
- L0/L1 working memory with expiry, supersession, provenance, relevance selection, and hard
  context-size budgets plus a provider-neutral persistent-memory interface;
- audit redaction for credential-shaped values and rejection of inline plan secrets in favor of
  `secret://`, `env://`, or `vault://` references;
- run evaluation, failure clustering, regression-case extraction, drift signals, and a
  human-reviewed policy/skill improvement workflow with **no autonomous self-modification**;
- operator CLI/API inspection for goal/plan/state, provider selection reason, verification,
  policy decisions, remaining budget, actions, checkpoints, reservations, memory, and recovery.

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
                          |
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
ruff check .
mypy src
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
  "requested_outputs": ["report"],
  "risk_level": "low",
  "budget": {"max_tool_calls": 10, "max_replans": 2}
}
```

```bash
agent-control-plane --db control.sqlite3 run create goal.json
agent-control-plane --db control.sqlite3 run inspect RUN_ID
agent-control-plane --db control.sqlite3 event list RUN_ID
agent-control-plane --db control.sqlite3 action list RUN_ID
agent-control-plane --db control.sqlite3 budget list RUN_ID
agent-control-plane --db control.sqlite3 capability health
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

A run reaches `COMPLETED` only when the full goal contract is covered and all current plan nodes
have passed required verification. `BLOCKED` or `INCONCLUSIVE` is never equivalent to PASS.

## Safety and recovery model

Every externally visible mutation creates an `ActionIntent` before provider invocation. High-impact
or policy-selected actions stop at a runtime human gate. A successful effect gets a verified
`ActionReceipt` linked to the true action-start time.

If a capability can fail after a mutation may have occurred, the runtime does **not** silently retry
unless the provider declares an idempotent contract. Ambiguous effects remain paused with their
budget reservations unresolved until an operator reconciles the external effect and resource use.
Resume validates the checkpoint projection digest, working-memory snapshot, external-resource
versions, unresolved actions, and unresolved budget reservations.

Inline credential material in plan inputs is rejected. Hosts should pass opaque secret references;
audit events recursively redact credential-shaped fields and exception text.

## Integration boundary

Existing systems remain authoritative for their own domain semantics:

- **agent-router** selects an eligible executor/model; the control plane owns global lifecycle and
  resource envelopes.
- **agentic-analytics** remains authoritative for analytical execution, artifacts, evidence and
  domain validation; control-plane events reference provider-native IDs rather than duplicating
  that provenance.
- **agentic-research** exposes high-level research capabilities and retains its human methodology
  gates.
- **agentic-vault** can act as a durable-memory provider; the control plane owns active
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

## Quality gates

CI runs the same quality gates on Python 3.12 and 3.13:

```bash
ruff check .
mypy src
pytest
```

The stable milestone additionally requires the acceptance/adversarial matrix in
`docs/acceptance.md`. A GitHub Actions run that fails before checkout with zero recorded job steps
is treated as an infrastructure blocker, not as a passing code gate.

## Documentation

- `docs/acceptance.md` — Issue #1 stable-milestone and adversarial-test mapping
- `docs/architecture.md` — control model, state machine and events
- `docs/providers.md` — capability and planner authoring guide
- `docs/policy.md` — versioned policy format and examples
- `docs/recovery.md` — intents, receipts, reservations, checkpoints and reconciliation
- `docs/cli.md` — operator CLI
- `docs/security.md` — trust boundaries, secrets and fail-closed behavior
- `docs/integrations.md` — sibling-project integration contracts
- `docs/versioning.md` — independent schema/API versioning policy

## License

MIT
