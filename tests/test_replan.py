from __future__ import annotations

from agent_control_plane import (
    CapabilityDescriptor,
    CapabilityRegistry,
    ControlPlane,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    SQLiteEventStore,
)
from agent_control_plane.models import NodeStatus


class ChangingPlanner:
    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode("first", "first v1", ("work",)),
                PlanNode(
                    "second",
                    "second",
                    ("work",),
                    dependencies=("first",),
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return Plan(
            id=f"plan_{version}",
            run_id=run.id,
            version=version,
            nodes=(
                PlanNode("first", "first v2 changed", ("work",)),
                PlanNode(
                    "second",
                    "second",
                    ("work",),
                    dependencies=("first",),
                    contributes_to=(run.goal.success_criteria[0],),
                ),
            ),
        )


def test_replan_does_not_preserve_structurally_changed_completed_node() -> None:
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(CapabilityDescriptor("work", "1", "test"), lambda _: True)
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=ChangingPlanner(),
        capabilities=registry,
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    runtime.plan_run(run.id)
    after_first = runtime.execute_next(run.id)
    assert after_first.node_status["first"] == NodeStatus.COMPLETED
    revised = runtime.request_replan(run.id, "planner changed first node")
    assert revised.plan_version == 2
    assert revised.node_status["first"] == NodeStatus.PENDING
    assert revised.node_status["second"] == NodeStatus.PENDING
