from __future__ import annotations

from agent_control_plane import (
    CapabilityDescriptor,
    CapabilityRegistry,
    CompensationResult,
    ControlPlane,
    Goal,
    InProcessCapability,
    InProcessCompensator,
    Plan,
    PlanNode,
    RunState,
    SQLiteEventStore,
    SideEffectClass,
)
from agent_control_plane.models import CapabilityResult


class Planner:
    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    "write",
                    "write state",
                    ("write",),
                    side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


class CancelPlanner(Planner):
    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    "write",
                    "write state",
                    ("write",),
                    side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                ),
                PlanNode(
                    "finish",
                    "finish later",
                    ("read",),
                    dependencies=("write",),
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )


def test_pause_resume_checkpoint_round_trip() -> None:
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                "write",
                "1",
                "test",
                side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                reversible=False,
            ),
            lambda _: True,
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"), planner=Planner(), capabilities=registry
    )
    run = runtime.create_run(Goal("recover", ("done",)))
    runtime.plan_run(run.id)
    paused = runtime.pause_run(run.id)
    assert paused.state == RunState.PAUSED
    assert runtime.list_checkpoints(run.id)
    resumed = runtime.resume_run(run.id)
    assert resumed.state == RunState.READY


def test_cancel_compensates_verified_reversible_effect() -> None:
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                "write",
                "1",
                "test",
                side_effect_class=SideEffectClass.REVERSIBLE_WRITE,
                reversible=True,
            ),
            lambda _: CapabilityResult(
                output={"verified": True},
                metadata={"effects": ["created record"], "rollback_ref": "record:1"},
            ),
        )
    )
    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                "read",
                "1",
                "test",
                side_effect_class=SideEffectClass.NONE,
            ),
            lambda _: True,
        )
    )
    compensated = []
    compensator = InProcessCompensator(
        lambda run_id, action: (
            compensated.append((run_id, action))
            or CompensationResult(True, "rolled back")
        )
    )
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=CancelPlanner(),
        capabilities=registry,
        compensator=compensator,
    )
    run = runtime.create_run(Goal("write", ("done",)))
    runtime.plan_run(run.id)
    after_write = runtime.execute_next(run.id)
    assert after_write.state == RunState.READY
    assert runtime.list_actions(run.id)[0]["receipt"] is not None
    cancelled = runtime.cancel_run(run.id)
    assert cancelled.state == RunState.CANCELLED
    assert len(compensated) == 1
