from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import RiskLevel, SideEffectClass


class PolicyDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN = "require_human"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: PolicyDecisionType
    reason: str
    policy_id: str
    policy_version: str = "1"
    required_verification: tuple[str, ...] = ()
    justification_required: bool = False
    reversible_required: bool = False
    max_estimated_cost_usd: float | None = None
    budget_ceiling: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyRule:
    id: str
    decision: PolicyDecisionType
    reason: str
    side_effect_classes: tuple[SideEffectClass, ...] = ()
    risk_levels: tuple[RiskLevel, ...] = ()
    capability_risk_levels: tuple[RiskLevel, ...] = ()
    reversible: bool | None = None
    estimated_cost_usd_gt: float | None = None
    permission_any: tuple[str, ...] = ()
    required_verification: tuple[str, ...] = ()
    justification_required: bool = False
    reversible_required: bool = False
    max_estimated_cost_usd: float | None = None
    budget_ceiling: tuple[tuple[str, float], ...] = ()

    def matches(
        self,
        *,
        side_effect_class: SideEffectClass,
        run_risk: RiskLevel,
        capability_risk: RiskLevel,
        reversible: bool,
        estimated_cost_usd: float,
        permissions: tuple[str, ...],
    ) -> bool:
        if self.side_effect_classes and side_effect_class not in self.side_effect_classes:
            return False
        if self.risk_levels and run_risk not in self.risk_levels:
            return False
        if self.capability_risk_levels and capability_risk not in self.capability_risk_levels:
            return False
        if self.reversible is not None and reversible != self.reversible:
            return False
        if self.estimated_cost_usd_gt is not None:
            if estimated_cost_usd <= self.estimated_cost_usd_gt:
                return False
        if self.permission_any and not set(self.permission_any).intersection(permissions):
            return False
        return True


class PolicyEngine:
    """Ordered, declarative, versioned, fail-closed action policy."""

    _BUDGET_KEYS = frozenset(
        {
            "max_cost_usd",
            "max_elapsed_ms",
            "max_model_calls",
            "max_tool_calls",
        }
    )
    _RULE_KEYS = frozenset({"id", "decision", "deny", "reason", "when", "match", "require"})

    def __init__(
        self,
        rules: tuple[PolicyRule, ...] | None = None,
        *,
        version: str = "1",
    ) -> None:
        if not version.strip():
            raise ValueError("policy version must not be empty")
        self.rules = rules or self.default_rules()
        self.version = version

    @staticmethod
    def default_rules() -> tuple[PolicyRule, ...]:
        return (
            PolicyRule(
                id="high-impact-side-effect",
                decision=PolicyDecisionType.REQUIRE_HUMAN,
                reason="high-impact side effects require explicit approval",
                side_effect_classes=(
                    SideEffectClass.DESTRUCTIVE,
                    SideEffectClass.EXTERNAL_COMMUNICATION,
                    SideEffectClass.FINANCIAL,
                    SideEffectClass.PRIVILEGED,
                ),
            ),
            PolicyRule(
                id="high-risk-write",
                decision=PolicyDecisionType.REQUIRE_HUMAN,
                reason="high-risk writes require explicit approval",
                side_effect_classes=(SideEffectClass.REVERSIBLE_WRITE,),
                risk_levels=(RiskLevel.HIGH, RiskLevel.CRITICAL),
            ),
            PolicyRule(
                id="irreversible-write",
                decision=PolicyDecisionType.REQUIRE_HUMAN,
                reason="irreversible writes require explicit approval",
                side_effect_classes=(SideEffectClass.REVERSIBLE_WRITE,),
                reversible=False,
            ),
            PolicyRule(
                id="default-allow",
                decision=PolicyDecisionType.ALLOW,
                reason="action permitted",
            ),
        )

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> PolicyEngine:
        unknown_document = set(document) - {"version", "policies", "rules"}
        if unknown_document:
            raise ValueError(f"unknown policy document keys: {sorted(unknown_document)}")
        raw_rules = document.get("policies", document.get("rules"))
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("policy document must contain a non-empty policies/rules list")
        rules: list[PolicyRule] = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                raise ValueError("policy rule must be an object")
            try:
                unknown_rule = set(raw) - cls._RULE_KEYS
                if unknown_rule:
                    raise ValueError(f"unknown policy rule keys: {sorted(unknown_rule)}")
                rule_id = str(raw["id"])
                if not rule_id.strip():
                    raise ValueError("policy rule id must not be empty")
                match = raw.get("when", raw.get("match", {}))
                require = raw.get("require", {})
                if not isinstance(match, dict) or not isinstance(require, dict):
                    raise ValueError("when/match and require must be objects")
                allowed_match = {
                    "side_effect_class",
                    "run_risk",
                    "capability_risk",
                    "reversible",
                    "estimated_cost_usd_gt",
                    "permission_any",
                }
                allowed_require = {
                    "human_approval",
                    "verification",
                    "justification",
                    "reversible",
                    "max_estimated_cost_usd",
                    "budget",
                }
                unknown = (set(match) - allowed_match) | (set(require) - allowed_require)
                if unknown:
                    raise ValueError(f"unknown policy operators: {sorted(unknown)}")
                budget = require.get("budget", {})
                if not isinstance(budget, dict):
                    raise ValueError("policy budget requirement must be an object")
                unknown_budget = set(budget) - cls._BUDGET_KEYS
                if unknown_budget:
                    raise ValueError(
                        f"unknown policy budget operators: {sorted(unknown_budget)}"
                    )
                budget_values = {
                    str(key): float(value) for key, value in budget.items()
                }
                if any(value < 0 for value in budget_values.values()):
                    raise ValueError("policy budget ceilings must be non-negative")

                threshold = (
                    float(match["estimated_cost_usd_gt"])
                    if "estimated_cost_usd_gt" in match
                    else None
                )
                if threshold is not None and threshold < 0:
                    raise ValueError("estimated_cost_usd_gt must be non-negative")
                max_estimated = (
                    float(require["max_estimated_cost_usd"])
                    if "max_estimated_cost_usd" in require
                    else None
                )
                if max_estimated is not None and max_estimated < 0:
                    raise ValueError("max_estimated_cost_usd must be non-negative")

                decision_raw = raw.get("decision")
                if decision_raw is None:
                    if require.get("human_approval") is True:
                        decision_raw = PolicyDecisionType.REQUIRE_HUMAN.value
                    elif raw.get("deny") is True:
                        decision_raw = PolicyDecisionType.DENY.value
                    else:
                        decision_raw = PolicyDecisionType.ALLOW.value
                side_values = cls._as_list(match.get("side_effect_class", []))
                risk_values = cls._as_list(match.get("run_risk", []))
                capability_risks = cls._as_list(match.get("capability_risk", []))
                permission_any = cls._as_list(match.get("permission_any", []))
                rule = PolicyRule(
                    id=rule_id,
                    decision=PolicyDecisionType(str(decision_raw)),
                    reason=str(raw.get("reason", rule_id)),
                    side_effect_classes=tuple(
                        cls._parse_side_effect(str(value)) for value in side_values
                    ),
                    risk_levels=tuple(RiskLevel(str(value)) for value in risk_values),
                    capability_risk_levels=tuple(
                        RiskLevel(str(value)) for value in capability_risks
                    ),
                    reversible=(
                        bool(match["reversible"]) if "reversible" in match else None
                    ),
                    estimated_cost_usd_gt=threshold,
                    permission_any=tuple(str(value) for value in permission_any),
                    required_verification=tuple(
                        str(value) for value in cls._as_list(require.get("verification", []))
                    ),
                    justification_required=bool(require.get("justification", False)),
                    reversible_required=bool(require.get("reversible", False)),
                    max_estimated_cost_usd=max_estimated,
                    budget_ceiling=tuple(sorted(budget_values.items())),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid policy rule: {raw!r}") from exc
            rules.append(rule)
        if not cls._is_unconditional_allow(rules[-1]):
            raise ValueError("policy document must end with an explicit default allow rule")
        return cls(tuple(rules), version=str(document.get("version", "1")))

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _parse_side_effect(value: str) -> SideEffectClass:
        legacy = {
            "read": SideEffectClass.READ_ONLY,
            "write": SideEffectClass.REVERSIBLE_WRITE,
        }
        if value in legacy:
            return legacy[value]
        return SideEffectClass(value)

    @staticmethod
    def _is_unconditional_allow(rule: PolicyRule) -> bool:
        return (
            rule.decision == PolicyDecisionType.ALLOW
            and not rule.side_effect_classes
            and not rule.risk_levels
            and not rule.capability_risk_levels
            and rule.reversible is None
            and rule.estimated_cost_usd_gt is None
            and not rule.permission_any
            and not rule.required_verification
            and not rule.justification_required
            and not rule.reversible_required
            and rule.max_estimated_cost_usd is None
            and not rule.budget_ceiling
        )

    def evaluate_action(
        self,
        *,
        side_effect_class: SideEffectClass,
        run_risk: RiskLevel,
        capability_risk: RiskLevel = RiskLevel.LOW,
        reversible: bool,
        estimated_cost_usd: float = 0.0,
        permissions: tuple[str, ...] = (),
    ) -> PolicyDecision:
        for rule in self.rules:
            if rule.matches(
                side_effect_class=side_effect_class,
                run_risk=run_risk,
                capability_risk=capability_risk,
                reversible=reversible,
                estimated_cost_usd=estimated_cost_usd,
                permissions=permissions,
            ):
                return PolicyDecision(
                    decision=rule.decision,
                    reason=rule.reason,
                    policy_id=rule.id,
                    policy_version=self.version,
                    required_verification=rule.required_verification,
                    justification_required=rule.justification_required,
                    reversible_required=rule.reversible_required,
                    max_estimated_cost_usd=rule.max_estimated_cost_usd,
                    budget_ceiling=rule.budget_ceiling,
                )
        raise RuntimeError("policy evaluation reached no terminal rule")
