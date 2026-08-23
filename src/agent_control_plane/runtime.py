"""Public control-plane runtime facade."""

from dataclasses import asdict

from .completion import require_goal_coverage
from .controller import ControlPlane as _ControlPlane
from .controller import RunBlocked, RunNotFound
from .errors import CheckpointInvalid, FailureCategory, FailureRecord
from .events import EventType
from .models import BudgetLimit, BudgetState, NodeStatus, Plan, Run, RunState
from .policy import PolicyDecision
from .projections import project_run


class ControlPlane(_ControlPlane):
    """Public runtime with fail-closed goal, policy, recovery and terminal semantics."""

    def _validate_plan_for_runtime(self, run: Run, plan: Plan) -> None:
        super()._validate_plan_for_runtime(run, plan)
        require_goal_coverage(run.goal, plan)
        probe_budget = BudgetState(BudgetLimit())
        for node in plan.nodes:
            name = node.required_capabilities[0]
            self.capabilities.resolve(
                name,
                probe_budget,
                side_effect_class=node.side_effect_class,
            )

    def _policy_decision(self, run: Run, node_id: str, provider: object) -> PolicyDecision:
        descriptor = provider.descriptor  # type: ignore[attr-defined]
        decision = self.policy.evaluate_action(
            side_effect_class=descriptor.side_effect_class,
            run_risk=run.risk_level,
            capability_risk=descriptor.risk_class,
            reversible=descriptor.reversible,
            estimated_cost_usd=descriptor.estimated_cost_usd,
            permissions=descriptor.permissions,
        )
        self._append(
            run.id,
            EventType.POLICY_EVALUATED,
            {
                "node_id": node_id,
                "policy_id": decision.policy_id,
                "decision": decision.decision.value,
                "reason": decision.reason,
                "required_verification": list(decision.required_verification),
                "justification_required": decision.justification_required,
                "reversible_required": decision.reversible_required,
                "max_estimated_cost_usd": decision.max_estimated_cost_usd,
            },
        )
        if decision.reversible_required and not descriptor.reversible:
            raise RunBlocked("policy requires reversible execution")
        if (
            decision.max_estimated_cost_usd is not None
            and descriptor.estimated_cost_usd > decision.max_estimated_cost_usd
        ):
            raise RunBlocked("policy step-cost ceiling exceeded")
        return decision

    def request_replan(
        self,
        run_id: str,
        reason: str,
        *,
        invalidated_nodes: set[str] | None = None,
    ) -> Run:
        run = self.get_run(run_id)
        limit = run.budget.limit.max_replans
        if limit is not None and run.budget.replans >= limit:
            return self._fail(
                run,
                FailureRecord(FailureCategory.BUDGET_EXHAUSTED, "replan limit reached"),
            )
        old_plan = run.plan
        if invalidated_nodes:
            for node_id in self._dependent_closure(run, invalidated_nodes):
                current = self.get_run(run.id)
                status = current.node_status.get(node_id, NodeStatus.PENDING)
                if status != NodeStatus.INVALIDATED:
                    self._set_node(current, node_id, NodeStatus.INVALIDATED)
                    self._append(
                        run.id,
                        EventType.NODE_INVALIDATED,
                        {"node_id": node_id, "reason": reason},
                    )
        run = self.get_run(run.id)
        if run.state != RunState.PLANNING:
            run = self._transition(run, RunState.PLANNING)
        self._append(run.id, EventType.PLAN_REVISION_REQUESTED, {"reason": reason})
        plan = self.planner.revise_plan(
            run=run,
            reason=reason,
            version=run.plan_version + 1,
        )
        self._validate_plan_for_runtime(run, plan)

        old_nodes = {node.id: node for node in old_plan.nodes} if old_plan else {}
        new_nodes = {node.id: node for node in plan.nodes}
        invalidated = invalidated_nodes or set()
        invalidated_closure = (
            self._dependent_closure(run, invalidated) if invalidated else set()
        )
        preserved = sorted(
            node.id
            for node in plan.nodes
            if run.node_status.get(node.id) == NodeStatus.COMPLETED
            and node.id not in invalidated_closure
            and old_nodes.get(node.id) == node
        )
        added = sorted(set(new_nodes) - set(old_nodes))
        removed = sorted(set(old_nodes) - set(new_nodes))
        changed = sorted(
            node_id
            for node_id in set(old_nodes).intersection(new_nodes)
            if old_nodes[node_id] != new_nodes[node_id]
        )
        self._append(
            run.id,
            EventType.PLAN_DIFF_RECORDED,
            {
                "from_version": old_plan.version if old_plan else 0,
                "to_version": plan.version,
                "added": added,
                "removed": removed,
                "changed": changed,
                "preserved_completed": preserved,
                "reason": reason,
            },
        )
        self._append(
            run.id,
            EventType.PLAN_REVISED,
            {
                "plan": asdict(plan),
                "preserved_completed": preserved,
                "reason": reason,
            },
        )
        return self._transition(self.get_run(run.id), RunState.READY)

    def _complete(self, run: Run) -> Run:
        if run.plan is None:
            raise RunBlocked("cannot complete a run without a plan")
        require_goal_coverage(run.goal, run.plan)
        if not run.node_status or any(
            status != NodeStatus.COMPLETED for status in run.node_status.values()
        ):
            raise RunBlocked("cannot complete while plan nodes remain unfinished")
        if run.state != RunState.VERIFYING:
            run = self._transition(run, RunState.VERIFYING)
        run = self._transition(run, RunState.COMPLETED)
        self._append(
            run.id,
            EventType.RUN_COMPLETED,
            {"outcome": "full goal contract satisfied"},
        )
        return self.get_run(run.id)

    def cancel_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            return run
        self._append(run.id, EventType.CANCEL_REQUESTED, {})
        run = self._transition(run, RunState.CANCELLING)
        compensation_failed = False
        if self.compensator is not None:
            for action in reversed(self.list_actions(run.id)):
                intent = action["intent"]
                receipt = action["receipt"]
                if receipt is None or not bool(intent["reversible"]):
                    continue
                self._append(
                    run.id,
                    EventType.COMPENSATION_ATTEMPTED,
                    {"intent_id": intent["id"], "receipt_id": receipt["id"]},
                )
                result = self.compensator.compensate(run_id=run.id, action=action)
                kind = (
                    EventType.COMPENSATION_SUCCEEDED
                    if result.success
                    else EventType.COMPENSATION_FAILED
                )
                self._append(
                    run.id,
                    kind,
                    {
                        "intent_id": intent["id"],
                        "detail": result.detail,
                        "metadata": result.metadata or {},
                    },
                )
                compensation_failed = compensation_failed or not result.success
        run = self._transition(self.get_run(run.id), RunState.CANCELLED)
        self._append(
            run.id,
            EventType.RUN_CANCELLED,
            {
                "outcome": (
                    "cancelled_with_compensation_failure"
                    if compensation_failed
                    else "cancelled"
                )
            },
        )
        return self.get_run(run.id)

    def resume_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state != RunState.PAUSED:
            raise RunBlocked("run is not paused")
        checkpoints = self.list_checkpoints(run_id)
        if checkpoints:
            checkpoint = checkpoints[-1]
            sequence = int(checkpoint["event_sequence"])
            projected = project_run(self._events(run_id)[:sequence])
            if projected is None:
                raise CheckpointInvalid("checkpoint references an empty projection")
            if self._checkpoint_digest(projected) != checkpoint["projected_state_digest"]:
                raise CheckpointInvalid("checkpoint digest does not match replayed state")

            unresolved = {
                str(item) for item in checkpoint.get("unresolved_action_intents", [])
            }
            records = self._intent_records(run_id)
            started = {
                str(event.payload["intent_id"])
                for event in self._events(run_id)
                if event.type == EventType.ACTION_STARTED
            }
            for intent_id in unresolved:
                record = records.get(intent_id)
                if record is None:
                    raise CheckpointInvalid("checkpoint references unknown action intent")
                if record["receipt"] is not None or record["rejected"]:
                    continue
                if intent_id in started:
                    raise RunBlocked(
                        "unsafe resume: started side effect has no verified receipt"
                    )
                if not record["authorized"]:
                    raise RunBlocked("unsafe resume: unresolved side-effect intent")

        self._append(run.id, EventType.RESUME_REQUESTED, {})
        target = RunState.READY if run.plan is not None else RunState.PLANNING
        run = self._transition(run, target)
        self._append(run.id, EventType.RUN_RESUMED, {"state": target.value})
        return self.get_run(run.id)


__all__ = ["ControlPlane", "RunBlocked", "RunNotFound"]
