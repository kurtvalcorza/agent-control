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
    required_verification: tuple[str, ...] = ()
    justification_required: bool = False


@dataclass(frozen=True, slots=True)
class PolicyRule:
    id: str
    decision: PolicyDecisionType
    reason: str
    side_effect_classes: tuple[SideEffectClass, ...] = ()
    risk_levels: tuple[RiskLevel, ...] = ()
    reversible: bool | None = None
    required_verification: tuple[str, ...] = ()
    justification_required: bool = False

    def matches(
        self,
        *,
        side_effect_class: SideEffectClass,
        run_risk: RiskLevel,
        reversible: bool,
    ) -> bool:
        if self.side_effect_classes and side_effect_class not in self.side_effect_classes:
            return False
        if self.risk_levels and run_risk not in self.risk_levels:
            return False
        if self.reversible is not None and reversible != self.reversible:
            return False
        return True


class PolicyEngine:
    """Ordered, declarative, fail-closed policy rules for action authorization."""

    def __init__(self, rules: tuple[PolicyRule, ...] | None = None) -> None:
        self.rules = rules or self.default_rules()

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
        raw_rules = document.get("policies", document.get("rules"))
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("policy document must contain a non-empty policies/rules list")
        rules: list[PolicyRule] = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                raise ValueError("policy rule must be an object")
            try:
                match = raw.get("when", raw.get("match", {}))
                require = raw.get("require", {})
                if not isinstance(match, dict) or not isinstance(require, dict):
                    raise ValueError("when/match and require must be objects")
                unknown_match = set(match) - {"side_effect_class", "run_risk", "reversible"}
                unknown_require = set(require) - {
                    "human_approval", "verification", "justification", "reversible"
                }
                if unknown_match or unknown_require:
                    raise ValueError(
                        f"unknown policy operators: {sorted(unknown_match | unknown_require)}"
                    )
                decision_raw = raw.get("decision")
                if decision_raw is None:
                    if require.get("human_approval") is True:
                        decision_raw = PolicyDecisionType.REQUIRE_HUMAN.value
                    elif raw.get("deny") is True:
                        decision_raw = PolicyDecisionType.DENY.value
                    else:
                        decision_raw = PolicyDecisionType.ALLOW.value
                side_values = match.get("side_effect_class", [])
                risk_values = match.get("run_risk", [])
                if isinstance(side_values, str):
                    side_values = [side_values]
                if isinstance(risk_values, str):
                    risk_values = [risk_values]
                reversible = match.get("reversible")
                if require.get("reversible") is True:
                    reversible = True
                rule = PolicyRule(
                    id=str(raw["id"]),
                    decision=PolicyDecisionType(str(decision_raw)),
                    reason=str(raw.get("reason", raw["id"])),
                    side_effect_classes=tuple(
                        cls._parse_side_effect(str(value)) for value in side_values
                    ),
                    risk_levels=tuple(RiskLevel(str(value)) for value in risk_values),
                    reversible=reversible,
                    required_verification=tuple(str(v) for v in require.get("verification", [])),
                    justification_required=bool(require.get("justification", False)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid policy rule: {raw!r}") from exc
            rules.append(rule)
        # A catch-all default allow is required so unmatched rules are explicit and auditable.
        terminal = rules[-1]
        if (
            terminal.decision != PolicyDecisionType.ALLOW
            or terminal.side_effect_classes
            or terminal.risk_levels
            or terminal.reversible is not None
        ):
            raise ValueError("policy document must end with an explicit default allow rule")
        return cls(tuple(rules))

    @staticmethod
    def _parse_side_effect(value: str) -> SideEffectClass:
        legacy = {
            "read": SideEffectClass.READ_ONLY,
            "write": SideEffectClass.REVERSIBLE_WRITE,
        }
        if value in legacy:
            return legacy[value]
        return SideEffectClass(value)

    def evaluate_action(
        self,
        *,
        side_effect_class: SideEffectClass,
        run_risk: RiskLevel,
        reversible: bool,
    ) -> PolicyDecision:
        for rule in self.rules:
            if rule.matches(
                side_effect_class=side_effect_class,
                run_risk=run_risk,
                reversible=reversible,
            ):
                return PolicyDecision(
                    decision=rule.decision,
                    reason=rule.reason,
                    policy_id=rule.id,
                    required_verification=rule.required_verification,
                    justification_required=rule.justification_required,
                )
        raise RuntimeError("policy evaluation reached no terminal rule")
