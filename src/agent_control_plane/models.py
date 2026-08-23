from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RunState(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INVALIDATED = "invalidated"


class SideEffectClass(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    DESTRUCTIVE = "destructive"
    EXTERNAL_COMMUNICATION = "external_communication"
    FINANCIAL = "financial"
    PRIVILEGED = "privileged"

    READ = "read_only"
    WRITE = "reversible_write"


def is_side_effecting(value: SideEffectClass | str) -> bool:
    """Return whether a capability can create externally visible state."""
    effect = SideEffectClass(value)
    return effect not in {SideEffectClass.NONE, SideEffectClass.READ_ONLY}


class AuthorizationState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class VerificationKind(StrEnum):
    DETERMINISTIC = "deterministic"
    EVIDENCE = "evidence"
    MODEL_JUDGE = "model_judge"
    EXTERNAL_STATE = "external_state"
    HUMAN = "human"
    CHALLENGE = "challenge"


class VerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"
    RETRY = "retry"
    REPLAN = "replan"

    HUMAN_GATE = "blocked"


class ObservationClass(StrEnum):
    EXPECTED = "expected"
    TRANSIENT_FAILURE = "transient_failure"
    CAPABILITY_FAILURE = "capability_failure"
    ASSUMPTION_INVALIDATED = "assumption_invalidated"
    INPUT_CHANGED = "input_changed"
    BUDGET_PRESSURE = "budget_pressure"
    POLICY_BLOCK = "policy_block"
    VERIFICATION_FAILURE = "verification_failure"
    HUMAN_REJECTED = "human_rejected"
    GOAL_UNREACHABLE = "goal_unreachable"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass(frozen=True, slots=True)
class Goal:
    objective: str
    success_criteria: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    requested_outputs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("goal objective must not be empty")
        if not self.success_criteria:
            raise ValueError("at least one success criterion is required")


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    kind: VerificationKind
    criteria: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True, slots=True)
class PlanNode:
    id: str
    objective: str
    required_capabilities: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    verification: VerificationSpec = field(
        default_factory=lambda: VerificationSpec(VerificationKind.DETERMINISTIC)
    )
    side_effect_class: SideEffectClass = SideEffectClass.NONE
    expected_outputs: tuple[str, ...] = ()
    contributes_to: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    justification: str | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    run_id: str
    version: int
    nodes: tuple[PlanNode, ...]


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    max_cost_usd: float | None = None
    max_elapsed_ms: int | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_replans: int | None = None
    max_retries_per_node: int = 2


@dataclass(slots=True)
class BudgetState:
    limit: BudgetLimit
    spent_cost_usd: float = 0.0
    elapsed_ms: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    replans: int = 0

    def can_spend(
        self,
        *,
        estimated_cost_usd: float = 0.0,
        estimated_elapsed_ms: int = 0,
        model_calls: int = 0,
        tool_calls: int = 0,
    ) -> bool:
        if (
            self.limit.max_cost_usd is not None
            and self.spent_cost_usd + estimated_cost_usd > self.limit.max_cost_usd
        ):
            return False
        if (
            self.limit.max_elapsed_ms is not None
            and self.elapsed_ms + estimated_elapsed_ms > self.limit.max_elapsed_ms
        ):
            return False
        if (
            self.limit.max_model_calls is not None
            and self.model_calls + model_calls > self.limit.max_model_calls
        ):
            return False
        if (
            self.limit.max_tool_calls is not None
            and self.tool_calls + tool_calls > self.limit.max_tool_calls
        ):
            return False
        return True


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    name: str
    version: str
    provider_id: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    side_effect_class: SideEffectClass = SideEffectClass.NONE
    reversible: bool = False
    permissions: tuple[str, ...] = ()
    estimated_cost_usd: float = 0.0
    estimated_elapsed_ms: int = 0
    estimated_model_calls: int = 0
    estimated_tool_calls: int = 1
    idempotent: bool = False
    risk_class: RiskLevel = RiskLevel.LOW


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    output: Any
    actual_cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionIntent:
    id: str
    run_id: str
    node_id: str
    plan_version: int
    capability: str
    arguments_digest: str
    expected_effect: str
    side_effect_class: SideEffectClass
    authorization_state: AuthorizationState
    reversible: bool


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    id: str
    intent_id: str
    actual_effects: tuple[str, ...]
    verification: VerificationResult
    rollback_ref: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    content: Any
    source: str
    created_at: str
    relevance: float = 0.0
    confidence: float = 1.0
    expires_at: str | None = None
    supersedes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Checkpoint:
    id: str
    run_id: str
    event_sequence: int
    plan_version: int
    projected_state_digest: str
    working_memory_snapshot_ref: str | None
    budget_state: dict[str, Any]
    unresolved_action_intents: tuple[str, ...]
    external_resource_versions: tuple[str, ...] = ()
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class HumanGate:
    id: str
    reason: str
    requested_action: str | None = None
    node_id: str | None = None
    status: str = "pending"
    decided_by: str | None = None
    decided_at: str | None = None


@dataclass(slots=True)
class Run:
    id: str
    goal: Goal
    risk_level: RiskLevel
    state: RunState
    plan_version: int
    budget: BudgetState
    policy_profile: str
    created_at: str
    updated_at: str
    outcome: str | None = None
    plan: Plan | None = None
    node_status: dict[str, NodeStatus] = field(default_factory=dict)
    node_outputs: dict[str, Any] = field(default_factory=dict)
    retry_counts: dict[str, int] = field(default_factory=dict)
    pending_action_intent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
