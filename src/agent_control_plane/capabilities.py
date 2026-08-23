from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .models import (
    BudgetState,
    CapabilityDescriptor,
    CapabilityResult,
    RiskLevel,
    SideEffectClass,
)


class CapabilityError(RuntimeError):
    pass


class CapabilityNotFound(CapabilityError):
    pass


class CapabilityBudgetExceeded(CapabilityError):
    pass


class CapabilityAuthorizationError(CapabilityError):
    pass


class CapabilityInvocationError(CapabilityError):
    pass


class TransientCapabilityError(CapabilityInvocationError):
    pass


class Capability(Protocol):
    descriptor: CapabilityDescriptor

    def invoke(self, arguments: dict[str, Any]) -> CapabilityResult: ...


@dataclass(slots=True)
class InProcessCapability:
    descriptor: CapabilityDescriptor
    handler: Callable[[dict[str, Any]], Any]

    def invoke(self, arguments: dict[str, Any]) -> CapabilityResult:
        result = self.handler(arguments)
        if isinstance(result, CapabilityResult):
            return result
        return CapabilityResult(output=result)


_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class CapabilityRegistry:
    def __init__(
        self,
        *,
        granted_permissions: frozenset[str] | None = None,
        max_risk: RiskLevel = RiskLevel.CRITICAL,
    ) -> None:
        self._providers: dict[str, list[Capability]] = {}
        self.granted_permissions = granted_permissions
        self.max_risk = max_risk

    def set_authorization_context(
        self,
        *,
        granted_permissions: frozenset[str] | None,
        max_risk: RiskLevel,
    ) -> None:
        self.granted_permissions = granted_permissions
        self.max_risk = max_risk

    def register(self, capability: Capability) -> None:
        providers = self._providers.setdefault(capability.descriptor.name, [])
        if any(
            item.descriptor.provider_id == capability.descriptor.provider_id
            and item.descriptor.version == capability.descriptor.version
            for item in providers
        ):
            raise CapabilityError("capability provider/version already registered")
        providers.append(capability)

    def unregister(self, name: str, provider_id: str) -> None:
        providers = self._providers.get(name, [])
        self._providers[name] = [
            item for item in providers if item.descriptor.provider_id != provider_id
        ]

    def list(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(
            item.descriptor
            for providers in self._providers.values()
            for item in providers
        )

    def descriptors(self, name: str) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item.descriptor for item in self._providers.get(name, []))

    def has(self, name: str) -> bool:
        return bool(self._providers.get(name))

    def resolve(
        self,
        name: str,
        budget: BudgetState,
        *,
        side_effect_class: SideEffectClass | None = None,
        granted_permissions: frozenset[str] | None = None,
        max_risk: RiskLevel | None = None,
    ) -> Capability:
        providers = self._providers.get(name, [])
        if not providers:
            raise CapabilityNotFound(name)
        class_matches = [
            item
            for item in providers
            if side_effect_class is None
            or item.descriptor.side_effect_class == side_effect_class
        ]
        if not class_matches:
            raise CapabilityAuthorizationError(
                f"no provider for {name!r} matches side-effect class"
            )
        effective_permissions = (
            granted_permissions
            if granted_permissions is not None
            else self.granted_permissions
        )
        effective_max_risk = max_risk if max_risk is not None else self.max_risk
        authorized = [
            item
            for item in class_matches
            if _RISK_ORDER[item.descriptor.risk_class] <= _RISK_ORDER[effective_max_risk]
            and (
                effective_permissions is None
                or set(item.descriptor.permissions).issubset(effective_permissions)
            )
        ]
        if not authorized:
            raise CapabilityAuthorizationError(
                f"no provider for {name!r} fits the permission/risk envelope"
            )
        eligible = [
            item
            for item in authorized
            if budget.can_spend(
                estimated_cost_usd=item.descriptor.estimated_cost_usd,
                estimated_elapsed_ms=item.descriptor.estimated_elapsed_ms,
                model_calls=item.descriptor.estimated_model_calls,
                tool_calls=item.descriptor.estimated_tool_calls,
            )
        ]
        if not eligible:
            raise CapabilityBudgetExceeded(name)
        return min(
            eligible,
            key=lambda item: (
                item.descriptor.estimated_cost_usd,
                item.descriptor.estimated_elapsed_ms,
                item.descriptor.provider_id,
            ),
        )
