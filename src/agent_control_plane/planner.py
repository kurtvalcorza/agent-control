from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import Goal, Plan, Run


class Planner(Protocol):
    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan: ...

    def revise_plan(self, *, run: Run, reason: str, version: int) -> Plan: ...


class StaticPlanner:
    """Deterministic planner for tests and hosts that build their own Plan objects."""

    def __init__(self, plan_factory: Callable[[str, Goal, int], Plan]) -> None:
        self._plan_factory = plan_factory

    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        return self._plan_factory(run_id, goal, version)

    def revise_plan(self, *, run: Run, reason: str, version: int) -> Plan:
        del reason
        return self._plan_factory(run.id, run.goal, version)
