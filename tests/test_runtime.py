from __future__ import annotations

from agent_control_plane import (
    BudgetLimit,
    CapabilityDescriptor,
    CapabilityRegistry,
    ControlPlane,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    RiskLevel,
    RunState,
    SQLiteEventStore,
    SideEffectClass,
)
from agent_control_plane.capabilities import TransientCapabilityError
from agent_control_plane.models import CapabilityResult


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
                    id="node",
                    objective="do work",
                    required_capabilities=("work",),
                    side_effect_class=self.side_effect,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def control(handler, *, side_effect=SideEffectClass.NONE, reversible=False, budget=None):
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                name="work",
                version="1",
                provider_id="test",
                side_effect_class=side_effect,
                reversible=reversible,
            ),
            handler,
        )
    )
    return ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(side_effect=side_effect),
        capabilities=registry,
    )


def test_read_only_run_completes_and_replays() -> None:
    runtime = control(lambda _: {"verified": True})
    run = runtime.create_run(Goal("finish", ("done",)))
    result = runtime.run_until_blocked(run.id)
    assert result.state == RunState.COMPLETED
    assert runtime.get_run(run.id).state == RunState.COMPLETED
    assert len(runtime.list_events(run.id)) > 5


def test_destructive_action_requires_approval_and_executes_once() -> None:
    calls = []

    def handler(_):
        calls.append(1)
        return CapabilityResult(
            output={"verified": True},
            metadata={"effects": ["changed"], "rollback_ref": "rb_1"},
        )

    runtime = control(handler, side_effect=SideEffectClass.DESTRUCTIVE, reversible=True)
    run = runtime.create_run(Goal("mutate", ("done",)), risk_level=RiskLevel.HIGH)
    blocked = runtime.run_until_blocked(run.id)
    assert blocked.state == RunState.PAUSED
    assert calls == []
    gate = runtime.list_pending_gates(run.id)[0]
    runtime.approve_gate(run.id, gate["id"])
    result = runtime.run_until_blocked(run.id)
    assert result.state == RunState.COMPLETED
    assert calls == [1]
    assert runtime.list_actions(run.id)[0]["receipt"] is not None


def test_transient_failure_retries_without_replan() -> None:
    attempts = 0

    def handler(_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientCapabilityError("temporary")
        return True

    runtime = control(handler)
    run = runtime.create_run(Goal("retry", ("done",)), budget_limit=BudgetLimit(max_replans=1))
    result = runtime.run_until_blocked(run.id)
    assert result.state == RunState.COMPLETED
    assert attempts == 2
    assert result.plan_version == 1


def test_budget_pressure_blocks_before_invocation() -> None:
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return True

    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor("work", "1", "test", estimated_cost_usd=2.0),
            handler,
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(),
        capabilities=registry,
    )
    run = runtime.create_run(Goal("bounded", ("done",)), budget_limit=BudgetLimit(max_cost_usd=1.0))
    result = runtime.run_until_blocked(run.id)
    assert result.state == RunState.READY
    assert calls == 0
    assert runtime.list_observations(run.id)[-1]["class"] == "budget_pressure"
