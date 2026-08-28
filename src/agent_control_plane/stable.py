from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from .audit import redact_for_audit, validate_plan_secrets
from .capabilities import (
    CapabilityAuthorizationError,
    CapabilityBudgetExceeded,
    CapabilityNotFound,
)
from .completion import require_goal_coverage
from .control_actions import DEFAULT_OBSERVATION_POLICY, ControlAction, ObservationPolicy
from .controller import ControlPlane as CoreControlPlane
from .errors import FailureCategory, FailureRecord
from .events import Event, EventType
from .ids import new_id
from .models import (
    ActionReceipt,
    BudgetLimit,
    BudgetState,
    NodeStatus,
    ObservationClass,
    Plan,
    Run,
    RunState,
    VerificationResult,
    VerificationStatus,
)
from .policy import PolicyDecision, PolicyDecisionType
from .projections import now_iso
from .runtime import ControlPlane as RecoveryControlPlane
from .runtime import RunBlocked, RunNotFound


class ControlPlane(RecoveryControlPlane):
    """Stable public runtime with final control, audit and recovery invariants."""

    _observation_policy: ObservationPolicy = DEFAULT_OBSERVATION_POLICY

    def configure_observation_policy(self, policy: ObservationPolicy) -> None:
        self._observation_policy = policy

    def control_action_for(self, classification: ObservationClass) -> ControlAction:
        return self._observation_policy.action_for(classification)

    def _record_observation(
        self,
        run_id: str,
        classification: ObservationClass,
        *,
        detail: str,
        node_id: str | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "class": classification.value,
            "detail": detail,
            "default_control_action": self.control_action_for(classification).value,
            **extra,
        }
        if node_id is not None:
            payload["node_id"] = node_id
        self._append(run_id, EventType.OBSERVATION_RECORDED, payload)

    def _append(self, run_id: str, kind: EventType, payload: dict[str, Any]) -> Event:
        if kind == EventType.ACTION_RECEIPT_RECORDED and "receipt" in payload:
            enriched = dict(payload)
            receipt = dict(payload["receipt"])
            intent_id = str(receipt["intent_id"])
            started = next(
                (
                    event
                    for event in reversed(self.store.load(run_id))
                    if event.type == EventType.ACTION_STARTED
                    and event.payload.get("intent_id") == intent_id
                ),
                None,
            )
            if started is not None:
                receipt["started_at"] = started.occurred_at
            receipt["completed_at"] = now_iso()
            enriched["receipt"] = receipt
            payload = enriched
        elif kind == EventType.CAPABILITY_RESOLVED:
            enriched = dict(payload)
            enriched.setdefault(
                "selection_reason",
                "cheapest eligible provider after capability, side-effect, authorization, "
                "risk and hard-budget filtering",
            )
            payload = enriched
        elif kind == EventType.HUMAN_GATE_RESOLVED:
            enriched = dict(payload)
            enriched.setdefault("decided_at", now_iso())
            payload = enriched
        return super()._append(run_id, kind, payload)

    def _validate_plan_for_runtime(self, run: Run, plan: Plan) -> None:
        validate_plan_secrets(plan)
        CoreControlPlane._validate_plan_for_runtime(self, run, plan)
        require_goal_coverage(run.goal, plan)
        unlimited = BudgetState(BudgetLimit())
        for node in plan.nodes:
            self.capabilities.resolve(
                node.required_capabilities[0],
                unlimited,
                side_effect_class=node.side_effect_class,
            )

    def _planner_usage(self) -> tuple[float, int, int]:
        cost = float(getattr(self.planner, "estimated_cost_usd_per_plan", 0.0))
        elapsed = int(getattr(self.planner, "estimated_elapsed_ms_per_plan", 0))
        model_calls = int(getattr(self.planner, "model_calls_per_plan", 1))
        if cost < 0 or elapsed < 0 or model_calls < 0:
            raise ValueError("planner resource estimates must be non-negative")
        return cost, elapsed, model_calls

    def _planning_fits_budget(self, run: Run) -> bool:
        cost, elapsed, model_calls = self._planner_usage()
        return run.budget.can_spend(
            estimated_cost_usd=cost,
            estimated_elapsed_ms=elapsed,
            model_calls=model_calls,
        )

    def _consume_planner_budget(self, run_id: str) -> None:
        cost, elapsed, model_calls = self._planner_usage()
        if cost == 0 and elapsed == 0 and model_calls == 0:
            return
        run = self.get_run(run_id)
        self._append(
            run_id,
            EventType.BUDGET_UPDATED,
            {
                "spent_cost_usd": run.budget.spent_cost_usd + cost,
                "elapsed_ms": run.budget.elapsed_ms + elapsed,
                "model_calls": run.budget.model_calls + model_calls,
                "tool_calls": run.budget.tool_calls,
            },
        )

    def plan_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state not in {
            RunState.CREATED,
            RunState.PLANNING,
            RunState.READY,
            RunState.PAUSED,
        }:
            raise RunBlocked(f"cannot plan from {run.state.value}")
        if not self._planning_fits_budget(run):
            return self._fail(
                run,
                FailureRecord(
                    FailureCategory.BUDGET_EXHAUSTED,
                    "planner call cannot fit the remaining run budget",
                ),
            )
        self._consume_planner_budget(run_id)
        return super().plan_run(run_id)

    def _policy_decision(self, run: Run, node_id: str, provider: Any) -> PolicyDecision:
        descriptor = provider.descriptor
        decision = self.policy.evaluate_action(
            side_effect_class=descriptor.side_effect_class,
            run_risk=run.risk_level,
            capability_risk=descriptor.risk_class,
            reversible=descriptor.reversible,
            estimated_cost_usd=descriptor.estimated_cost_usd,
            permissions=descriptor.permissions,
        )
        ceiling = dict(decision.budget_ceiling)
        self._append(
            run.id,
            EventType.POLICY_EVALUATED,
            {
                "node_id": node_id,
                "policy_id": decision.policy_id,
                "policy_version": decision.policy_version,
                "decision": decision.decision.value,
                "reason": decision.reason,
                "required_verification": list(decision.required_verification),
                "justification_required": decision.justification_required,
                "reversible_required": decision.reversible_required,
                "max_estimated_cost_usd": decision.max_estimated_cost_usd,
                "budget_ceiling": ceiling,
            },
        )
        if decision.decision == PolicyDecisionType.DENY:
            self._record_observation(
                run.id,
                ObservationClass.POLICY_BLOCK,
                detail=decision.reason,
                node_id=node_id,
                policy_id=decision.policy_id,
            )
        if decision.reversible_required and not descriptor.reversible:
            self._record_observation(
                run.id,
                ObservationClass.POLICY_BLOCK,
                detail="policy requires reversible execution",
                node_id=node_id,
            )
            raise RunBlocked("policy requires reversible execution")
        if (
            decision.max_estimated_cost_usd is not None
            and descriptor.estimated_cost_usd > decision.max_estimated_cost_usd
        ):
            self._record_observation(
                run.id,
                ObservationClass.POLICY_BLOCK,
                detail="policy step-cost ceiling exceeded",
                node_id=node_id,
            )
            raise RunBlocked("policy step-cost ceiling exceeded")
        checks = {
            "max_cost_usd": run.budget.spent_cost_usd + descriptor.estimated_cost_usd,
            "max_elapsed_ms": run.budget.elapsed_ms + descriptor.estimated_elapsed_ms,
            "max_model_calls": run.budget.model_calls + descriptor.estimated_model_calls,
            "max_tool_calls": run.budget.tool_calls + descriptor.estimated_tool_calls,
        }
        for key, projected in checks.items():
            limit = ceiling.get(key)
            if limit is not None and projected > limit:
                self._record_observation(
                    run.id,
                    ObservationClass.POLICY_BLOCK,
                    detail=f"policy budget ceiling exceeded: {key}",
                    node_id=node_id,
                )
                raise RunBlocked(f"policy budget ceiling exceeded: {key}")

        if decision.required_verification:
            if run.plan is None:
                raise RunBlocked("policy verification requirements need an active plan")
            node = next(item for item in run.plan.nodes if item.id == node_id)
            configured = {kind.value for kind in node.verification.kinds}
            missing = set(decision.required_verification) - configured
            if missing:
                names = ", ".join(sorted(missing))
                self._record_observation(
                    run.id,
                    ObservationClass.POLICY_BLOCK,
                    detail=f"missing policy-required verification: {names}",
                    node_id=node_id,
                )
                raise RunBlocked(f"plan is missing policy-required verification: {names}")
            return replace(
                decision,
                required_verification=(node.verification.kind.value,),
            )
        return decision

    @staticmethod
    def _descendant_closure(plan: Plan, roots: set[str]) -> set[str]:
        result = set(roots)
        changed = True
        while changed:
            changed = False
            for node in plan.nodes:
                if node.id not in result and any(dep in result for dep in node.dependencies):
                    result.add(node.id)
                    changed = True
        return result

    def request_replan(
        self,
        run_id: str,
        reason: str,
        *,
        invalidated_nodes: set[str] | None = None,
    ) -> Run:
        run = self.get_run(run_id)
        if run.plan is None:
            raise RunBlocked("cannot replan before an initial plan exists")
        limit = run.budget.limit.max_replans
        if limit is not None and run.budget.replans >= limit:
            return self._fail(
                run,
                FailureRecord(FailureCategory.BUDGET_EXHAUSTED, "replan limit reached"),
            )
        if not self._planning_fits_budget(run):
            return self._fail(
                run,
                FailureRecord(
                    FailureCategory.BUDGET_EXHAUSTED,
                    "replanner call cannot fit the remaining run budget",
                ),
            )

        old_plan = run.plan
        explicit = set(invalidated_nodes or ())
        explicit_old = self._descendant_closure(old_plan, explicit) if explicit else set()
        for node_id in explicit_old:
            current = self.get_run(run.id)
            status = current.node_status.get(node_id, NodeStatus.PENDING)
            if status == NodeStatus.RUNNING:
                current = self._set_node(current, node_id, NodeStatus.PENDING)
                status = NodeStatus.PENDING
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
        try:
            plan = self.planner.revise_plan(
                run=run,
                reason=reason,
                version=run.plan_version + 1,
            )
        finally:
            self._consume_planner_budget(run.id)
        self._validate_plan_for_runtime(self.get_run(run.id), plan)

        old_nodes = {node.id: node for node in old_plan.nodes}
        new_nodes = {node.id: node for node in plan.nodes}
        structural_changed = {
            node_id
            for node_id in set(old_nodes).intersection(new_nodes)
            if old_nodes[node_id] != new_nodes[node_id]
        }
        affected = set(explicit_old)
        if explicit:
            affected.update(self._descendant_closure(plan, explicit))
        if structural_changed:
            affected.update(self._descendant_closure(plan, structural_changed))

        for node_id in sorted(affected):
            current = self.get_run(run.id)
            current_status = current.node_status.get(node_id)
            if current_status is None or current_status == NodeStatus.INVALIDATED:
                continue
            if current_status == NodeStatus.RUNNING:
                current = self._set_node(current, node_id, NodeStatus.PENDING)
            self._set_node(current, node_id, NodeStatus.INVALIDATED)
            self._append(
                run.id,
                EventType.NODE_INVALIDATED,
                {"node_id": node_id, "reason": "structural dependency changed"},
            )

        current = self.get_run(run.id)
        preserved = sorted(
            node.id
            for node in plan.nodes
            if current.node_status.get(node.id) == NodeStatus.COMPLETED
            and node.id not in affected
            and old_nodes.get(node.id) == node
        )
        self._append(
            run.id,
            EventType.PLAN_DIFF_RECORDED,
            {
                "from_version": old_plan.version,
                "to_version": plan.version,
                "added": sorted(set(new_nodes) - set(old_nodes)),
                "removed": sorted(set(old_nodes) - set(new_nodes)),
                "changed": sorted(structural_changed),
                "preserved_completed": preserved,
                "reason": reason,
            },
        )
        self._append(
            run.id,
            EventType.PLAN_REVISED,
            {"plan": asdict(plan), "preserved_completed": preserved, "reason": reason},
        )
        return self._transition(self.get_run(run.id), RunState.READY)

    @staticmethod
    def _verification_from_payload(payload: dict[str, Any]) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus(str(payload["status"])),
            message=str(payload.get("message", "")),
            evidence=dict(payload.get("evidence", {})),
        )

    def _ensure_effect_receipts(self, run_id: str) -> None:
        events = self.list_events(run_id)
        records = self._intent_records(run_id)
        for record in records.values():
            if record["receipt"] is not None:
                continue
            intent = record["intent"]
            intent_id = str(intent["id"])
            node_id = str(intent["node_id"])
            started = next(
                (
                    event
                    for event in events
                    if event.type == EventType.ACTION_STARTED
                    and event.payload.get("intent_id") == intent_id
                ),
                None,
            )
            if started is None:
                continue
            invoked = next(
                (
                    event
                    for event in events
                    if event.sequence > started.sequence
                    and event.type == EventType.CAPABILITY_INVOKED
                    and event.payload.get("node_id") == node_id
                ),
                None,
            )
            if invoked is None:
                continue
            verification_event = next(
                (
                    event
                    for event in reversed(events)
                    if event.sequence > invoked.sequence
                    and event.type == EventType.VERIFICATION_RECORDED
                    and event.payload.get("node_id") == node_id
                ),
                None,
            )
            if verification_event is None:
                verification = VerificationResult(
                    VerificationStatus.BLOCKED,
                    "execution observed but verification was not recorded",
                    {
                        "execution_observed": True,
                        "verification_not_recorded": True,
                    },
                )
            else:
                verification = self._verification_from_payload(
                    dict(verification_event.payload["result"])
                )
            receipt = ActionReceipt(
                id=new_id("receipt"),
                intent_id=intent_id,
                actual_effects=(),
                verification=verification,
            )
            self._append(
                run_id,
                EventType.ACTION_RECEIPT_RECORDED,
                {"receipt": asdict(receipt)},
            )

    def _started_unresolved_action(self, run_id: str) -> dict[str, Any] | None:
        started = {
            str(event.payload["intent_id"])
            for event in self.list_events(run_id)
            if event.type == EventType.ACTION_STARTED
        }
        for record in self._intent_records(run_id).values():
            intent_id = str(record["intent"]["id"])
            if intent_id in started and record["receipt"] is None and not record["rejected"]:
                return record
        return None

    def execute_next(self, run_id: str) -> Run:
        try:
            result = super().execute_next(run_id)
        except CapabilityBudgetExceeded as exc:
            self._record_observation(
                run_id,
                ObservationClass.BUDGET_PRESSURE,
                detail=f"no eligible provider fits the remaining hard budget: {exc}",
            )
            raise RunBlocked("budget pressure: no eligible provider") from exc
        except CapabilityAuthorizationError as exc:
            self._record_observation(
                run_id,
                ObservationClass.POLICY_BLOCK,
                detail=str(exc),
            )
            raise RunBlocked("capability authorization envelope blocks execution") from exc
        except CapabilityNotFound as exc:
            return self._fail(
                self.get_run(run_id),
                FailureRecord(FailureCategory.CAPABILITY_UNAVAILABLE, str(exc)),
            )
        except RunBlocked:
            raise
        except Exception as exc:
            record = self._started_unresolved_action(run_id)
            if record is None:
                raise
            node_id = str(record["intent"]["node_id"])
            self._record_observation(
                run_id,
                ObservationClass.UNKNOWN_FAILURE,
                detail=f"ambiguous side effect after started action: {exc}",
                node_id=node_id,
            )
            current = self.get_run(run_id)
            if current.node_status.get(node_id) == NodeStatus.RUNNING:
                self._set_node(current, node_id, NodeStatus.BLOCKED)
            return self.pause_run(run_id, reason="ambiguous side effect")
        self._ensure_effect_receipts(run_id)
        return self.get_run(result.id)

    def pause_run(self, run_id: str, *, reason: str = "operator request") -> Run:
        self._ensure_effect_receipts(run_id)
        return super().pause_run(run_id, reason=reason)

    def reverify_node(self, run_id: str, node_id: str) -> Run:
        run = self.get_run(run_id)
        if run.plan is None:
            raise RunBlocked("run has no plan")
        node = next((item for item in run.plan.nodes if item.id == node_id), None)
        if node is None:
            raise RunBlocked("node is not in the active plan")
        if node_id not in run.node_outputs:
            raise RunBlocked("node has no persisted output to reverify")
        if run.state == RunState.PAUSED:
            run = self.resume_run(run_id)
        if run.state != RunState.VERIFYING:
            run = self._transition(run, RunState.VERIFYING)
        if run.node_status.get(node_id) != NodeStatus.VERIFYING:
            run = self._set_node(run, node_id, NodeStatus.VERIFYING)

        self._append(
            run.id,
            EventType.VERIFICATION_STARTED,
            {"node_id": node_id, "reverification": True},
        )
        verification = self.verifiers.verify(
            node.verification,
            run.node_outputs[node_id],
            {"run_id": run.id, "node_id": node_id, "reverification": True},
        )
        payload = {"node_id": node_id, "result": asdict(verification), "reverification": True}
        self._append(run.id, EventType.VERIFICATION_FINISHED, payload)
        self._append(run.id, EventType.VERIFICATION_RECORDED, payload)

        intent_record = next(
            (
                record
                for record in self._intent_records(run.id).values()
                if record["intent"]["node_id"] == node_id
                and int(record["intent"]["plan_version"]) == run.plan_version
            ),
            None,
        )
        if intent_record is not None:
            previous = intent_record["receipt"] or {}
            receipt = ActionReceipt(
                id=new_id("receipt"),
                intent_id=str(intent_record["intent"]["id"]),
                actual_effects=tuple(previous.get("actual_effects", ())),
                verification=verification,
                rollback_ref=previous.get("rollback_ref"),
                precondition_refs=tuple(previous.get("precondition_refs", ())),
                artifact_refs=tuple(previous.get("artifact_refs", ())),
            )
            self._append(
                run.id,
                EventType.ACTION_RECEIPT_RECORDED,
                {"receipt": asdict(receipt)},
            )

        if verification.status == VerificationStatus.PASS:
            run = self._set_node(self.get_run(run.id), node_id, NodeStatus.COMPLETED)
            self._append(run.id, EventType.NODE_COMPLETED, {"node_id": node_id})
            run = self._transition(self.get_run(run.id), RunState.READY)
            if run.node_status and all(
                status == NodeStatus.COMPLETED for status in run.node_status.values()
            ):
                return self._complete(run)
            return run
        if verification.status in {
            VerificationStatus.BLOCKED,
            VerificationStatus.INCONCLUSIVE,
            VerificationStatus.RETRY,
        }:
            self._set_node(self.get_run(run.id), node_id, NodeStatus.BLOCKED)
            return self.pause_run(run.id, reason=verification.message or "verification blocked")
        return self._fail(
            self.get_run(run.id),
            FailureRecord(
                FailureCategory.VERIFICATION_FAILED,
                verification.message or "verification failed",
                node_id=node_id,
            ),
        )

    def _fail(self, run: Run, failure: FailureRecord) -> Run:
        self._ensure_effect_receipts(run.id)
        classes = {
            FailureCategory.CAPABILITY_UNAVAILABLE: ObservationClass.CAPABILITY_FAILURE,
            FailureCategory.CAPABILITY_TIMEOUT: ObservationClass.CAPABILITY_FAILURE,
            FailureCategory.CAPABILITY_FAILURE: ObservationClass.CAPABILITY_FAILURE,
            FailureCategory.POLICY_DENIED: ObservationClass.POLICY_BLOCK,
            FailureCategory.BUDGET_EXHAUSTED: ObservationClass.BUDGET_PRESSURE,
            FailureCategory.VERIFICATION_FAILED: ObservationClass.VERIFICATION_FAILURE,
            FailureCategory.GOAL_UNREACHABLE: ObservationClass.GOAL_UNREACHABLE,
        }
        observation = classes.get(failure.category)
        if observation is not None:
            self._record_observation(
                run.id,
                observation,
                detail=failure.reason,
                node_id=failure.node_id,
                failure_category=failure.category.value,
            )
        return super()._fail(self.get_run(run.id), failure)

    @staticmethod
    def _remaining(limit: float | int | None, spent: float | int) -> float | int | None:
        if limit is None:
            return None
        return max(0, limit - spent)

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        result = super().inspect_run(run_id)
        run = self.get_run(run_id)
        events = self.list_events(run_id)
        result["active_assumptions"] = list(run.goal.assumptions)
        result["working_memory"] = (
            redact_for_audit(self.working_memory.snapshot())
            if self.working_memory is not None
            else []
        )
        result["verified_nodes"] = sorted(
            node_id
            for node_id, status in run.node_status.items()
            if status == NodeStatus.COMPLETED
        )
        result["unverified_nodes"] = sorted(
            node_id
            for node_id, status in run.node_status.items()
            if status != NodeStatus.COMPLETED
        )
        result["provider_selections"] = [
            dict(event.payload)
            for event in events
            if event.type == EventType.CAPABILITY_RESOLVED
        ]
        result["verification_results"] = [
            dict(event.payload)
            for event in events
            if event.type == EventType.VERIFICATION_RECORDED
        ]
        result["policy_decisions"] = [
            dict(event.payload)
            for event in events
            if event.type == EventType.POLICY_EVALUATED
        ]
        result["plan_diffs"] = [
            dict(event.payload)
            for event in events
            if event.type == EventType.PLAN_DIFF_RECORDED
        ]
        result["budget_remaining"] = {
            "cost_usd": self._remaining(
                run.budget.limit.max_cost_usd,
                run.budget.spent_cost_usd,
            ),
            "elapsed_ms": self._remaining(
                run.budget.limit.max_elapsed_ms,
                run.budget.elapsed_ms,
            ),
            "model_calls": self._remaining(
                run.budget.limit.max_model_calls,
                run.budget.model_calls,
            ),
            "tool_calls": self._remaining(
                run.budget.limit.max_tool_calls,
                run.budget.tool_calls,
            ),
            "replans": self._remaining(
                run.budget.limit.max_replans,
                run.budget.replans,
            ),
        }
        result["control_contract"] = {
            "goal": asdict(run.goal),
            "plan_version": run.plan_version,
            "state": run.state.value,
        }
        result["observation_policy"] = self._observation_policy.to_dict()
        return result


__all__ = ["ControlPlane", "RunBlocked", "RunNotFound"]
