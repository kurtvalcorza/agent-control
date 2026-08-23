from __future__ import annotations

import pytest

from agent_control_plane import (
    CapabilityDescriptor,
    CapabilityRegistry,
    ControlPlane,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    RunBlocked,
    SQLiteEventStore,
    SideEffectClass,
)
from agent_control_plane.events import EventType
from agent_control_plane.models import CapabilityResult
from agent_control_plane.policy import PolicyEngine


class Planner:
    def __init__(self, *, side_effect: SideEffectClass = SideEffectClass.NONE) -> None:
        self.side_effect = side_effect

    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        return Plan(
            f"plan_{version}",
            run_id,
            version,
            (
                PlanNode(
                    "node",
                    "work",
                    ("work",),
                    side_effect_class=self.side_effect,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def test_policy_budget_ceiling_blocks_projected_run_usage() -> None:
    policy = PolicyEngine.from_document(
        {
            "version": "2026-08-23",
            "policies": [
                {
                    "id": "tight-budget",
                    "when": {},
                    "require": {"budget": {"max_tool_calls": 0}},
                },
                {"id": "default", "decision": "allow", "when": {}},
            ],
        }
    )
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor("work", "1", "test", estimated_tool_calls=1),
            lambda _: True,
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(),
        capabilities=registry,
        policy=policy,
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    runtime.plan_run(run.id)
    with pytest.raises(RunBlocked, match="policy budget ceiling"):
        runtime.execute_next(run.id)
    policy_events = [
        event
        for event in runtime.list_events(run.id)
        if event.type == EventType.POLICY_EVALUATED
    ]
    assert policy_events[-1].payload["policy_version"] == "2026-08-23"


def test_success_receipt_uses_actual_action_started_timestamp() -> None:
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "test",
                side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                reversible=True,
            ),
            lambda _: CapabilityResult(
                output={"verified": True},
                metadata={"effects": ["record-created"], "rollback_ref": "record:1"},
            ),
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(side_effect=SideEffectClass.REVERSIBLE_WRITE),
        capabilities=registry,
    )
    run = runtime.create_run(Goal("write", ("done",)))
    runtime.run_until_blocked(run.id)
    started = next(
        event
        for event in runtime.list_events(run.id)
        if event.type == EventType.ACTION_STARTED
    )
    receipt = runtime.list_actions(run.id)[0]["receipt"]
    assert receipt["started_at"] == started.occurred_at
    assert receipt["completed_at"] >= receipt["started_at"]


def test_legacy_policy_side_effect_aliases_parse_without_eager_failure() -> None:
    policy = PolicyEngine.from_document(
        {
            "policies": [
                {
                    "id": "legacy-read",
                    "when": {"side_effect_class": "read"},
                    "decision": "allow",
                },
                {"id": "default", "decision": "allow", "when": {}},
            ]
        }
    )
    assert policy.rules[0].side_effect_classes == (SideEffectClass.READ_ONLY,)
