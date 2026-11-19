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
| Failure taxonomy | typed terminal categories and causal metadata contract | `src/agent_control/errors.py` |
| Evaluation/drift hooks | run evaluation, regression extraction, drift and reviewed proposals | `src/agent_control/evaluation.py`, `src/agent_control/drift.py`, `src/agent_control/improvement.py` |
| No autonomous self-modification | proposal validate/approve/promote workflow only | `src/agent_control/improvement.py` |
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

## RFC #6 Phase A — capability provenance and runtime pre-resolution

| Requirement | Implementation | Primary coverage |
| --- | --- | --- |
| Per-run `ResolvedCapabilityProvenance` (not on the descriptor) | `provenance.py`, `CapabilityPreResolved`, `CapabilityResolved.source_provenance` | `tests/test_capability_provenance.py` (A) |
| Canonical SHA-256 identity | `canonical_digest`, typed digest validation | `tests/test_capability_provenance.py` |
| Checkpointed source identity + unconditional resume revalidation | `capability_source_versions`, `CapabilitySourceChecked` | `tests/test_capability_provenance.py` (B–E) |
| Small runtime vocabulary, no `HUMAN_AVAILABLE` | `RuntimeRequirement` | `tests/test_capability_provenance.py` (vocabulary) |
| Human absence gates instead of failing pre-resolution | existing `HumanGate` after `SATISFIED` pre-resolution | `tests/test_phase_a_review_fixes.py` (R: sourced irreversible write pauses at a gate, no failure) |
| Revalidation on every resume, including gate pauses | `_revalidate_capability_sources` over history + checkpoint | `tests/test_phase_a_review_fixes.py` (AC6-R1) |
| Schema-"1" streams still replay and resume | `SUPPORTED_EVENT_SCHEMA_VERSIONS` | `tests/test_phase_a_review_fixes.py` with `tests/fixtures/schema_v1_paused_run.json` recorded from a7d6dc9 |
| Pre-resolution precedes registry resolution | `pre_resolve`, `ControlPlane._pre_resolve_node` | `tests/test_capability_provenance.py` (F, G, H, K) |
| Fail-closed clause subset rule | `VerificationSpec.requires_clause_ids` | `tests/test_capability_provenance.py` (I, J) |
| Delegated commit verification is independent | `ActionIntent.delegated`, `ExternalStateObserver` | `tests/test_delegated_verification.py` (M, N, O) |
| Reconciliation cannot bypass delegated verification | delegated guard in `reconcile_action()` | `tests/test_delegated_verification.py` (Q + later-evidence + local-unchanged) |
| Blinded source re-derivation (Phase B2, AC6-R3) | `CapabilitySourceLocator` (no digest field), `DerivingCapabilitySource.derive`, `derive_capability_source`, `CapabilitySourceChecked.method` | `tests/test_blinded_derive.py` (locator has no digest, derive never sees it, wrong digest MISMATCH, raise/None/untyped/other source UNAVAILABLE, check-only still works, both uses derive only) |
| Observation references are independently resolvable (Phase B1) | `ExternalObservationResolver`, `ControlPlane(observation_resolver=...)`, `_resolve_observation_reference` | `tests/test_observation_resolver.py` (P: resolved PASS, unresolved BLOCKED, contradicting FAIL, resolver input, reconcile incl. AC6-R2) |

Scenario L (an Agent Relay handoff ends at an actor/`HumanGate` boundary) has no executable route in
Phase A. The runtime registers no relay provider. Scenario P (reference-only observations) is
covered by Phase B1: a separate host resolver re-fetches the reference; without one, a
reference-only observation (`state=None`) is still blocked before the verifier
(`tests/test_phase_a_review_fixes.py`, AC6-R7, and `tests/test_observation_resolver.py`).
