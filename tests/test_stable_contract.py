from __future__ import annotations

from agent_control_plane import (
    CapabilityDescriptor,
    CapabilityRegistry,
    ControlPlane,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    SideEffectClass,
    SQLiteEventStore,
)
from agent_control_plane.control_actions import ControlAction, ObservationPolicy
from agent_control_plane.events import EventType
from agent_control_plane.models import CapabilityResult, ObservationClass
from agent_control_plane.policy import PolicyEngine
from agent_control_plane.runtime import RunBlocked


class Planner:
    def __init__(self, *, side_effect: SideEffectClass = SideEffectClass.NONE) -> None:
        self.side_effect = side_effect

    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    id="work",
                    objective="perform work",
                    required_capabilities=("work",),
                    side_effect_class=self.side_effect,
                    contributes_to=goal.success_criteria,
                    expected_outputs=goal.requested_outputs,
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def runtime(
    *,
    descriptor: CapabilityDescriptor | None = None,
    handler=lambda _: True,
    policy: PolicyEngine | None = None,
) -> ControlPlane:
    descriptor = descriptor or CapabilityDescriptor("work", "1", "test")
    registry = CapabilityRegistry()
    registry.register(InProcessCapability(descriptor, handler))
    return ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(side_effect=descriptor.side_effect_class),
        capabilities=registry,
        policy=policy,
    )


def test_stable_inspection_exposes_control_story() -> None:
    control = runtime()
    run = control.create_run(Goal("finish", ("done",)))
    completed = control.run_until_blocked(run.id)
    inspection = control.inspect_run(completed.id)

    assert inspection["verified_nodes"] == ["work"]
    assert inspection["unverified_nodes"] == []
    assert inspection["provider_selections"][0]["provider_id"] == "test"
    assert "selection_reason" in inspection["provider_selections"][0]
    assert inspection["verification_results"]
    assert inspection["policy_decisions"][0]["policy_version"] == "1"
    assert "budget_remaining" in inspection
    assert inspection["control_contract"]["goal"]["objective"] == "finish"
    assert inspection["observation_policy"]["transient_failure"] == "retry"


def test_observation_policy_is_configurable_and_complete() -> None:
    control = runtime()
    policy = ObservationPolicy().with_overrides(
        {ObservationClass.CAPABILITY_FAILURE: ControlAction.REPLAN}
    )
    control.configure_observation_policy(policy)
    assert control.control_action_for(ObservationClass.CAPABILITY_FAILURE) == ControlAction.REPLAN


def test_policy_budget_ceiling_blocks_before_invocation() -> None:
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return True

    policy = PolicyEngine.from_document(
        {
            "version": "budget-v1",
            "policies": [
                {
                    "id": "tight-budget",
                    "when": {},
                    "require": {"budget": {"max_cost_usd": 0.25}},
                },
                {"id": "default", "decision": "allow", "when": {}},
            ],
        }
    )
    control = runtime(
        descriptor=CapabilityDescriptor(
            "work",
            "1",
            "costly",
            estimated_cost_usd=0.5,
        ),
        handler=handler,
        policy=policy,
    )
    run = control.create_run(Goal("bounded", ("done",)))
    control.plan_run(run.id)

    try:
        control.execute_next(run.id)
    except RunBlocked as exc:
        assert "policy budget ceiling" in str(exc)
    else:
        raise AssertionError("policy budget ceiling should block execution")

    assert calls == 0
    decision = [
        event
        for event in control.list_events(run.id)
        if event.type == EventType.POLICY_EVALUATED
    ][-1]
    assert decision.payload["policy_version"] == "budget-v1"


def test_receipt_uses_true_action_start_timestamp() -> None:
    descriptor = CapabilityDescriptor(
        "work",
        "1",
        "writer",
        side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
        reversible=True,
    )
    control = runtime(
        descriptor=descriptor,
        handler=lambda _: CapabilityResult(
            output={"verified": True},
            metadata={"effects": ["changed record"]},
        ),
    )
    run = control.create_run(Goal("write", ("done",)))
    completed = control.run_until_blocked(run.id)
    assert completed.outcome == "full goal contract satisfied"

    events = control.list_events(run.id)
    started = next(event for event in events if event.type == EventType.ACTION_STARTED)
    receipt = control.list_actions(run.id)[0]["receipt"]
    assert receipt["started_at"] == started.occurred_at
    assert receipt["completed_at"]
