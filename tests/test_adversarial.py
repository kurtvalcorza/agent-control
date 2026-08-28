from __future__ import annotations

import pytest

from agent_control_plane import (
    BudgetLimit,
    CapabilityDescriptor,
    CapabilityRegistry,
    CheckpointInvalid,
    ControlPlane,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    RunBlocked,
    RunState,
    SideEffectClass,
    SQLiteEventStore,
    VerificationKind,
    VerificationSpec,
)
from agent_control_plane.capabilities import CapabilityNotFound, TransientCapabilityError
from agent_control_plane.events import Event, EventType
from agent_control_plane.plans import InvalidPlan
from agent_control_plane.policy import PolicyEngine
from agent_control_plane.projections import now_iso, validate_event_stream


class StaticPlanner:
    def __init__(self, factory) -> None:
        self.factory = factory

    def create_plan(self, *, run_id, goal, version):
        return self.factory(run_id, goal, version)

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.factory(run.id, run.goal, version)


def _default_handler(_):
    return True


def registry(
    *,
    side_effect: SideEffectClass = SideEffectClass.NONE,
    reversible: bool = False,
    idempotent: bool = False,
    handler=_default_handler,
) -> CapabilityRegistry:
    result = CapabilityRegistry()
    result.register(
        InProcessCapability(
            CapabilityDescriptor(
                "work",
                "1",
                "test",
                side_effect_class=side_effect,
                reversible=reversible,
                idempotent=idempotent,
            ),
            handler,
        )
    )
    return result


def single_node_plan(
    run_id: str,
    goal: Goal,
    version: int,
    *,
    capability: str = "work",
    side_effect: SideEffectClass = SideEffectClass.NONE,
    verification: VerificationSpec | None = None,
) -> Plan:
    return Plan(
        f"plan_{version}",
        run_id,
        version,
        (
            PlanNode(
                "node",
                "work",
                (capability,),
                verification=verification or VerificationSpec(VerificationKind.DETERMINISTIC),
                side_effect_class=side_effect,
                contributes_to=(goal.success_criteria[0],),
            ),
        ),
    )


def test_cyclic_plan_is_rejected() -> None:
    def factory(run_id, goal, version):
        return Plan(
            f"plan_{version}",
            run_id,
            version,
            (
                PlanNode("a", "a", ("work",), dependencies=("b",)),
                PlanNode(
                    "b",
                    "b",
                    ("work",),
                    dependencies=("a",),
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(factory),
        capabilities=registry(),
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    with pytest.raises(InvalidPlan, match="acyclic"):
        runtime.plan_run(run.id)


def test_unavailable_capability_is_rejected() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(
            lambda run_id, goal, version: single_node_plan(
                run_id, goal, version, capability="missing"
            )
        ),
        capabilities=registry(),
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    with pytest.raises(CapabilityNotFound):
        runtime.plan_run(run.id)


def test_planner_cannot_understate_destructive_side_effect() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(single_node_plan),
        capabilities=registry(side_effect=SideEffectClass.DESTRUCTIVE),
    )
    run = runtime.create_run(Goal("finish", ("done",)))
    with pytest.raises(ValueError, match="side-effect class"):
        runtime.plan_run(run.id)


def test_idempotent_transient_write_reuses_intent_without_duplicate_intent() -> None:
    attempts = 0

    def handler(_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientCapabilityError("temporary")
        return True

    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(
            lambda run_id, goal, version: single_node_plan(
                run_id,
                goal,
                version,
                side_effect=SideEffectClass.REVERSIBLE_WRITE,
            )
        ),
        capabilities=registry(
            side_effect=SideEffectClass.REVERSIBLE_WRITE,
            reversible=True,
            idempotent=True,
            handler=handler,
        ),
    )
    run = runtime.create_run(Goal("write", ("done",)))
    completed = runtime.run_until_blocked(run.id)
    assert completed.state == RunState.COMPLETED
    assert attempts == 2
    intents = [
        event
        for event in runtime.list_events(run.id)
        if event.type == EventType.ACTION_INTENT_CREATED
    ]
    assert len(intents) == 1


def test_missing_verifier_blocks_instead_of_self_certifying() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(
            lambda run_id, goal, version: single_node_plan(
                run_id,
                goal,
                version,
                verification=VerificationSpec(VerificationKind.EVIDENCE),
            )
        ),
        capabilities=registry(),
    )
    run = runtime.create_run(Goal("verify", ("done",)))
    paused = runtime.run_until_blocked(run.id)
    assert paused.state == RunState.PAUSED


def test_corrupted_latest_checkpoint_fails_closed() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(single_node_plan),
        capabilities=registry(),
    )
    run = runtime.create_run(Goal("recover", ("done",)))
    runtime.plan_run(run.id)
    runtime.pause_run(run.id)
    previous = runtime.list_checkpoints(run.id)[-1]
    corrupted = dict(previous)
    corrupted["id"] = "checkpoint_corrupt"
    corrupted["projected_state_digest"] = "not-the-real-digest"
    runtime._append(run.id, EventType.CHECKPOINT_CREATED, {"checkpoint": corrupted})
    with pytest.raises(CheckpointInvalid, match="digest"):
        runtime.resume_run(run.id)


def test_event_stream_gap_and_unknown_schema_fail_closed() -> None:
    event1 = Event(
        "evt_a",
        "run_a",
        EventType.RUN_CREATED,
        now_iso(),
        1,
        {},
    )
    event3 = Event(
        "evt_b",
        "run_a",
        EventType.GOAL_NORMALIZED,
        now_iso(),
        3,
        {},
    )
    with pytest.raises(ValueError, match="gap"):
        validate_event_stream([event1, event3])
    unsupported = Event(
        "evt_c",
        "run_a",
        EventType.RUN_CREATED,
        now_iso(),
        1,
        {},
        schema_version="999",
    )
    with pytest.raises(ValueError, match="unsupported event schema"):
        validate_event_stream([unsupported])


def test_replan_loop_is_bounded_by_run_budget() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(single_node_plan),
        capabilities=registry(),
    )
    run = runtime.create_run(
        Goal("finish", ("done",)),
        budget_limit=BudgetLimit(max_replans=1),
    )
    runtime.plan_run(run.id)
    runtime.request_replan(run.id, "first")
    failed = runtime.request_replan(run.id, "second")
    assert failed.state == RunState.FAILED
    assert "replan limit" in (failed.outcome or "")


def test_authorization_from_superseded_plan_is_rejected() -> None:
    runtime = ControlPlane(
        store=SQLiteEventStore(":memory:"),
        planner=StaticPlanner(
            lambda run_id, goal, version: single_node_plan(
                run_id,
                goal,
                version,
                side_effect=SideEffectClass.DESTRUCTIVE,
            )
        ),
        capabilities=registry(
            side_effect=SideEffectClass.DESTRUCTIVE,
            reversible=True,
        ),
    )
    run = runtime.create_run(Goal("mutate", ("done",)))
    runtime.run_until_blocked(run.id)
    gate = runtime.list_pending_gates(run.id)[0]
    runtime.request_replan(run.id, "supersede approval request")
    with pytest.raises(RunBlocked, match="stale authorization"):
        runtime.approve_gate(run.id, gate["id"])


def test_unknown_policy_operator_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown policy operators"):
        PolicyEngine.from_document(
            {
                "policies": [
                    {"id": "bad", "when": {"magic": True}},
                    {"id": "default", "decision": "allow", "when": {}},
                ]
            }
        )


def test_capability_health_metadata_is_inspectable() -> None:
    capabilities = registry()
    health = capabilities.health("work")
    assert health[0]["status"] == "ok"
    assert health[0]["provider_id"] == "test"
