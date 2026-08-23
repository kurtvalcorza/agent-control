from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .events import Event, EventType


@dataclass(frozen=True, slots=True)
class RunEvaluation:
    run_id: str
    completed: bool
    failed: bool
    tool_calls: int
    replans: int
    verification_failures: int
    policy_blocks: int
    failure_classes: tuple[str, ...]


def evaluate_events(events: list[Event]) -> RunEvaluation:
    if not events:
        raise ValueError("cannot evaluate an empty event stream")
    tool_calls = 0
    replans = 0
    verification_failures = 0
    policy_blocks = 0
    failure_classes: list[str] = []
    completed = False
    failed = False
    for event in events:
        if event.type == EventType.CAPABILITY_INVOKED:
            tool_calls += 1
        elif event.type == EventType.PLAN_REVISED:
            replans += 1
        elif event.type == EventType.OBSERVATION_RECORDED:
            classification = str(event.payload.get("classification", ""))
            if classification:
                failure_classes.append(classification)
            if classification == "verification_failure":
                verification_failures += 1
            if classification == "policy_block":
                policy_blocks += 1
        elif event.type == EventType.RUN_COMPLETED:
            completed = True
        elif event.type == EventType.RUN_FAILED:
            failed = True
    return RunEvaluation(
        run_id=events[0].run_id,
        completed=completed,
        failed=failed,
        tool_calls=tool_calls,
        replans=replans,
        verification_failures=verification_failures,
        policy_blocks=policy_blocks,
        failure_classes=tuple(failure_classes),
    )


def regression_case(events: list[Event]) -> dict[str, Any] | None:
    """Produce a portable candidate regression case from a failed run.

    This deliberately does not auto-modify skills or policies. Operational evidence becomes a
    reviewable artifact first.
    """
    evaluation = evaluate_events(events)
    if not evaluation.failed:
        return None
    created = events[0]
    return {
        "schema_version": 1,
        "source_run_id": evaluation.run_id,
        "goal": created.payload.get("goal"),
        "risk_level": created.payload.get("risk_level"),
        "failure_classes": list(evaluation.failure_classes),
        "expected": {"terminal_state": "not_failed"},
    }


def cluster_failures(evaluations: list[RunEvaluation]) -> dict[str, int]:
    clusters: dict[str, int] = {}
    for evaluation in evaluations:
        for classification in evaluation.failure_classes:
            clusters[classification] = clusters.get(classification, 0) + 1
    return dict(sorted(clusters.items(), key=lambda item: (-item[1], item[0])))
