from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    RUN_CREATED = "RunCreated"
    GOAL_NORMALIZED = "GoalNormalized"
    PLANNING_STARTED = "PlanningStarted"
    STATE_CHANGED = "StateChanged"
    PLAN_CREATED = "PlanCreated"
    PLAN_PROPOSED = "PlanProposed"
    PLAN_ACCEPTED = "PlanAccepted"
    PLAN_REJECTED = "PlanRejected"
    PLAN_REVISION_REQUESTED = "PlanRevisionRequested"
    PLAN_REVISED = "PlanRevised"
    NODE_STATUS_CHANGED = "NodeStatusChanged"
    NODE_READY = "NodeReady"
    NODE_STARTED = "NodeStarted"
    NODE_EXECUTION_FINISHED = "NodeExecutionFinished"
    NODE_COMPLETED = "NodeCompleted"
    NODE_INVALIDATED = "NodeInvalidated"
    CAPABILITY_RESOLVED = "CapabilityResolved"
    CAPABILITY_INVOKED = "CapabilityInvoked"
    OBSERVATION_RECORDED = "ObservationRecorded"
    VERIFICATION_RECORDED = "VerificationRecorded"
    VERIFICATION_STARTED = "VerificationStarted"
    VERIFICATION_FINISHED = "VerificationFinished"
    RETRY_RECORDED = "RetryRecorded"
    POLICY_EVALUATED = "PolicyEvaluated"
    BUDGET_RESERVED = "BudgetReserved"
    BUDGET_CONSUMED = "BudgetConsumed"
    ACTION_INTENT_CREATED = "ActionIntentCreated"
    ACTION_AUTHORIZED = "ActionAuthorized"
    ACTION_REJECTED = "ActionRejected"
    ACTION_STARTED = "ActionStarted"
    ACTION_RECEIPT_RECORDED = "ActionReceiptRecorded"
    HUMAN_GATE_OPENED = "HumanGateOpened"
    HUMAN_GATE_RESOLVED = "HumanGateResolved"
    COMPENSATION_ATTEMPTED = "CompensationAttempted"
    COMPENSATION_SUCCEEDED = "CompensationSucceeded"
    COMPENSATION_FAILED = "CompensationFailed"
    BUDGET_UPDATED = "BudgetUpdated"
    CHECKPOINT_CREATED = "CheckpointCreated"
    PAUSE_REQUESTED = "PauseRequested"
    RUN_PAUSED = "RunPaused"
    RESUME_REQUESTED = "ResumeRequested"
    RUN_RESUMED = "RunResumed"
    CANCEL_REQUESTED = "CancelRequested"
    MEMORY_RECORDED = "MemoryRecorded"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"
    RUN_CANCELLED = "RunCancelled"


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    run_id: str
    type: EventType
    occurred_at: str
    sequence: int
    payload: dict[str, Any]
    schema_version: str = "1"
    actor: str = "runtime"
    causation_id: str | None = None
    correlation_id: str | None = None
