"""Public control-plane runtime facade."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .audit import redact_for_audit, validate_plan_secrets
from .capabilities import CapabilityRegistry
from .completion import require_goal_coverage
from .compensation import Compensator
from .controller import ControlPlane as _ControlPlane
from .controller import RunBlocked, RunNotFound
from .errors import CheckpointInvalid, FailureCategory, FailureRecord
from .events import Event, EventType
from .ids import new_id
from .memory import WorkingMemory
from .models import (
    ActionReceipt,
    BudgetLimit,
    BudgetReservation,
    BudgetState,
    NodeStatus,
    Plan,
    Run,
    RunState,
    VerificationResult,
    VerificationStatus,
)
from .planner import Planner
from .plans import ready_node_ids
from .policy import PolicyDecision, PolicyEngine
from .projections import now_iso, project_run
from .resources import ExternalResourceVersionProvider
from .store import SQLiteEventStore
from .verification import VerifierRegistry


class ControlPlane(_ControlPlane):
    """Fail-closed runtime with goal, audit, budget, recovery, and policy invariants."""

    def __init__(
        self,
        *,
        store: SQLiteEventStore,
        planner: Planner,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine | None = None,
        verifiers: VerifierRegistry | None = None,
        compensator: Compensator | None = None,
        working_memory: WorkingMemory | None = None,
        resource_versions: ExternalResourceVersionProvider | None = None,
    ) -> None:
        super().__init__(
            store=store,
            planner=planner,
            capabilities=capabilities,
            policy=policy,
            verifiers=verifiers,
            compensator=compensator,
        )
        self.working_memory = working_memory
        self.resource_versions = resource_versions

    def _append(self, run_id: str, kind: EventType, payload: dict[str, Any]) -> Event:
        return super()._append(run_id, kind, redact_for_audit(payload))

    def _validate_plan_for_runtime(self, run: Run, plan: Plan) -> None:
        validate_plan_secrets(plan)
        super()._validate_plan_for_runtime(run, plan)
        require_goal_coverage(run.goal, plan)
        probe_budget = BudgetState(BudgetLimit())
        for node in plan.nodes:
            self.capabilities.resolve(
                node.required_capabilities[0],
                probe_budget,
                side_effect_class=node.side_effect_class,
            )

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
        self._append(
            run.id,
            EventType.PLAN_DIFF_RECORDED,
            {
                "from_version": old_plan.version if old_plan else 0,
                "to_version": plan.version,
                "added": sorted(set(new_nodes) - set(old_nodes)),
                "removed": sorted(set(old_nodes) - set(new_nodes)),
                "changed": sorted(
                    node_id
                    for node_id in set(old_nodes).intersection(new_nodes)
                    if old_nodes[node_id] != new_nodes[node_id]
                ),
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

    def _active_budget_reservations(self, run_id: str) -> dict[str, dict[str, Any]]:
        reservations: dict[str, dict[str, Any]] = {}
        settled: set[str] = set()
        for event in self._events(run_id):
            if event.type == EventType.BUDGET_RESERVED:
                reservation = dict(event.payload["reservation"])
                reservations[str(reservation["id"])] = reservation
            elif event.type in {EventType.BUDGET_CONSUMED, EventType.BUDGET_RELEASED}:
                settled.add(str(event.payload["reservation_id"]))
        return {
            reservation_id: reservation
            for reservation_id, reservation in reservations.items()
            if reservation_id not in settled
        }

    def list_budget_reservations(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._active_budget_reservations(run_id).values())

    def resolve_budget_reservation(
        self,
        run_id: str,
        reservation_id: str,
        *,
        consumed: bool,
        actual_cost_usd: float = 0.0,
        elapsed_ms: int = 0,
        model_calls: int = 0,
        tool_calls: int = 0,
        reason: str = "operator reconciliation",
    ) -> Run:
        active = self._active_budget_reservations(run_id)
        if reservation_id not in active:
            raise RunBlocked("budget reservation is not active")
        run = self.get_run(run_id)
        if consumed:
            self._append(
                run_id,
                EventType.BUDGET_UPDATED,
                {
                    "spent_cost_usd": run.budget.spent_cost_usd + actual_cost_usd,
                    "elapsed_ms": run.budget.elapsed_ms + elapsed_ms,
                    "model_calls": run.budget.model_calls + model_calls,
                    "tool_calls": run.budget.tool_calls + tool_calls,
                },
            )
            self._append(
                run_id,
                EventType.BUDGET_CONSUMED,
                {"reservation_id": reservation_id, "reason": reason},
            )
        else:
            self._append(
                run_id,
                EventType.BUDGET_RELEASED,
                {"reservation_id": reservation_id, "reason": reason},
            )
        return self.get_run(run_id)

    def execute_next(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state == RunState.CREATED:
            run = self.plan_run(run_id)
        if run.state == RunState.PAUSED:
            raise RunBlocked("run is paused")
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            return run
        if run.plan is None:
            run = self.plan_run(run_id)
        assert run.plan is not None
        if self._active_budget_reservations(run_id):
            raise RunBlocked("unresolved budget reservation requires reconciliation")
        ready = ready_node_ids(
            run.plan,
            {node_id: status.value for node_id, status in run.node_status.items()},
        )
        if not ready:
            return super().execute_next(run_id)
        node = next(item for item in run.plan.nodes if item.id == ready[0])
        provider = self.capabilities.resolve(
            node.required_capabilities[0],
            run.budget,
            side_effect_class=node.side_effect_class,
        )
        descriptor = provider.descriptor
        reservation = BudgetReservation(
            id=new_id("reservation"),
            run_id=run.id,
            node_id=node.id,
            provider_id=descriptor.provider_id,
            estimated_cost_usd=descriptor.estimated_cost_usd,
            estimated_elapsed_ms=descriptor.estimated_elapsed_ms,
            estimated_model_calls=descriptor.estimated_model_calls,
            estimated_tool_calls=descriptor.estimated_tool_calls,
        )
        reserved_event = self._append(
            run.id,
            EventType.BUDGET_RESERVED,
            {"reservation": asdict(reservation)},
        )
        before = self.get_run(run.id).budget
        try:
            result = super().execute_next(run.id)
        except Exception:
            new_events = [
                event
                for event in self._events(run.id)
                if event.sequence > reserved_event.sequence
            ]
            started = any(
                event.type == EventType.ACTION_STARTED
                and event.payload.get("node_id") == node.id
                for event in new_events
            )
            if not started:
                self._append(
                    run.id,
                    EventType.BUDGET_RELEASED,
                    {
                        "reservation_id": reservation.id,
                        "reason": "execution did not start",
                    },
                )
            raise
        new_events = [
            event
            for event in self._events(run.id)
            if event.sequence > reserved_event.sequence
        ]
        invoked = any(
            event.type == EventType.CAPABILITY_INVOKED
            and event.payload.get("node_id") == node.id
            for event in new_events
        )
        started = any(
            event.type == EventType.ACTION_STARTED
            and event.payload.get("node_id") == node.id
            for event in new_events
        )
        if invoked:
            after = self.get_run(run.id).budget
            self._append(
                run.id,
                EventType.BUDGET_CONSUMED,
                {
                    "reservation_id": reservation.id,
                    "actual_cost_usd": after.spent_cost_usd - before.spent_cost_usd,
                    "actual_elapsed_ms": after.elapsed_ms - before.elapsed_ms,
                    "actual_model_calls": after.model_calls - before.model_calls,
                    "actual_tool_calls": after.tool_calls - before.tool_calls,
                },
            )
        elif not started:
            self._append(
                run.id,
                EventType.BUDGET_RELEASED,
                {"reservation_id": reservation.id, "reason": "capability not invoked"},
            )
        return self.get_run(result.id)

    def _memory_snapshot_ref(self, snapshot: tuple[dict[str, Any], ...]) -> str:
        encoded = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return "memory:sha256:" + hashlib.sha256(encoded).hexdigest()

    def pause_run(self, run_id: str, *, reason: str = "operator request") -> Run:
        run = self.get_run(run_id)
        if run.state == RunState.PAUSED:
            return run
        self._append(run.id, EventType.PAUSE_REQUESTED, {"reason": reason})
        run = self._transition(self.get_run(run.id), RunState.PAUSING)
        projected = self.get_run(run.id)
        snapshot_ref: str | None = None
        if self.working_memory is not None:
            snapshot = self.working_memory.snapshot()
            safe_snapshot = tuple(redact_for_audit(item) for item in snapshot)
            snapshot_ref = self._memory_snapshot_ref(safe_snapshot)
            self._append(
                run.id,
                EventType.MEMORY_RECORDED,
                {
                    "kind": "checkpoint_snapshot",
                    "ref": snapshot_ref,
                    "snapshot": safe_snapshot,
                },
            )
        resource_versions = (
            self.resource_versions.snapshot(projected)
            if self.resource_versions is not None
            else ()
        )
        unresolved = tuple(
            record["intent"]["id"]
            for record in self._intent_records(run.id).values()
            if record["receipt"] is None and not record["rejected"]
        )
        checkpoint = {
            "id": new_id("checkpoint"),
            "run_id": run.id,
            "event_sequence": len(self._events(run.id)),
            "plan_version": projected.plan_version,
            "projected_state_digest": self._checkpoint_digest(projected),
            "working_memory_snapshot_ref": snapshot_ref,
            "budget_state": asdict(projected.budget),
            "unresolved_action_intents": list(unresolved),
            "unresolved_budget_reservations": list(
                self._active_budget_reservations(run.id)
            ),
            "external_resource_versions": list(resource_versions),
            "created_at": now_iso(),
        }
        self._append(run.id, EventType.CHECKPOINT_CREATED, {"checkpoint": checkpoint})
        run = self._transition(self.get_run(run.id), RunState.PAUSED)
        self._append(run.id, EventType.RUN_PAUSED, {"reason": reason})
        return self.get_run(run.id)

    def _restore_memory_snapshot(self, run_id: str, snapshot_ref: str) -> None:
        if self.working_memory is None:
            return
        for event in reversed(self._events(run_id)):
            if (
                event.type == EventType.MEMORY_RECORDED
                and event.payload.get("kind") == "checkpoint_snapshot"
                and event.payload.get("ref") == snapshot_ref
            ):
                snapshot = tuple(dict(item) for item in event.payload.get("snapshot", []))
                if self._memory_snapshot_ref(snapshot) != snapshot_ref:
                    raise CheckpointInvalid("working-memory snapshot digest mismatch")
                self.working_memory.restore(snapshot)
                return
        raise CheckpointInvalid("working-memory snapshot is missing")

    def reconcile_action(
        self,
        run_id: str,
        intent_id: str,
        *,
        occurred: bool,
        actual_effects: tuple[str, ...] = (),
        rollback_ref: str | None = None,
        precondition_refs: tuple[str, ...] = (),
        artifact_refs: tuple[str, ...] = (),
        actual_cost_usd: float = 0.0,
    ) -> Run:
        records = self._intent_records(run_id)
        record = records.get(intent_id)
        if record is None or record["receipt"] is not None:
            raise RunBlocked("action intent is not reconcilable")
        started_event = next(
            (
                event
                for event in self._events(run_id)
                if event.type == EventType.ACTION_STARTED
                and event.payload.get("intent_id") == intent_id
            ),
            None,
        )
        if started_event is None:
            raise RunBlocked("action was never started")
        intent = record["intent"]
        verification = VerificationResult(
            VerificationStatus.PASS,
            "operator reconciled ambiguous external effect",
            {"operator_reconciled": True, "occurred": occurred},
        )
        receipt = ActionReceipt(
            id=new_id("receipt"),
            intent_id=intent_id,
            actual_effects=actual_effects if occurred else (),
            verification=verification,
            rollback_ref=rollback_ref,
            started_at=started_event.occurred_at,
            completed_at=now_iso(),
            precondition_refs=precondition_refs,
            artifact_refs=artifact_refs,
        )
        self._append(
            run_id,
            EventType.ACTION_RECEIPT_RECORDED,
            {"receipt": asdict(receipt)},
        )
        run = self.get_run(run_id)
        node_id = str(intent["node_id"])
        if run.node_status.get(node_id) == NodeStatus.BLOCKED:
            self._set_node(
                run,
                node_id,
                NodeStatus.COMPLETED if occurred else NodeStatus.PENDING,
            )
        for reservation_id, reservation in self._active_budget_reservations(run_id).items():
            if reservation.get("node_id") == node_id:
                self.resolve_budget_reservation(
                    run_id,
                    reservation_id,
                    consumed=occurred,
                    actual_cost_usd=actual_cost_usd,
                    tool_calls=1 if occurred else 0,
                    reason="ambiguous action reconciled",
                )
        return self.get_run(run_id)

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
            snapshot_ref = checkpoint.get("working_memory_snapshot_ref")
            if snapshot_ref:
                self._restore_memory_snapshot(run_id, str(snapshot_ref))
            versions = tuple(
                str(item)
                for item in checkpoint.get("external_resource_versions", [])
            )
            if self.resource_versions is not None and versions:
                changed = self.resource_versions.validate(run, versions)
                if changed:
                    self._append(
                        run.id,
                        EventType.OBSERVATION_RECORDED,
                        {
                            "class": "input_changed",
                            "detail": "external resources changed while paused",
                            "resources": list(changed),
                        },
                    )
                    raise RunBlocked(
                        "external resource versions changed; replan required"
                    )
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
            active_reservations = self._active_budget_reservations(run_id)
            for reservation_id in checkpoint.get("unresolved_budget_reservations", []):
                key = str(reservation_id)
                if key not in active_reservations:
                    continue
                reservation = active_reservations[key]
                node_id = str(reservation["node_id"])
                started_for_node = any(
                    event.type == EventType.ACTION_STARTED
                    and event.payload.get("node_id") == node_id
                    for event in self._events(run_id)
                )
                if started_for_node:
                    raise RunBlocked("unsafe resume: budget use is ambiguous")
                self.resolve_budget_reservation(
                    run_id,
                    key,
                    consumed=False,
                    reason="unused reservation recovered on resume",
                )
        self._append(run.id, EventType.RESUME_REQUESTED, {})
        target = RunState.READY if run.plan is not None else RunState.PLANNING
        run = self._transition(run, target)
        self._append(run.id, EventType.RUN_RESUMED, {"state": target.value})
        return self.get_run(run.id)

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

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        result = super().inspect_run(run_id)
        result["budget_reservations"] = self.list_budget_reservations(run_id)
        result["working_memory_snapshot_ref"] = (
            self.working_memory.snapshot_ref()
            if self.working_memory is not None
            else None
        )
        return result

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
        for reservation_id in list(self._active_budget_reservations(run.id)):
            self.resolve_budget_reservation(
                run.id,
                reservation_id,
                consumed=False,
                reason="run cancelled",
            )
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


__all__ = ["ControlPlane", "RunBlocked", "RunNotFound"]
