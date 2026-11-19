from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import ObservationClass


class ControlAction(StrEnum):
    CONTINUE = "continue"
    RETRY = "retry"
    REPLAN = "replan"
    PAUSE = "pause"
    FAIL = "fail"


_DEFAULT_ACTIONS: tuple[tuple[ObservationClass, ControlAction], ...] = (
    (ObservationClass.EXPECTED, ControlAction.CONTINUE),
    (ObservationClass.TRANSIENT_FAILURE, ControlAction.RETRY),
    (ObservationClass.CAPABILITY_FAILURE, ControlAction.FAIL),
    (ObservationClass.ASSUMPTION_INVALIDATED, ControlAction.REPLAN),
    (ObservationClass.INPUT_CHANGED, ControlAction.REPLAN),
    (ObservationClass.BUDGET_PRESSURE, ControlAction.REPLAN),
    (ObservationClass.POLICY_BLOCK, ControlAction.PAUSE),
    (ObservationClass.VERIFICATION_FAILURE, ControlAction.FAIL),
    (ObservationClass.HUMAN_REJECTED, ControlAction.REPLAN),
    (ObservationClass.GOAL_UNREACHABLE, ControlAction.FAIL),
    (ObservationClass.UNKNOWN_FAILURE, ControlAction.PAUSE),
)


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    mappings: tuple[tuple[ObservationClass, ControlAction], ...] = _DEFAULT_ACTIONS

    def __post_init__(self) -> None:
        classes = [classification for classification, _ in self.mappings]
        if len(classes) != len(set(classes)):
            raise ValueError("observation policy contains duplicate classifications")
        missing = set(ObservationClass) - set(classes)
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"observation policy is missing classifications: {names}")

    def action_for(self, classification: ObservationClass) -> ControlAction:
        return dict(self.mappings)[classification]

    def with_overrides(
        self,
        overrides: dict[ObservationClass, ControlAction],
    ) -> ObservationPolicy:
        values = dict(self.mappings)
        values.update(overrides)
        return ObservationPolicy(tuple(sorted(values.items(), key=lambda item: item[0].value)))

    def to_dict(self) -> dict[str, str]:
        return {classification.value: action.value for classification, action in self.mappings}


DEFAULT_OBSERVATION_POLICY = ObservationPolicy()
