from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .models import Run


class ExternalResourceVersionProvider(Protocol):
    def snapshot(self, run: Run) -> tuple[str, ...]: ...

    def validate(self, run: Run, versions: tuple[str, ...]) -> tuple[str, ...]: ...


@dataclass(slots=True)
class CallbackResourceVersionProvider:
    snapshot_handler: Callable[[Run], tuple[str, ...]]
    validate_handler: Callable[[Run, tuple[str, ...]], tuple[str, ...]]

    def snapshot(self, run: Run) -> tuple[str, ...]:
        return self.snapshot_handler(run)

    def validate(self, run: Run, versions: tuple[str, ...]) -> tuple[str, ...]:
        return self.validate_handler(run, versions)
