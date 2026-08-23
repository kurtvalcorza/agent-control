from __future__ import annotations

import re
from collections import deque

from .models import Plan


class InvalidPlan(ValueError):
    pass


_CANONICAL_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def validate_plan(plan: Plan, success_criteria: tuple[str, ...]) -> None:
    if not plan.nodes:
        raise InvalidPlan("plan must contain at least one node")
    ids = [node.id for node in plan.nodes]
    if len(ids) != len(set(ids)):
        raise InvalidPlan("plan node ids must be unique")
    for node_id in ids:
        if not _CANONICAL_NODE_ID.fullmatch(node_id):
            raise InvalidPlan(
                f"plan node id {node_id!r} is not canonical; use 1-128 alphanumeric/._:- characters"
            )
    nodes = {node.id: node for node in plan.nodes}
    for node in plan.nodes:
        if not node.objective.strip():
            raise InvalidPlan(f"node {node.id!r} has an empty objective")
        for dep in node.dependencies:
            if dep not in nodes:
                raise InvalidPlan(f"node {node.id!r} depends on unknown node {dep!r}")
            if dep == node.id:
                raise InvalidPlan(f"node {node.id!r} cannot depend on itself")

    indegree = {node.id: len(node.dependencies) for node in plan.nodes}
    children: dict[str, list[str]] = {node.id: [] for node in plan.nodes}
    for node in plan.nodes:
        for dep in node.dependencies:
            children[dep].append(node.id)
    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(plan.nodes):
        raise InvalidPlan("plan must be acyclic")

    terminal = [node for node in plan.nodes if not children[node.id]]
    if not terminal:
        raise InvalidPlan("plan must contain a terminal node")
    criteria = set(success_criteria)
    if criteria and not any(criteria.intersection(node.contributes_to) for node in terminal):
        raise InvalidPlan("at least one terminal node must contribute to a success criterion")


def ready_node_ids(plan: Plan, statuses: dict[str, str]) -> list[str]:
    completed = {node_id for node_id, status in statuses.items() if status == "completed"}
    return [
        node.id
        for node in plan.nodes
        if statuses.get(node.id, "pending") == "pending"
        and all(dep in completed for dep in node.dependencies)
    ]
