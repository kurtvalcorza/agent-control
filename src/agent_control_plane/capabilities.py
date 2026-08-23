from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .models import BudgetState, CapabilityDescriptor, CapabilityResult, SideEffectClass


class CapabilityError(RuntimeError):
    pass


class CapabilityNotFound(CapabilityError):
    pass


class CapabilityBudgetExceeded(CapabilityError):
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


class CapabilityRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, list[Capability]] = {}

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
    ) -> Capability:
        providers = self._providers.get(name, [])
        if not providers:
            raise CapabilityNotFound(name)
        eligible = [
            item
            for item in providers
            if (side_effect_class is None or item.descriptor.side_effect_class == side_effect_class)
            and budget.can_spend(
                estimated_cost_usd=item.descriptor.estimated_cost_usd,
                estimated_elapsed_ms=item.descriptor.estimated_elapsed_ms,
                model_calls=item.descriptor.estimated_model_calls,
                tool_calls=item.descriptor.estimated_tool_calls,
            )
        ]
        if not eligible:
            raise CapabilityBudgetExceeded(name)
        return min(eligible, key=lambda item: item.descriptor.estimated_cost_usd)
