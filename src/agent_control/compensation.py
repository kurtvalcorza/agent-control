from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CompensationResult:
    success: bool
    detail: str
    metadata: dict[str, Any] | None = None


class Compensator(Protocol):
    def compensate(
        self,
        *,
        run_id: str,
        action: dict[str, Any],
    ) -> CompensationResult: ...


@dataclass(slots=True)
class InProcessCompensator:
    handler: Callable[[str, dict[str, Any]], CompensationResult]

    def compensate(
        self,
        *,
        run_id: str,
        action: dict[str, Any],
    ) -> CompensationResult:
        return self.handler(run_id, action)
