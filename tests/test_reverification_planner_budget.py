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
    RunState,
    SQLiteEventStore,
    VerificationKind,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
)


class EvidencePlanner:
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
                    verification=VerificationSpec(VerificationKind.EVIDENCE),
                    contributes_to=goal.success_criteria,
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


class CountingPlanner:
    def __init__(self) -> None:
        self.create_calls = 0
        self.revise_calls = 0

    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        self.create_calls += 1
        return Plan(
            f"plan_{version}",
            run_id,
            version,
            (
                PlanNode(
                    "work",
                    "work",
                    ("work",),
                    contributes_to=goal.success_criteria,
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        self.revise_calls += 1
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def registry(handler) -> CapabilityRegistry:
    result = CapabilityRegistry()
    result.register(
        InProcessCapability(
            CapabilityDescriptor("work", "1", "test"),
            handler,
        )
    )
    return result


def test_blocked_output_can_be_reverified_without_reinvocation() -> None:
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return {"answer": 42}

    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=EvidencePlanner(),
        capabilities=registry(handler),
    )
    run = control.create_run(Goal("verify", ("done",)))
    assert control.run_until_blocked(run.id).state == RunState.PAUSED
    assert calls == 1

    def verifier(spec, output, context) -> VerificationResult:
        del spec, context
        assert output == {"answer": 42}
        return VerificationResult(VerificationStatus.PASS, "evidence verified")

    control.verifiers.register(VerificationKind.EVIDENCE, verifier)
    assert control.reverify_node(run.id, "work").state == RunState.COMPLETED
    assert calls == 1


def test_planner_model_calls_are_bounded_by_run_budget() -> None:
    planner = CountingPlanner()
    control = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=planner,
        capabilities=registry(lambda _: True),
    )
    run = control.create_run(
        Goal("bounded planning", ("done",)),
        budget_limit=BudgetLimit(max_model_calls=1),
    )
    control.plan_run(run.id)
    projected = control.get_run(run.id)
    assert projected.budget.model_calls == 1
    failed = control.request_replan(run.id, "try another plan")
    assert failed.state == RunState.FAILED
    assert planner.revise_calls == 0
