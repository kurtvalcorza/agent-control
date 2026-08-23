from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureCategory(StrEnum):
    PLANNING_ERROR = "planning_error"
    INVALID_PLAN = "invalid_plan"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    CAPABILITY_TIMEOUT = "capability_timeout"
    CAPABILITY_FAILURE = "capability_failure"
    POLICY_DENIED = "policy_denied"
    AUTHORIZATION_REQUIRED = "authorization_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    VERIFICATION_FAILED = "verification_failed"
    CHECKPOINT_INVALID = "checkpoint_invalid"
    EXTERNAL_STATE_DIVERGED = "external_state_diverged"
    NON_IDEMPOTENT_RETRY_RISK = "non_idempotent_retry_risk"
    COMPENSATION_FAILED = "compensation_failed"
    GOAL_UNREACHABLE = "goal_unreachable"
    INTERNAL_INVARIANT_VIOLATION = "internal_invariant_violation"


@dataclass(frozen=True, slots=True)
class FailureRecord:
    category: FailureCategory
    reason: str
    node_id: str | None = None
    action_id: str | None = None
    causal_event_ids: tuple[str, ...] = ()
    retryable: bool = False
    replanning_allowed: bool = False
    human_recoverable: bool = False


class ControlPlaneError(RuntimeError):
    category = FailureCategory.INTERNAL_INVARIANT_VIOLATION


class CheckpointInvalid(ControlPlaneError):
    category = FailureCategory.CHECKPOINT_INVALID
