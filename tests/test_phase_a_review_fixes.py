"""Regression tests for the RFC #6 Phase A review findings AC6-R1..R7."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_delegated_verification import _ambiguous, _observer
from test_delegated_verification import _runtime as _delegated_runtime

from agent_control import (
    CallbackExternalStateObserver,
    CapabilityContract,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilitySourceRegistry,
    ControlPlane,
    Event,
    EventType,
    ExternalObservation,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    ResolvedCapabilityProvenance,
    RunBlocked,
    RunState,
    SideEffectClass,
    SourceCheckResult,
    SourceCheckStatus,
    SQLiteEventStore,
    canonical_digest,
    check_capability_source,
)
from agent_control.models import NodeStatus

FIXTURES = Path(__file__).parent / "fixtures"


class PayloadSource:
    """Source double that re-derives identity from a backing payload; it never echoes."""

    def __init__(self, payload: bytes = b"v1") -> None:
        self.payload = payload
        self.available = True
        self.checks = 0

    def _provenance(self) -> ResolvedCapabilityProvenance:
        return ResolvedCapabilityProvenance(
            capability="work", source_ref="git:example#work", digest=canonical_digest(self.payload)
        )

    def load(self, capability: str) -> CapabilityContract:
        return CapabilityContract(provenance=self._provenance())

    def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
        self.checks += 1
        if not self.available:
            return SourceCheckResult(SourceCheckStatus.UNAVAILABLE, reason="unreachable")
        current = self._provenance()
        status = (
            SourceCheckStatus.MATCH
            if current.same_identity(recorded)
            else SourceCheckStatus.MISMATCH
        )
        return SourceCheckResult(status, current=current)


class Planner:
    def __init__(self, side_effect: SideEffectClass = SideEffectClass.NONE) -> None:
        self.side_effect = side_effect

    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    "a",
                    "a",
                    ("work",),
                    side_effect_class=self.side_effect,
                ),
                PlanNode(
                    "b",
                    "b",
                    ("work",),
                    dependencies=("a",),
                    side_effect_class=self.side_effect,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def _runtime(
    source: PayloadSource | None,
    *,
    side_effect: SideEffectClass = SideEffectClass.NONE,
    store: SQLiteEventStore | None = None,
    planner: Any = None,
) -> ControlPlane:
    registry = CapabilityRegistry()
    # Irreversible write -> the default policy requires a human gate (pause, no checkpoint).
    registry.register(
        InProcessCapability(
            CapabilityDescriptor("work", "1", "local", side_effect_class=side_effect),
            lambda _: True,
        )
    )
    sources = None
    if source is not None:
        sources = CapabilitySourceRegistry()
        sources.register("work", source)
    return ControlPlane(
        store=store or SQLiteEventStore(":memory:"),
        planner=planner or Planner(side_effect),
        capabilities=registry,
        capability_sources=sources,
    )


def _gate_paused(source: PayloadSource) -> tuple[ControlPlane, str]:
    runtime = _runtime(source, side_effect=SideEffectClass.REVERSIBLE_WRITE)
    run = runtime.create_run(Goal("gate", ("done",)))
    paused = runtime.run_until_blocked(run.id)
    assert paused.state == RunState.PAUSED
    assert runtime.list_checkpoints(run.id) == []  # the human-gate pause writes no checkpoint
    return runtime, run.id


# --- AC6-R1 + AC6-R6 ----------------------------------------------------------------------


def test_r6_scenario_r_human_absence_is_a_gate_not_unsatisfied() -> None:
    runtime, run_id = _gate_paused(PayloadSource())
    pre = [
        e.payload
        for e in runtime.list_events(run_id)
        if e.type == EventType.CAPABILITY_PRE_RESOLVED
    ]
    assert pre and pre[-1]["pre_resolution"]["state"] == "satisfied"
    assert runtime.list_pending_gates(run_id)
    assert runtime.get_run(run_id).state == RunState.PAUSED
    assert not [e for e in runtime.list_events(run_id) if e.type == EventType.RUN_FAILED]


def test_r1_gate_resume_without_checkpoint_still_revalidates_and_blocks() -> None:
    source = PayloadSource()
    runtime, run_id = _gate_paused(source)
    source.available = False
    gate = runtime.list_pending_gates(run_id)[0]
    with pytest.raises(RunBlocked, match="cannot be revalidated"):
        runtime.approve_gate(run_id, gate["id"])
    assert source.checks == 1
    assert runtime.get_run(run_id).state == RunState.PAUSED
    source.available = True
    assert runtime.resume_run(run_id).state == RunState.READY
    after = runtime.run_until_blocked(run_id)  # node "b" is also gated
    assert after.node_status["a"] == NodeStatus.COMPLETED
    assert source.checks == 2


def test_r1_revalidation_covers_provenance_recorded_after_the_last_checkpoint() -> None:
    source = PayloadSource()
    runtime = _runtime(source, side_effect=SideEffectClass.REVERSIBLE_WRITE)
    run = runtime.create_run(Goal("late", ("done",)))
    runtime.plan_run(run.id)
    runtime.pause_run(run.id)  # checkpoint written before any pre-resolution
    assert runtime.list_checkpoints(run.id)[-1]["capability_source_versions"] == []
    runtime.resume_run(run.id)
    assert runtime.run_until_blocked(run.id).state == RunState.PAUSED  # gate, no new checkpoint
    assert len(runtime.list_checkpoints(run.id)) == 1
    source.payload = b"v2"
    gate = runtime.list_pending_gates(run.id)[0]
    with pytest.raises(RunBlocked, match="capability source changed"):
        runtime.approve_gate(run.id, gate["id"])


# --- AC6-R3 -------------------------------------------------------------------------------


def test_r3_echoed_recorded_object_is_not_a_rederivation() -> None:
    recorded = PayloadSource()._provenance()

    class Echo:
        def check(self, item: ResolvedCapabilityProvenance) -> SourceCheckResult:
            return SourceCheckResult(SourceCheckStatus.MATCH, current=item)

        def load(self, capability: str) -> CapabilityContract:
            raise NotImplementedError

    assert check_capability_source(Echo(), recorded).status == SourceCheckStatus.UNAVAILABLE
    assert check_capability_source(PayloadSource(), recorded).status == SourceCheckStatus.MATCH


# --- AC6-R4 -------------------------------------------------------------------------------


def test_r4_pre_resolution_precedes_budget_reservation_and_resolution() -> None:
    runtime = _runtime(PayloadSource())
    run = runtime.create_run(Goal("order", ("done",)))
    runtime.execute_next(run.id)
    kinds = [e.type for e in runtime.list_events(run.id)]
    pre = kinds.index(EventType.CAPABILITY_PRE_RESOLVED)
    reserved = kinds.index(EventType.BUDGET_RESERVED)
    resolved = kinds.index(EventType.CAPABILITY_RESOLVED)
    assert pre < reserved < resolved


# --- AC6-R5 -------------------------------------------------------------------------------


def test_r5_schema_v1_stream_from_origin_main_resumes_under_schema_v2() -> None:
    fixture = json.loads((FIXTURES / "schema_v1_paused_run.json").read_text(encoding="utf-8"))
    store = SQLiteEventStore(":memory:")
    for raw in fixture["events"]:
        store.append(Event(**{**raw, "type": EventType(raw["type"])}))
    source = PayloadSource()
    runtime = _runtime(source, store=store, planner=Planner())
    run_id = fixture["run_id"]
    assert runtime.get_run(run_id).state == RunState.PAUSED
    assert "capability_source_versions" not in runtime.list_checkpoints(run_id)[-1]
    assert runtime.resume_run(run_id).state == RunState.READY
    assert source.checks == 0  # schema-1 runs recorded no capability provenance
    assert runtime.run_until_blocked(run_id).state == RunState.COMPLETED
    versions = [e.schema_version for e in runtime.list_events(run_id)]
    assert versions[: len(fixture["events"])] == ["1"] * len(fixture["events"])
    assert set(versions[len(fixture["events"]) :]) == {"2"}


# --- AC6-R2 + AC6-R7 (delegated reconciliation) -------------------------------------------

def _ambiguous_delegated() -> tuple[ControlPlane, str, str]:
    runtime = _delegated_runtime(_ambiguous)
    run = runtime.create_run(Goal("delegate", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.PAUSED
    return runtime, run.id, runtime.list_actions(run.id)[0]["intent"]["id"]


@pytest.mark.parametrize(("occurred", "observed"), [(False, True), (True, False)])
def test_r2_operator_claim_contradicting_evidence_blocks(occurred: bool, observed: bool) -> None:
    runtime, run_id, intent_id = _ambiguous_delegated()
    runtime.external_observer = _observer({"exists": observed})
    with pytest.raises(RunBlocked, match="contradicts"):
        runtime.reconcile_action(run_id, intent_id, occurred=occurred)
    assert runtime.list_actions(run_id)[0]["receipt"] is None
    assert runtime.get_run(run_id).node_status["write"] == NodeStatus.BLOCKED


def test_r2_confirmed_absent_effect_takes_the_not_occurred_path() -> None:
    runtime, run_id, intent_id = _ambiguous_delegated()
    runtime.external_observer = _observer({"exists": False})
    reconciled = runtime.reconcile_action(run_id, intent_id, occurred=False)
    assert reconciled.node_status["write"] == NodeStatus.PENDING
    receipt = runtime.list_actions(run_id)[0]["receipt"]
    assert receipt["actual_effects"] == []
    assert receipt["verification"]["evidence"]["effect_observed"] is False


def test_r7_reference_only_observation_is_blocked_before_the_verifier() -> None:
    subjects: list[Any] = []

    def reference_only(_: dict) -> ExternalObservation:
        return ExternalObservation(ref="crm:42", observer="dispatcher", observed_at="t")

    runtime = _delegated_runtime(
        lambda _: {"verified": True},
        observer=CallbackExternalStateObserver(reference_only),
        seen_subjects=subjects,
    )
    run = runtime.create_run(Goal("ref", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.PAUSED
    assert subjects == []
    receipts = [a["receipt"] for a in runtime.list_actions(run.id) if a["receipt"]]
    assert all(r["verification"]["status"] != "pass" for r in receipts)
