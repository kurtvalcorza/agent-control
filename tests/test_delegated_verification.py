"""RFC #6 Phase A: delegated commits need independent EXTERNAL_STATE evidence (s13, s14)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_control import (
    CallbackCapabilitySource,
    CallbackExternalStateObserver,
    CapabilityContract,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilitySourceRegistry,
    ControlPlane,
    EventType,
    ExternalObservation,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    ResolvedCapabilityProvenance,
    RunBlocked,
    RunState,
    RuntimeRequirement,
    SideEffectClass,
    SourceCheckResult,
    SourceCheckStatus,
    SQLiteEventStore,
    VerificationKind,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
    VerifierRegistry,
    canonical_digest,
)
from agent_control.models import NodeStatus

PROVENANCE = ResolvedCapabilityProvenance(
    capability="write", source_ref="git:example/skills#write", digest=canonical_digest(b"w")
)
DELEGATING = CapabilityContract(
    provenance=PROVENANCE,
    runtime_requirements=frozenset({RuntimeRequirement.GPU}),
    delegation_permitted=True,
)
EXTERNAL = VerificationSpec(VerificationKind.DETERMINISTIC, additional_kinds=(
    VerificationKind.EXTERNAL_STATE,
))


class Planner:
    def __init__(self, verification: VerificationSpec) -> None:
        self.verification = verification

    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    "write",
                    "delegated write",
                    ("write",),
                    side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                    verification=self.verification,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def _source(contract: CapabilityContract) -> CapabilitySourceRegistry:
    sources = CapabilitySourceRegistry()
    sources.register(
        "write",
        CallbackCapabilitySource(
            lambda _: contract,
            lambda recorded: SourceCheckResult(SourceCheckStatus.MATCH, current=recorded),
        ),
    )
    return sources


def _runtime(
    handler: Any,
    *,
    verification: VerificationSpec = EXTERNAL,
    observer: Any = None,
    seen_subjects: list[Any] | None = None,
    contract: CapabilityContract = DELEGATING,
) -> ControlPlane:
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                "write",
                "1",
                "remote-executor",
                side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                reversible=True,
            ),
            handler,
        )
    )
    verifiers = VerifierRegistry()
    subjects = seen_subjects if seen_subjects is not None else []

    def external_state(spec: VerificationSpec, subject: Any, context: dict) -> VerificationResult:
        subjects.append(subject)
        if isinstance(subject, ExternalObservation) and subject.state == {"exists": True}:
            return VerificationResult(VerificationStatus.PASS, "record observed")
        return VerificationResult(VerificationStatus.FAIL, "record not observed")

    verifiers.register(VerificationKind.EXTERNAL_STATE, external_state)
    return ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(verification),
        capabilities=registry,
        verifiers=verifiers,
        capability_sources=_source(contract),
        external_observer=observer,
    )


def _observer(state: dict, contexts: list[dict] | None = None) -> CallbackExternalStateObserver:
    def observe(context: dict) -> ExternalObservation:
        if contexts is not None:
            contexts.append(dict(context))
        return ExternalObservation(
            ref="crm:record/42@etag-7",
            observer="dispatching-run",
            observed_at="2026-11-19T00:00:00Z",
            state=state,
            verification_method="refetch",
        )

    return CallbackExternalStateObserver(observe)


def _self_reporting_executor(_: dict) -> dict:
    return {"verified": True, "effects": ["created record 42"]}


def _receipts(runtime: ControlPlane, run_id: str) -> list[dict]:
    return [a["receipt"] for a in runtime.list_actions(run_id) if a["receipt"] is not None]


def test_delegated_commit_without_external_state_kind_blocks_before_invocation() -> None:
    calls: list[str] = []
    runtime = _runtime(
        lambda _: calls.append("x") or True,
        verification=VerificationSpec(VerificationKind.DETERMINISTIC),
    )
    run = runtime.create_run(Goal("delegate", ("done",)))
    with pytest.raises(RunBlocked, match="external_state"):
        runtime.execute_next(run.id)
    assert calls == []
    assert runtime.get_run(run.id).node_status["write"] == NodeStatus.BLOCKED


def test_scenario_m_and_n_self_report_without_independent_observation_blocks() -> None:
    subjects: list[Any] = []
    runtime = _runtime(_self_reporting_executor, seen_subjects=subjects)
    run = runtime.create_run(Goal("delegate", ("done",)))
    paused = runtime.run_until_blocked(run.id)
    assert paused.state == RunState.PAUSED
    assert paused.node_status["write"] == NodeStatus.BLOCKED
    events = runtime.list_events(run.id)
    created = next(e for e in events if e.type == EventType.ACTION_INTENT_CREATED)
    started = next(e for e in events if e.type == EventType.ACTION_STARTED)
    assert created.sequence < started.sequence
    assert created.payload["intent"]["delegated"] is True
    assert subjects == []  # no observer: the executor's report never reached the verifier
    assert all(r["verification"]["status"] != "pass" for r in _receipts(runtime, run.id))


def test_external_state_verifier_never_receives_executor_output() -> None:
    subjects: list[Any] = []
    contexts: list[dict] = []
    runtime = _runtime(
        _self_reporting_executor,
        observer=_observer({"exists": False}, contexts),
        seen_subjects=subjects,
    )
    run = runtime.create_run(Goal("delegate", ("done",)))
    final = runtime.run_until_blocked(run.id)
    assert final.state == RunState.FAILED
    assert subjects and all(isinstance(item, ExternalObservation) for item in subjects)
    assert contexts and all("output" not in context for context in contexts)
    assert all(r["verification"]["status"] != "pass" for r in _receipts(runtime, run.id))


def test_scenario_o_independent_observation_verifies_delegated_commit() -> None:
    runtime = _runtime(_self_reporting_executor, observer=_observer({"exists": True}))
    run = runtime.create_run(Goal("delegate", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    receipt = _receipts(runtime, run.id)[-1]
    assert receipt["verification"]["status"] == "pass"
    evidence = receipt["verification"]["evidence"]
    assert evidence["delegated_commit"] is True
    external = evidence["verifiers"]["external_state"]["evidence"]
    assert external["independent_observation"]["ref"] == "crm:record/42@etag-7"
    assert external["independent_observation"]["observer"] == "dispatching-run"


def test_local_execution_is_not_classified_as_delegated() -> None:
    contract = CapabilityContract(provenance=PROVENANCE)
    runtime = _runtime(
        _self_reporting_executor,
        verification=VerificationSpec(VerificationKind.DETERMINISTIC),
        contract=contract,
    )
    run = runtime.create_run(Goal("local", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    intent = runtime.list_actions(run.id)[0]["intent"]
    assert intent["delegated"] is False


def _ambiguous(_: dict) -> dict:
    raise ConnectionError("executor connection lost after submission")


def test_scenario_q_reconciliation_cannot_mint_delegated_pass_without_evidence() -> None:
    runtime = _runtime(_ambiguous)
    run = runtime.create_run(Goal("delegate", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.PAUSED
    intent_id = runtime.list_actions(run.id)[0]["intent"]["id"]
    with pytest.raises(RunBlocked, match="independent external-state evidence"):
        runtime.reconcile_action(run.id, intent_id, occurred=True)
    assert runtime.list_actions(run.id)[0]["receipt"] is None
    assert runtime.get_run(run.id).node_status["write"] == NodeStatus.BLOCKED
    assert any(o.get("operator_acknowledged") for o in runtime.list_observations(run.id))
    with pytest.raises(RunBlocked, match="started side effect"):
        runtime.resume_run(run.id)


def test_delegated_reconciliation_succeeds_once_independent_evidence_exists() -> None:
    runtime = _runtime(_ambiguous)
    run = runtime.create_run(Goal("delegate", ("done",)))
    runtime.run_until_blocked(run.id)
    intent_id = runtime.list_actions(run.id)[0]["intent"]["id"]
    with pytest.raises(RunBlocked):
        runtime.reconcile_action(run.id, intent_id, occurred=True)
    runtime.external_observer = _observer({"exists": True})
    reconciled = runtime.reconcile_action(run.id, intent_id, occurred=True)
    assert reconciled.node_status["write"] == NodeStatus.COMPLETED
    receipt = runtime.list_actions(run.id)[0]["receipt"]
    assert receipt["verification"]["status"] == "pass"
    assert receipt["verification"]["evidence"]["delegated_commit"] is True
    assert receipt["verification"]["evidence"]["external_state"]["status"] == "pass"


def test_local_reconciliation_behaviour_is_unchanged() -> None:
    runtime = _runtime(
        _ambiguous,
        verification=VerificationSpec(VerificationKind.DETERMINISTIC),
        contract=CapabilityContract(provenance=PROVENANCE),
    )
    run = runtime.create_run(Goal("local", ("done",)))
    runtime.run_until_blocked(run.id)
    intent_id = runtime.list_actions(run.id)[0]["intent"]["id"]
    reconciled = runtime.reconcile_action(run.id, intent_id, occurred=True)
    assert reconciled.node_status["write"] == NodeStatus.COMPLETED
    evidence = runtime.list_actions(run.id)[0]["receipt"]["verification"]["evidence"]
    assert evidence == {"operator_reconciled": True, "occurred": True}
