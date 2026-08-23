from __future__ import annotations

from pathlib import Path

from agent_control_plane import (
    CapabilityDescriptor,
    CapabilityRegistry,
    ControlPlane,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    RunState,
    SQLiteEventStore,
    WorkingMemory,
)
from agent_control_plane.models import MemoryItem
from agent_control_plane.projections import now_iso


class Planner:
    model_calls_per_plan = 0

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
                    contributes_to=goal.success_criteria,
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def capabilities() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor("work", "1", "test"),
            lambda _: True,
        )
    )
    return registry


def test_pause_close_reopen_replay_and_resume(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    memory = WorkingMemory()
    memory.add(
        MemoryItem(
            id="fact-1",
            content={"fact": "persist me"},
            source="test",
            created_at=now_iso(),
            relevance=1.0,
        )
    )
    first_store = SQLiteEventStore(database)
    first = ControlPlane(
        store=first_store,
        planner=Planner(),
        capabilities=capabilities(),
        working_memory=memory,
    )
    run = first.create_run(Goal("recover", ("done",)))
    first.plan_run(run.id)
    paused = first.pause_run(run.id)
    checkpoint = first.list_checkpoints(run.id)[-1]
    assert paused.state == RunState.PAUSED
    assert checkpoint["working_memory_snapshot_ref"]
    first_store.close()

    restored_memory = WorkingMemory()
    second_store = SQLiteEventStore(database)
    second = ControlPlane(
        store=second_store,
        planner=Planner(),
        capabilities=capabilities(),
        working_memory=restored_memory,
    )
    replayed = second.get_run(run.id)
    assert replayed.state == RunState.PAUSED
    resumed = second.resume_run(run.id)
    assert resumed.state == RunState.READY
    restored = restored_memory.snapshot()
    assert restored[0]["id"] == "fact-1"
    assert restored[0]["content"] == {"fact": "persist me"}
    second_store.close()
