from __future__ import annotations

from .models import NodeStatus, RunState


class InvalidTransition(ValueError):
    pass


_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset(
        {RunState.PLANNING, RunState.PAUSING, RunState.CANCELLING, RunState.FAILED}
    ),
    RunState.PLANNING: frozenset(
        {RunState.READY, RunState.PAUSING, RunState.CANCELLING, RunState.FAILED}
    ),
    RunState.READY: frozenset(
        {
            RunState.EXECUTING,
            RunState.VERIFYING,
            RunState.PLANNING,
            RunState.PAUSING,
            RunState.CANCELLING,
            RunState.FAILED,
        }
    ),
    RunState.EXECUTING: frozenset(
        {
            RunState.VERIFYING,
            RunState.READY,
            RunState.PLANNING,
            RunState.PAUSING,
            RunState.CANCELLING,
            RunState.FAILED,
        }
    ),
    RunState.VERIFYING: frozenset(
        {
            RunState.READY,
            RunState.EXECUTING,
            RunState.PLANNING,
            RunState.PAUSING,
            RunState.CANCELLING,
            RunState.COMPLETED,
            RunState.FAILED,
        }
    ),
    RunState.PAUSING: frozenset({RunState.PAUSED, RunState.FAILED}),
    RunState.PAUSED: frozenset(
        {RunState.READY, RunState.PLANNING, RunState.CANCELLING, RunState.FAILED}
    ),
    RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.FAILED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


def assert_transition(current: RunState, target: RunState) -> None:
    if target not in _ALLOWED[current]:
        raise InvalidTransition(f"invalid run transition: {current.value} -> {target.value}")


_NODE_ALLOWED: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset(
        {NodeStatus.READY, NodeStatus.RUNNING, NodeStatus.BLOCKED, NodeStatus.INVALIDATED}
    ),
    NodeStatus.READY: frozenset(
        {NodeStatus.RUNNING, NodeStatus.BLOCKED, NodeStatus.INVALIDATED}
    ),
    NodeStatus.RUNNING: frozenset(
        {NodeStatus.VERIFYING, NodeStatus.PENDING, NodeStatus.BLOCKED, NodeStatus.FAILED}
    ),
    NodeStatus.VERIFYING: frozenset(
        {
            NodeStatus.COMPLETED,
            NodeStatus.PENDING,
            NodeStatus.BLOCKED,
            NodeStatus.FAILED,
            NodeStatus.INVALIDATED,
        }
    ),
    NodeStatus.BLOCKED: frozenset(
        {NodeStatus.PENDING, NodeStatus.COMPLETED, NodeStatus.INVALIDATED, NodeStatus.FAILED}
    ),
    NodeStatus.COMPLETED: frozenset({NodeStatus.INVALIDATED}),
    NodeStatus.FAILED: frozenset({NodeStatus.INVALIDATED}),
    NodeStatus.INVALIDATED: frozenset({NodeStatus.PENDING}),
}


def assert_node_transition(current: NodeStatus, target: NodeStatus) -> None:
    if target not in _NODE_ALLOWED[current]:
        raise InvalidTransition(
            f"invalid node transition: {current.value} -> {target.value}"
        )
