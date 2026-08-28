from __future__ import annotations

import json

import pytest

from agent_control_plane import (
    CallbackResourceVersionProvider,
    CapabilityDescriptor,
    CapabilityRegistry,
    ControlPlane,
    Goal,
    InProcessCapability,
    MemoryItem,
    Plan,
    PlanNode,
    RunBlocked,
    RunState,
    SideEffectClass,
    SQLiteEventStore,
    WorkingMemory,
)
from agent_control_plane.events import EventType
from agent_control_plane.models import CapabilityResult


class Planner:
    def __init__(
        self,
        *,
        side_effect: SideEffectClass = SideEffectClass.NONE,
        inputs: dict[str, object] | None = None,
    ) -> None:
        self.side_effect = side_effect
        self.inputs = inputs or {}

    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    "work",
                    "work",
                    ("work",),
                    side_effect_class=self.side_effect,
                    inputs=dict(self.inputs),
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason: str, version: int) -> Plan:
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def runtime_for(
    handler,
    *,
    side_effect: SideEffectClass = SideEffectClass.NONE,
    reversible: bool = False,
    inputs: dict[str, object] | None = None,
    memory: WorkingMemory | None = None,
    resources=None,
) -> ControlPlane:
    registry = CapabilityRegistry()
    registry.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "test",
                side_effect_class=side_effect,
                reversible=reversible,
            ),
            handler,
        )
    )
    return ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=Planner(side_effect=side_effect, inputs=inputs),
        capabilities=registry,
        working_memory=memory,
        resource_versions=resources,
    )


def test_successful_step_reserves_then_consumes_budget() -> None:
    runtime = runtime_for(lambda _: CapabilityResult(True, actual_cost_usd=0.25))
    run = runtime.create_run(Goal("finish", ("done",)))
    result = runtime.run_until_blocked(run.id)
    assert result.state == RunState.COMPLETED
    assert runtime.list_budget_reservations(run.id) == []
    kinds = [event.type for event in runtime.list_events(run.id)]
    assert EventType.BUDGET_RESERVED in kinds
    assert EventType.BUDGET_CONSUMED in kinds


def test_gate_releases_reservation_before_human_approval() -> None:
    runtime = runtime_for(
        lambda _: True,
        side_effect=SideEffectClass.DESTRUCTIVE,
        reversible=True,
    )
    run = runtime.create_run(Goal("mutate", ("done",)))
    paused = runtime.run_until_blocked(run.id)
    assert paused.state == RunState.PAUSED
    assert runtime.list_budget_reservations(run.id) == []


def test_ambiguous_write_requires_action_and_budget_reconciliation() -> None:
    def crash_after_effect(_):
        raise RuntimeError("transport disconnected after write")

    runtime = runtime_for(
        crash_after_effect,
        side_effect=SideEffectClass.REVERSIBLE_WRITE,
        reversible=True,
    )
    run = runtime.create_run(Goal("write", ("done",)))
    paused = runtime.run_until_blocked(run.id)
    assert paused.state == RunState.PAUSED
    reservations = runtime.list_budget_reservations(run.id)
    assert len(reservations) == 1
    action = runtime.list_actions(run.id)[0]
    assert action["receipt"] is None
    with pytest.raises(RunBlocked, match="started side effect"):
        runtime.resume_run(run.id)

    reconciled = runtime.reconcile_action(
        run.id,
        action["intent"]["id"],
        occurred=True,
        actual_effects=("record-created",),
        rollback_ref="record:1",
        actual_cost_usd=0.10,
    )
    assert reconciled.state == RunState.PAUSED
    assert runtime.list_budget_reservations(run.id) == []
    receipt = runtime.list_actions(run.id)[0]["receipt"]
    assert receipt["started_at"]
    assert receipt["completed_at"]
    assert receipt["rollback_ref"] == "record:1"
    runtime.resume_run(run.id)
    completed = runtime.run_until_blocked(run.id)
    assert completed.state == RunState.COMPLETED


def test_checkpoint_restores_memory_and_blocks_changed_resources() -> None:
    memory = WorkingMemory()
    memory.add(MemoryItem("m1", {"fact": 1}, "test", "2026-08-23T00:00:00Z"))
    resources = CallbackResourceVersionProvider(
        snapshot_handler=lambda _: ("dataset@v1",),
        validate_handler=lambda _run, _versions: ("dataset",),
    )
    runtime = runtime_for(
        lambda _: True,
        memory=memory,
        resources=resources,
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    runtime.plan_run(run.id)
    paused = runtime.pause_run(run.id)
    assert paused.state == RunState.PAUSED
    checkpoint = runtime.list_checkpoints(run.id)[-1]
    assert checkpoint["working_memory_snapshot_ref"].startswith("memory:sha256:")
    assert checkpoint["external_resource_versions"] == ["dataset@v1"]
    with pytest.raises(RunBlocked, match="resource versions changed"):
        runtime.resume_run(run.id)
    assert runtime.list_observations(run.id)[-1]["class"] == "input_changed"


def test_inline_secret_is_rejected_but_opaque_reference_is_auditable() -> None:
    unsafe = runtime_for(lambda _: True, inputs={"api_key": "literal-secret"})
    run = unsafe.create_run(Goal("finish", ("done",)))
    with pytest.raises(ValueError, match="inline secret material"):
        unsafe.plan_run(run.id)
    serialized = json.dumps(unsafe.store.raw_events())
    assert "literal-secret" not in serialized

    safe = runtime_for(lambda _: True, inputs={"api_key": "secret://provider/key"})
    run2 = safe.create_run(Goal("finish", ("done",)))
    safe.plan_run(run2.id)
    serialized2 = json.dumps(safe.store.raw_events())
    assert "secret://provider/key" in serialized2


def test_exception_text_is_redacted_before_event_persistence() -> None:
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"

    def fail(_):
        raise RuntimeError(f"Authorization: Bearer {secret}")

    runtime = runtime_for(fail)
    run = runtime.create_run(Goal("finish", ("done",)))
    result = runtime.run_until_blocked(run.id)
    assert result.state == RunState.FAILED
    serialized = json.dumps(runtime.store.raw_events())
    assert secret not in serialized
    assert "<redacted>" in serialized
