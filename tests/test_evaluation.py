from __future__ import annotations

from agent_control_plane.evaluation import cluster_failures, evaluate_events, regression_case
from agent_control_plane.events import Event, EventType


def event(
    sequence: int,
    kind: EventType,
    payload: dict[str, object],
) -> Event:
    return Event(
        id=f"evt_{sequence}",
        run_id="run_evaluation",
        type=kind,
        occurred_at=f"2026-08-23T00:00:0{sequence}Z",
        sequence=sequence,
        payload=payload,
    )


def test_evaluation_uses_persisted_observation_class_taxonomy() -> None:
    events = [
        event(
            1,
            EventType.RUN_CREATED,
            {
                "goal": {"objective": "finish"},
                "risk_level": "low",
            },
        ),
        event(
            2,
            EventType.OBSERVATION_RECORDED,
            {"class": "verification_failure", "node_id": "verify"},
        ),
        event(
            3,
            EventType.OBSERVATION_RECORDED,
            {"class": "policy_block", "node_id": "write"},
        ),
        event(
            4,
            EventType.OBSERVATION_RECORDED,
            {"class": "budget_pressure", "node_id": "route"},
        ),
        event(5, EventType.RUN_FAILED, {"reason": "blocked"}),
    ]

    evaluation = evaluate_events(events)

    assert evaluation.verification_failures == 1
    assert evaluation.policy_blocks == 1
    assert evaluation.failure_classes == (
        "verification_failure",
        "policy_block",
        "budget_pressure",
    )
    assert cluster_failures([evaluation]) == {
        "budget_pressure": 1,
        "policy_block": 1,
        "verification_failure": 1,
    }

    case = regression_case(events)
    assert case is not None
    assert case["failure_classes"] == [
        "verification_failure",
        "policy_block",
        "budget_pressure",
    ]
