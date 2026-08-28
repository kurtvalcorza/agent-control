from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .events import SUPPORTED_EVENT_SCHEMA_VERSIONS, Event, EventType
from .models import (
    BudgetLimit,
    BudgetState,
    Goal,
    NodeStatus,
    Plan,
    PlanNode,
    RiskLevel,
    Run,
    RunState,
    SideEffectClass,
    VerificationKind,
    VerificationSpec,
)


def _goal(payload: dict[str, Any]) -> Goal:
    return Goal(
        objective=payload["objective"],
        success_criteria=tuple(payload["success_criteria"]),
        constraints=tuple(payload.get("constraints", [])),
        requested_outputs=tuple(payload.get("requested_outputs", [])),
        assumptions=tuple(payload.get("assumptions", [])),
    )


def _plan(payload: dict[str, Any]) -> Plan:
    nodes = tuple(
        PlanNode(
            id=node["id"],
            objective=node["objective"],
            required_capabilities=tuple(node["required_capabilities"]),
            dependencies=tuple(node.get("dependencies", [])),
            verification=VerificationSpec(
                kind=VerificationKind(node["verification"]["kind"]),
                criteria=tuple(node["verification"].get("criteria", [])),
                required=bool(node["verification"].get("required", True)),
                additional_kinds=tuple(
                    VerificationKind(value)
                    for value in node["verification"].get("additional_kinds", [])
                ),
            ),
            side_effect_class=SideEffectClass(node.get("side_effect_class", "none")),
            expected_outputs=tuple(node.get("expected_outputs", [])),
            contributes_to=tuple(node.get("contributes_to", [])),
            inputs=dict(node.get("inputs", {})),
            justification=node.get("justification"),
        )
        for node in payload["nodes"]
    )
    return Plan(payload["id"], payload["run_id"], int(payload["version"]), nodes)


def validate_event_stream(events: list[Event]) -> None:
    if not events:
        return
    run_id = events[0].run_id
    seen: set[str] = set()
    for expected_sequence, event in enumerate(events, start=1):
        if event.run_id != run_id:
            raise ValueError("event stream contains multiple run ids")
        if event.sequence != expected_sequence:
            raise ValueError(
                f"event stream gap/out-of-order event: expected {expected_sequence}, "
                f"got {event.sequence}"
            )
        if event.id in seen:
            raise ValueError(f"duplicate event id: {event.id}")
        seen.add(event.id)
        if event.schema_version not in SUPPORTED_EVENT_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported event schema version: {event.schema_version!r}")
        timestamp = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware UTC")
        if timestamp.utcoffset() != UTC.utcoffset(timestamp):
            raise ValueError("event timestamp must be normalized to UTC")


def project_run(events: list[Event]) -> Run | None:
    if not events:
        return None
    validate_event_stream(events)
    created = events[0]
    if created.type != EventType.RUN_CREATED:
        raise ValueError("first event must be RunCreated")
    payload = created.payload
    limit_payload = payload["budget_limit"]
    run = Run(
        id=created.run_id,
        goal=_goal(payload["goal"]),
        risk_level=RiskLevel(payload["risk_level"]),
        state=RunState.CREATED,
        plan_version=0,
        budget=BudgetState(
            BudgetLimit(
                max_cost_usd=limit_payload.get("max_cost_usd"),
                max_elapsed_ms=limit_payload.get("max_elapsed_ms"),
                max_model_calls=limit_payload.get("max_model_calls"),
                max_tool_calls=limit_payload.get("max_tool_calls"),
                max_replans=limit_payload.get("max_replans"),
                max_retries_per_node=limit_payload.get("max_retries_per_node", 2),
            )
        ),
        policy_profile=payload["policy_profile"],
        created_at=created.occurred_at,
        updated_at=created.occurred_at,
    )
    for event in events[1:]:
        run.updated_at = event.occurred_at
        payload = event.payload
        if event.type == EventType.STATE_CHANGED:
            run.state = RunState(payload["to"])
        elif event.type in {EventType.PLAN_CREATED, EventType.PLAN_REVISED}:
            run.plan = _plan(payload["plan"])
            run.plan_version = run.plan.version
            preserved = set(payload.get("preserved_completed", []))
            valid_ids = {node.id for node in run.plan.nodes}
            run.node_status = {
                node.id: NodeStatus.COMPLETED if node.id in preserved else NodeStatus.PENDING
                for node in run.plan.nodes
            }
            run.node_outputs = {
                node_id: output
                for node_id, output in run.node_outputs.items()
                if node_id in valid_ids and node_id in preserved
            }
            run.retry_counts = {
                node_id: count
                for node_id, count in run.retry_counts.items()
                if node_id in valid_ids and node_id in preserved
            }
            if event.type == EventType.PLAN_REVISED:
                run.budget.replans += 1
        elif event.type == EventType.NODE_STATUS_CHANGED:
            run.node_status[payload["node_id"]] = NodeStatus(payload["status"])
        elif event.type == EventType.CAPABILITY_INVOKED:
            run.node_outputs[payload["node_id"]] = payload.get("output")
        elif event.type == EventType.BUDGET_UPDATED:
            run.budget.spent_cost_usd = float(payload["spent_cost_usd"])
            run.budget.model_calls = int(payload["model_calls"])
            run.budget.tool_calls = int(payload["tool_calls"])
            run.budget.elapsed_ms = int(payload.get("elapsed_ms", 0))
        elif event.type == EventType.RETRY_RECORDED:
            node_id = payload["node_id"]
            run.retry_counts[node_id] = int(payload["count"])
        elif event.type == EventType.ACTION_INTENT_CREATED:
            intent = payload["intent"]
            if intent.get("authorization_state") == "pending":
                run.pending_action_intent_id = intent["id"]
        elif event.type in {EventType.ACTION_AUTHORIZED, EventType.ACTION_REJECTED}:
            if payload["intent_id"] == run.pending_action_intent_id:
                run.pending_action_intent_id = None
        elif event.type == EventType.ACTION_RECEIPT_RECORDED:
            if payload["receipt"]["intent_id"] == run.pending_action_intent_id:
                run.pending_action_intent_id = None
        elif event.type == EventType.RUN_COMPLETED:
            run.state = RunState.COMPLETED
            run.outcome = payload.get("outcome", "completed")
        elif event.type == EventType.RUN_FAILED:
            run.state = RunState.FAILED
            run.outcome = payload.get("reason", "failed")
        elif event.type == EventType.RUN_CANCELLED:
            run.state = RunState.CANCELLED
            run.outcome = payload.get("outcome", payload.get("reason", "cancelled"))
    return run


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
