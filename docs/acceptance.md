# Issue #1 acceptance matrix

This matrix is the release checklist for the first stable milestone in Issue #1. Each requirement is
backed by an implementation surface and executable coverage; CI remains the independent gate for
formatting, lint, typing, and tests.

| Requirement | Implementation | Primary coverage |
| --- | --- | --- |
| Host-neutral runtime | Python API, SQLite, in-process/subprocess/MCP boundaries | `tests/test_runtime.py` |
| Explicit goals and completion | full success-criterion + requested-output coverage | `tests/test_goal_policy.py` |
| Validated plan DAG | cycle/dependency/terminal validation | `tests/test_adversarial.py` |
| Deterministic state machines | central run/node transition tables | `tests/test_runtime.py` |
| Provider-neutral capabilities | registry, authorization envelope, health metadata | `tests/test_goal_policy.py`, `tests/test_adversarial.py` |
| Verification before completion | verifier registry; missing verifier blocks | `tests/test_adversarial.py` |
| Authoritative replayable events | append-only SQLite, sequence/schema validation | `tests/test_runtime.py`, `tests/test_adversarial.py` |
| Bounded execution | cost/time/model/tool/retry/replan budgets + reservations | `tests/test_runtime.py`, `tests/test_audit_recovery.py` |
| Side-effect governance | intent-before-effect, receipts, stale approval protection | `tests/test_runtime.py`, `tests/test_ambiguous_effect.py` |
| Human approval gates | runtime-enforced destructive/high-impact gates | `tests/test_runtime.py` |
| Safe pause/resume/cancel | checkpoint digest, reconciliation, compensation | `tests/test_recovery.py`, `tests/test_audit_recovery.py` |
| Working memory | provenance, expiry/supersession, context budget, checkpoint snapshot | `tests/test_audit_recovery.py` |
| External-state recovery | resource-version snapshot/validation | `tests/test_audit_recovery.py` |
| Adaptive replanning | observation taxonomy, structural diff/invalidation, bounded loops | `tests/test_replan.py`, `tests/test_adversarial.py` |
| Configurable observation actions | complete `ObservationPolicy` mapping | `tests/test_stable_contract.py` |
| Policy enforcement | versioned declarative rules, risk/permission/cost/budget controls | `tests/test_goal_policy.py`, `tests/test_stable_contract.py` |
| Audit-safe secrets | inline-secret rejection + recursive event redaction | `tests/test_audit_recovery.py` |
| Ambiguous-effect fail-closed behavior | started write pauses until operator reconciliation | `tests/test_ambiguous_effect.py`, `tests/test_audit_recovery.py` |
| Idempotent retry safety | intent reuse + conservative failed-attempt accounting | `tests/test_adversarial.py` |
| Operator inspectability | goal/plan/state, provider reason, verification, policy, budget, actions | `tests/test_stable_contract.py` |
| Failure taxonomy | typed terminal categories and causal metadata contract | `src/agent_control_plane/errors.py` |
| Evaluation/drift hooks | run evaluation, regression extraction, drift and reviewed proposals | `src/agent_control_plane/evaluation.py`, `src/agent_control_plane/drift.py`, `src/agent_control_plane/improvement.py` |
| No autonomous self-modification | proposal validate/approve/promote workflow only | `src/agent_control_plane/improvement.py` |
| Schema compatibility | explicit supported event schema versions; unknown versions rejected | `tests/test_adversarial.py` |
| CI quality gates | Ruff, strict Mypy, pytest on Python 3.12/3.13 | `.github/workflows/ci.yml` |

## Adversarial scenarios

The test suite explicitly covers the Issue #1 adversarial list:

- cyclic DAG;
- unavailable capability;
- planner understates a destructive provider as read-only;
- duplicate/idempotent retry after a transient failure;
- missing verification provider;
- corrupted checkpoint;
- event-stream gap and unsupported schema version;
- repeated replan loop;
- stale authorization after plan supersession;
- external resource changes between checkpoint and resume;
- ambiguous side effect after invocation;
- invalid/unknown policy operators.

## Stable-milestone rule

Issue #1 is implementation-complete when the full PR stack is merged and the CI quality gates have
executed successfully. A GitHub Actions failure that occurs before checkout and records zero job
steps is tracked as an infrastructure blocker, not treated as a passing code gate.
