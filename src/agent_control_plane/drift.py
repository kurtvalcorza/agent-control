from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum


class DriftKind(StrEnum):
    SUCCESS_RATE = "success_rate"
    COST = "cost"
    LATENCY = "latency"
    CAPABILITY = "capability"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class DriftSignal:
    kind: DriftKind
    key: str
    expected: float
    observed: float
    delta: float
    threshold: float


class RollingDriftDetector:
    """Small operational hook for run/capability metrics.

    It intentionally reports signals only; changing policy or routing remains a reviewed action.
    """

    def __init__(self, *, window: int = 50) -> None:
        if window < 2:
            raise ValueError("window must be at least 2")
        self.window = window
        self._series: dict[tuple[DriftKind, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=window)
        )

    def observe(self, kind: DriftKind, key: str, value: float) -> None:
        self._series[(kind, key)].append(value)

    def detect(
        self,
        kind: DriftKind,
        key: str,
        *,
        expected: float,
        threshold: float,
        minimum_samples: int = 10,
    ) -> DriftSignal | None:
        values = self._series.get((kind, key))
        if values is None or len(values) < minimum_samples:
            return None
        observed = sum(values) / len(values)
        delta = observed - expected
        if abs(delta) < threshold:
            return None
        return DriftSignal(kind, key, expected, observed, delta, threshold)
