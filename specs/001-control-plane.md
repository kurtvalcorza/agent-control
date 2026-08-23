# Specification 001 — Agent Control Plane

The normative product specification is GitHub Issue #1:

https://github.com/kurtvalcorza/agent-control-plane/issues/1

This repository implements that specification as an event-sourced Python 3.12+ runtime. The issue
remains the requirement source; code, tests and documentation are the executable evidence.

## Milestone mapping

- Phase 1: contracts, DAG, FSM, SQLite event store/replay, capability registry, verifier, CLI.
- Phase 2: action intents/receipts, policy/gates, checkpoint/recovery, compensation.
- Phase 3: observations/retries, invalidation/replanning, budgets, router integration boundary.
- Phase 4: supersession/expiry/context-budget working memory and persistent-memory interface.
- Phase 5: evaluations, regression extraction, drift signals and human-reviewed improvement
  proposals without autonomous self-modification.
