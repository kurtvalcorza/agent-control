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
    RunState,
    SQLiteEventStore,
    SideEffectClass,
)


class Planner:
    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    "write",
                    "perform write",
                    ("write",),
                    side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def test_started_write_without_receipt_blocks_resume() -> None:
    registry = CapabilityRegistry()

    def ambiguous_write(_):
        raise RuntimeError("connection lost after submission")

    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                "write",
                "1",
                "test",
                side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                reversible=True,
            ),
            ambiguous_write,
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(),
        capabilities=registry,
    )
    run = runtime.create_run(Goal("mutate safely", ("done",)))
    paused = runtime.run_until_blocked(run.id)
    assert paused.state == RunState.PAUSED
    assert runtime.list_actions(run.id)[0]["receipt"] is None
    with pytest.raises(RunBlocked, match="started side effect"):
        runtime.resume_run(run.id)
