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
    RiskLevel,
    RunBlocked,
    SQLiteEventStore,
)
from agent_control_plane.capabilities import CapabilityAuthorizationError
from agent_control_plane.policy import PolicyEngine


class CoveragePlanner:
    def __init__(
        self,
        contributions: tuple[str, ...],
        *,
        expected_outputs: tuple[str, ...] = (),
        justification: str | None = None,
    ) -> None:
        self.contributions = contributions
        self.expected_outputs = expected_outputs
        self.justification = justification

    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    id="work",
                    objective="work",
                    required_capabilities=("work",),
                    expected_outputs=self.expected_outputs,
                    contributes_to=self.contributions,
                    justification=self.justification,
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def registry(
    *,
    permissions: tuple[str, ...] = (),
    risk: RiskLevel = RiskLevel.LOW,
) -> CapabilityRegistry:
    result = CapabilityRegistry()
    result.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "test",
                permissions=permissions,
                risk_class=risk,
            ),
            lambda _: True,
        )
    )
    return result


def test_plan_must_cover_every_success_criterion() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=CoveragePlanner(("criterion-a",)),
        capabilities=registry(),
    )
    run = runtime.create_run(Goal("finish", ("criterion-a", "criterion-b")))
    with pytest.raises(ValueError, match="missing success criteria"):
        runtime.plan_run(run.id)


def test_plan_must_cover_requested_outputs() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=CoveragePlanner(("criterion",)),
        capabilities=registry(),
    )
    run = runtime.create_run(
        Goal("finish", ("criterion",), requested_outputs=("report.pdf",))
    )
    with pytest.raises(ValueError, match="missing requested outputs"):
        runtime.plan_run(run.id)


def test_plan_accepts_requested_outputs_from_expected_outputs() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=CoveragePlanner(
            ("criterion",),
            expected_outputs=("report.pdf",),
        ),
        capabilities=registry(),
    )
    run = runtime.create_run(
        Goal("finish", ("criterion",), requested_outputs=("report.pdf",))
    )
    planned = runtime.plan_run(run.id)
    assert planned.plan is not None


def test_capability_permissions_are_enforced_before_ready() -> None:
    capabilities = CapabilityRegistry(granted_permissions=frozenset({"read:data"}))
    capabilities.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "privileged",
                permissions=("write:repo",),
            ),
            lambda _: True,
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=CoveragePlanner(("done",)),
        capabilities=capabilities,
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    with pytest.raises(CapabilityAuthorizationError):
        runtime.plan_run(run.id)


def test_capability_risk_ceiling_is_enforced() -> None:
    capabilities = CapabilityRegistry(max_risk=RiskLevel.MEDIUM)
    capabilities.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "critical",
                risk_class=RiskLevel.CRITICAL,
            ),
            lambda _: True,
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=CoveragePlanner(("done",)),
        capabilities=capabilities,
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    with pytest.raises(CapabilityAuthorizationError):
        runtime.plan_run(run.id)


def test_policy_can_require_justification_for_expensive_step() -> None:
    policy = PolicyEngine.from_document(
        {
            "policies": [
                {
                    "id": "expensive",
                    "when": {"estimated_cost_usd_gt": 0.5},
                    "require": {"justification": True},
                },
                {"id": "default", "decision": "allow", "when": {}},
            ]
        }
    )
    capabilities = CapabilityRegistry()
    capabilities.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "costly",
                estimated_cost_usd=0.75,
            ),
            lambda _: True,
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=CoveragePlanner(("done",)),
        capabilities=capabilities,
        policy=policy,
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    runtime.plan_run(run.id)
    with pytest.raises(RunBlocked, match="justification"):
        runtime.execute_next(run.id)


def test_legacy_policy_side_effect_aliases_parse() -> None:
    engine = PolicyEngine.from_document(
        {
            "policies": [
                {
                    "id": "read",
                    "when": {"side_effect_class": "read"},
                    "decision": "allow",
                },
                {"id": "default", "decision": "allow", "when": {}},
            ]
        }
    )
    assert engine.rules[0].side_effect_classes
