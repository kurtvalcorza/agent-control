from __future__ import annotations

import pytest

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
    RunBlocked,
    RunState,
    SideEffectClass,
    SQLiteEventStore,
    VerificationKind,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
    VerifierRegistry,
)
from agent_control_plane.models import NodeStatus
from agent_control_plane.plans import InvalidPlan
from agent_control_plane.policy import PolicyEngine


class OneNodePlanner:
    def __init__(
        self,
        *,
        verification: VerificationSpec | None = None,
        side_effect: SideEffectClass = SideEffectClass.NONE,
    ) -> None:
        self.verification = verification or VerificationSpec(VerificationKind.DETERMINISTIC)
        self.side_effect = side_effect

    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        return Plan(
            f"plan_{version}",
            run_id,
            version,
            (
                PlanNode(
                    "work",
                    "work",
                    ("work",),
                    verification=self.verification,
                    side_effect_class=self.side_effect,
                    expected_outputs=goal.requested_outputs,
                    contributes_to=goal.success_criteria,
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def registry(
    *,
    descriptor: CapabilityDescriptor | None = None,
    handler=lambda _: True,
) -> CapabilityRegistry:
    result = CapabilityRegistry()
    result.register(
        InProcessCapability(
            descriptor or CapabilityDescriptor("work", "1", "test"),
            handler,
        )
    )
    return result


def test_requested_outputs_are_covered_by_expected_outputs() -> None:
    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=OneNodePlanner(),
        capabilities=registry(),
    )
    run = control.create_run(
        Goal("produce report", ("verified",), requested_outputs=("report.pdf",))
    )
    assert control.run_until_blocked(run.id).state == RunState.COMPLETED


def test_noncanonical_node_id_is_rejected() -> None:
    class BadPlanner(OneNodePlanner):
        def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
            return Plan(
                f"plan_{version}",
                run_id,
                version,
                (
                    PlanNode(
                        "bad id",
                        "work",
                        ("work",),
                        contributes_to=goal.success_criteria,
                    ),
                ),
            )

    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=BadPlanner(),
        capabilities=registry(),
    )
    run = control.create_run(Goal("finish", ("done",)))
    with pytest.raises(InvalidPlan, match="not canonical"):
        control.plan_run(run.id)


def test_budget_fit_is_checked_at_execution_not_plan_acceptance() -> None:
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return True

    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=OneNodePlanner(),
        capabilities=registry(
            descriptor=CapabilityDescriptor(
                "work",
                "1",
                "costly",
                estimated_cost_usd=2.0,
            ),
            handler=handler,
        ),
    )
    run = control.create_run(
        Goal("bounded", ("done",)),
        budget_limit=BudgetLimit(max_cost_usd=1.0),
    )
    assert control.plan_run(run.id).state == RunState.READY
    with pytest.raises(RunBlocked, match="budget pressure"):
        control.execute_next(run.id)
    assert calls == 0
    assert control.list_observations(run.id)[-1]["class"] == "budget_pressure"


def test_authorization_drift_after_planning_blocks_execution() -> None:
    capabilities = CapabilityRegistry(
        granted_permissions=frozenset({"write:data"}),
        max_risk=RiskLevel.HIGH,
    )
    capabilities.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "writer",
                permissions=("write:data",),
                risk_class=RiskLevel.HIGH,
            ),
            lambda _: True,
        )
    )
    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=OneNodePlanner(),
        capabilities=capabilities,
    )
    run = control.create_run(Goal("finish", ("done",)))
    control.plan_run(run.id)
    capabilities.set_authorization_context(
        granted_permissions=frozenset(),
        max_risk=RiskLevel.HIGH,
    )
    with pytest.raises(RunBlocked, match="authorization envelope"):
        control.execute_next(run.id)
    assert control.list_observations(run.id)[-1]["class"] == "policy_block"


def pass_verifier(spec, output, context) -> VerificationResult:
    del spec, output, context
    return VerificationResult(VerificationStatus.PASS, "verified")


def verification_policy() -> PolicyEngine:
    return PolicyEngine.from_document(
        {
            "policies": [
                {
                    "id": "independent-verifiers",
                    "when": {},
                    "require": {"verification": ["evidence", "challenge"]},
                },
                {"id": "default", "decision": "allow", "when": {}},
            ]
        }
    )


def test_policy_required_verifiers_are_all_enforced() -> None:
    verifiers = VerifierRegistry()
    verifiers.register(VerificationKind.EVIDENCE, pass_verifier)
    verifiers.register(VerificationKind.CHALLENGE, pass_verifier)
    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=OneNodePlanner(
            verification=VerificationSpec(
                VerificationKind.EVIDENCE,
                additional_kinds=(VerificationKind.CHALLENGE,),
            )
        ),
        capabilities=registry(),
        policy=verification_policy(),
        verifiers=verifiers,
    )
    run = control.create_run(Goal("verify", ("done",)))
    assert control.run_until_blocked(run.id).state == RunState.COMPLETED


def test_missing_one_required_verifier_blocks_completion() -> None:
    verifiers = VerifierRegistry()
    verifiers.register(VerificationKind.EVIDENCE, pass_verifier)
    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=OneNodePlanner(
            verification=VerificationSpec(
                VerificationKind.EVIDENCE,
                additional_kinds=(VerificationKind.CHALLENGE,),
            )
        ),
        capabilities=registry(),
        policy=verification_policy(),
        verifiers=verifiers,
    )
    run = control.create_run(Goal("verify", ("done",)))
    assert control.run_until_blocked(run.id).state == RunState.PAUSED


def test_executed_side_effect_gets_receipt_when_verification_blocks() -> None:
    descriptor = CapabilityDescriptor(
        "work",
        "1",
        "writer",
        side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
        reversible=True,
    )
    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=OneNodePlanner(
            verification=VerificationSpec(VerificationKind.EVIDENCE),
            side_effect=SideEffectClass.REVERSIBLE_WRITE,
        ),
        capabilities=registry(descriptor=descriptor),
    )
    run = control.create_run(Goal("write", ("done",)))
    assert control.run_until_blocked(run.id).state == RunState.PAUSED
    receipt = control.list_actions(run.id)[0]["receipt"]
    assert receipt is not None
    assert receipt["verification"]["status"] == "blocked"


class DependencyChangingPlanner:
    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        return Plan(
            f"plan_{version}",
            run_id,
            version,
            (
                PlanNode("first", "v1", ("work",)),
                PlanNode("second", "second", ("work",), dependencies=("first",)),
                PlanNode(
                    "third",
                    "third",
                    ("work",),
                    dependencies=("second",),
                    contributes_to=goal.success_criteria,
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return Plan(
            f"plan_{version}",
            run.id,
            version,
            (
                PlanNode("first", "v2 changed", ("work",)),
                PlanNode("second", "second", ("work",), dependencies=("first",)),
                PlanNode(
                    "third",
                    "third",
                    ("work",),
                    dependencies=("second",),
                    contributes_to=run.goal.success_criteria,
                ),
            ),
        )


def test_changed_upstream_node_invalidates_completed_descendants() -> None:
    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=DependencyChangingPlanner(),
        capabilities=registry(),
    )
    run = control.create_run(Goal("finish", ("done",)))
    control.plan_run(run.id)
    control.execute_next(run.id)
    after_second = control.execute_next(run.id)
    assert after_second.node_status["first"] == NodeStatus.COMPLETED
    assert after_second.node_status["second"] == NodeStatus.COMPLETED
    revised = control.request_replan(run.id, "upstream contract changed")
    assert revised.node_status["first"] == NodeStatus.PENDING
    assert revised.node_status["second"] == NodeStatus.PENDING
    assert revised.node_status["third"] == NodeStatus.PENDING


def test_policy_default_allow_cannot_hide_requirements() -> None:
    with pytest.raises(ValueError, match="default allow"):
        PolicyEngine.from_document(
            {
                "policies": [
                    {
                        "id": "default",
                        "decision": "allow",
                        "when": {},
                        "require": {"justification": True},
                    }
                ]
            }
        )


def test_negative_resource_limits_fail_closed() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        BudgetLimit(max_cost_usd=-1.0)
    with pytest.raises(ValueError, match="non-negative"):
        PolicyEngine.from_document(
            {
                "policies": [
                    {
                        "id": "bad",
                        "when": {},
                        "require": {"budget": {"max_cost_usd": -1}},
                    },
                    {"id": "default", "decision": "allow", "when": {}},
                ]
            }
        )
