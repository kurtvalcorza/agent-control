from __future__ import annotations

from dataclasses import dataclass

from .models import Goal, Plan


@dataclass(frozen=True, slots=True)
class GoalCoverage:
    covered_success_criteria: frozenset[str]
    covered_requested_outputs: frozenset[str]
    missing_success_criteria: frozenset[str]
    missing_requested_outputs: frozenset[str]

    @property
    def complete(self) -> bool:
        return not self.missing_success_criteria and not self.missing_requested_outputs


def evaluate_goal_coverage(goal: Goal, plan: Plan) -> GoalCoverage:
    contributions = {
        contribution
        for node in plan.nodes
        for contribution in node.contributes_to
    }
    produced_outputs = {
        output
        for node in plan.nodes
        for output in node.expected_outputs
    }
    criteria = frozenset(goal.success_criteria)
    outputs = frozenset(goal.requested_outputs)
    return GoalCoverage(
        covered_success_criteria=criteria.intersection(contributions),
        covered_requested_outputs=outputs.intersection(produced_outputs),
        missing_success_criteria=criteria.difference(contributions),
        missing_requested_outputs=outputs.difference(produced_outputs),
    )


def require_goal_coverage(goal: Goal, plan: Plan) -> None:
    coverage = evaluate_goal_coverage(goal, plan)
    if coverage.complete:
        return
    details: list[str] = []
    if coverage.missing_success_criteria:
        details.append(
            "missing success criteria: " + ", ".join(sorted(coverage.missing_success_criteria))
        )
    if coverage.missing_requested_outputs:
        details.append(
            "missing requested outputs: " + ", ".join(sorted(coverage.missing_requested_outputs))
        )
    raise ValueError("plan does not cover the full goal contract (" + "; ".join(details) + ")")
