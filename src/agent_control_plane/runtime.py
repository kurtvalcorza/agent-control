from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .capabilities import (
    CapabilityBudgetExceeded,
    CapabilityNotFound,
    CapabilityRegistry,
    TransientCapabilityError,
)
from .compensation import Compensator
from .events import Event, EventType
from .ids import new_id
from .models import (
    ActionIntent,
    ActionReceipt,
    AuthorizationState,
    BudgetLimit,
    Goal,
    HumanGate,
    NodeStatus,
    ObservationClass,
    Plan,
    RiskLevel,
    Run,
    RunState,
    VerificationStatus,
    is_side_effecting,
)
from .planner import Planner
from .plans import ready_node_ids, validate_plan
from .policy import PolicyDecisionType, PolicyEngine
from .projections import now_iso, project_run
from .state_machine import assert_node_transition, assert_transition
from .store import SQLiteEventStore
from .verification import DefaultVerifier, VerifierRegistry


class RunNotFound(KeyError):
    pass


class RunBlocked(RuntimeError):
    pass


class ControlPlane:
    def __init__(
        self,
        *,
        store: SQLiteEventStore,
        planner: Planner,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine | None = None,
        verifiers: VerifierRegistry | None = None,
        compensator: Compensator | None = None,
    ) -> None:
        self.store = store
        self.planner = planner
        self.capabilities = capabilities
        self.policy = policy or PolicyEngine()
        self.verifiers = verifiers or DefaultVerifier()
        self.compensator = compensator

    def _events(self, run_id: str) -> list[Event]:
        events = self.store.load(run_id)
        if not events:
            raise RunNotFound(run_id)
        return events

    def _append(self, run_id: str, event_type: EventType, payload: dict[str, Any]) -> Event:
        current = self.store.load(run_id)
        event = Event(
            id=new_id("evt"),
            run_id=run_id,
            type=event_type,
            occurred_at=now_iso(),
            sequence=len(current) + 1,
            payload=payload,
        )
        self.store.append(event, expected_sequence=len(current))
        return event

    def _transition(self, run: Run, target: RunState) -> Run:
        assert_transition(run.state, target)
        self._append(run.id, EventType.STATE_CHANGED, {"from": run.state.value, "to": target.value})
        return self.get_run(run.id)

    def _node_status(self, run: Run, node_id: str, target: NodeStatus) -> Run:
        current = run.node_status.get(node_id, NodeStatus.PENDING)
        assert_node_transition(current, target)
        self._append(
            run.id,
            EventType.NODE_STATUS_CHANGED,
            {"node_id": node_id, "status": target.value},
        )
        return self.get_run(run.id)

    @staticmethod
    def _goal_payload(goal: Goal) -> dict[str, Any]:
        return asdict(goal)

    @staticmethod
    def _plan_payload(plan: Plan) -> dict[str, Any]:
        return asdict(plan)

    def create_run(
        self,
        goal: Goal,
        *,
        risk_level: RiskLevel = RiskLevel.LOW,
        budget_limit: BudgetLimit | None = None,
        policy_profile: str = "default",
    ) -> Run:
        run_id = new_id("run")
        event = Event(
            id=new_id("evt"),
            run_id=run_id,
            type=EventType.RUN_CREATED,
            occurred_at=now_iso(),
            sequence=1,
            payload={
                "goal": self._goal_payload(goal),
                "risk_level": risk_level.value,
                "budget_limit": asdict(budget_limit or BudgetLimit()),
                "policy_profile": policy_profile,
            },
        )
        self.store.append(event, expected_sequence=0)
        self._append(run_id, EventType.GOAL_NORMALIZED, {"goal": self._goal_payload(goal)})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        run = project_run(self._events(run_id))
        if run is None:
            raise RunNotFound(run_id)
        return run

    def plan_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state == RunState.CREATED:
            run = self._transition(run, RunState.PLANNING)
        elif run.state not in {RunState.PLANNING, RunState.READY, RunState.PAUSED}:
            raise RunBlocked(f"cannot plan from state {run.state.value}")
        elif run.state != RunState.PLANNING:
            run = self._transition(run, RunState.PLANNING)
        self._append(run_id, EventType.PLANNING_STARTED, {"version": run.plan_version + 1})
        try:
            plan = self.planner.create_plan(
                run_id=run_id,
                goal=run.goal,
                version=run.plan_version + 1,
            )
            validate_plan(plan, run.goal.success_criteria)
            for node in plan.nodes:
                for capability in node.required_capabilities:
                    if not self.capabilities.has(capability):
                        raise CapabilityNotFound(capability)
            self._append(run_id, EventType.PLAN_PROPOSED, {"plan": self._plan_payload(plan)})
            self._append(run_id, EventType.PLAN_CREATED, {"plan": self._plan_payload(plan)})
            self._append(run_id, EventType.PLAN_ACCEPTED, {"plan_id": plan.id})
        except Exception as exc:
            self._append(run_id, EventType.PLAN_REJECTED, {"reason": str(exc)})
            raise
        return self._transition(self.get_run(run_id), RunState.READY)

    def request_replan(self, run_id: str, reason: str) -> Run:
        run = self.get_run(run_id)
        limit = run.budget.limit.max_replans
        if limit is not None and run.budget.replans >= limit:
            return self._fail(run, "BUDGET_EXHAUSTED: replan limit reached")
        if run.state != RunState.PLANNING:
            run = self._transition(run, RunState.PLANNING)
        self._append(run_id, EventType.PLAN_REVISION_REQUESTED, {"reason": reason})
        plan = self.planner.revise_plan(run=run, reason=reason, version=run.plan_version + 1)
        validate_plan(plan, run.goal.success_criteria)
        completed = {
            node_id
            for node_id, status in run.node_status.items()
            if status == NodeStatus.COMPLETED and any(n.id == node_id for n in plan.nodes)
        }
        self._append(
            run_id,
            EventType.PLAN_REVISED,
            {"plan": self._plan_payload(plan), "preserved_completed": sorted(completed)},
        )
        return self._transition(self.get_run(run_id), RunState.READY)

    def _find_authorized_intent(self, run: Run, node_id: str) -> dict[str, Any] | None:
        intents: dict[str, dict[str, Any]] = {}
        authorized: set[str] = set()
        receipts: set[str] = set()
        for event in self._events(run.id):
            if event.type == EventType.ACTION_INTENT_CREATED:
                intent = dict(event.payload["intent"])
                intents[str(intent["id"])] = intent
            elif event.type == EventType.ACTION_AUTHORIZED:
                authorized.add(str(event.payload["intent_id"]))
            elif event.type == EventType.ACTION_RECEIPT_RECORDED:
                receipts.add(str(event.payload["receipt"]["intent_id"]))
        for intent_id in authorized - receipts:
            intent = intents[intent_id]
            if intent["node_id"] == node_id and int(intent["plan_version"]) == run.plan_version:
                return intent
        return None

    def _open_gate(self, run: Run, node_id: str, capability_name: str, reversible: bool) -> Run:
        node = next(node for node in run.plan.nodes if node.id == node_id) if run.plan else None
        if node is None:
            raise RunBlocked("active node not found")
        intent = ActionIntent(
            id=new_id("intent"),
            run_id=run.id,
            node_id=node_id,
            plan_version=run.plan_version,
            capability=capability_name,
            arguments_digest=hashlib.sha256(
                json.dumps(node.inputs, sort_keys=True, default=str).encode()
            ).hexdigest(),
            expected_effect=node.objective,
            side_effect_class=node.side_effect_class,
            authorization_state=AuthorizationState.PENDING,
            reversible=reversible,
        )
        gate = HumanGate(
            id=new_id("gate"),
            reason="policy requires human approval",
            requested_action=intent.id,
            node_id=node_id,
        )
        self._append(run.id, EventType.ACTION_INTENT_CREATED, {"intent": asdict(intent)})
        self._append(run.id, EventType.HUMAN_GATE_OPENED, {"gate": asdict(gate)})
        run = self._node_status(self.get_run(run.id), node_id, NodeStatus.BLOCKED)
        if run.state != RunState.PAUSING:
            run = self._transition(run, RunState.PAUSING)
        run = self._transition(run, RunState.PAUSED)
        self._append(run.id, EventType.RUN_PAUSED, {"reason": "human_gate"})
        return self.get_run(run.id)

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
        ready = ready_node_ids(run.plan, {key: value.value for key, value in run.node_status.items()})
        if not ready:
            if all(status == NodeStatus.COMPLETED for status in run.node_status.values()):
                if run.state != RunState.VERIFYING:
                    run = self._transition(run, RunState.VERIFYING)
                self._append(run.id, EventType.RUN_COMPLETED, {"outcome": "success criteria satisfied"})
                return self.get_run(run.id)
            raise RunBlocked("no executable node is ready")
        node_id = ready[0]
        node = next(item for item in run.plan.nodes if item.id == node_id)
        if len(node.required_capabilities) != 1:
            raise RunBlocked("MVP requires exactly one capability per node")
        capability_name = node.required_capabilities[0]
        try:
            provider = self.capabilities.resolve(capability_name, run.budget)
        except CapabilityBudgetExceeded:
            self._append(
                run.id,
                EventType.OBSERVATION_RECORDED,
                {"class": ObservationClass.BUDGET_PRESSURE.value, "node_id": node_id},
            )
            raise RunBlocked("budget pressure: no eligible provider")
        descriptor = provider.descriptor
        if is_side_effecting(descriptor.side_effect_class) and not is_side_effecting(node.side_effect_class):
            raise RunBlocked("planner understated capability side effects")
        if node.side_effect_class != descriptor.side_effect_class:
            if is_side_effecting(node.side_effect_class) or is_side_effecting(descriptor.side_effect_class):
                raise RunBlocked("plan/provider side-effect classification mismatch")
        decision = self.policy.evaluate_action(
            side_effect_class=descriptor.side_effect_class,
            run_risk=run.risk_level,
            reversible=descriptor.reversible,
        )
        self._append(
            run.id,
            EventType.POLICY_EVALUATED,
            {
                "node_id": node_id,
                "policy_id": decision.policy_id,
                "decision": decision.decision.value,
                "reason": decision.reason,
            },
        )
        if decision.justification_required and not node.justification:
            raise RunBlocked("policy requires node justification")
        if decision.decision == PolicyDecisionType.DENY:
            raise RunBlocked(f"policy denied action: {decision.reason}")
        authorized_intent = self._find_authorized_intent(run, node_id)
        if decision.decision == PolicyDecisionType.REQUIRE_HUMAN and authorized_intent is None:
            return self._open_gate(run, node_id, capability_name, descriptor.reversible)
        if run.state != RunState.EXECUTING:
            run = self._transition(run, RunState.EXECUTING)
        run = self._node_status(run, node_id, NodeStatus.RUNNING)
        self._append(
            run.id,
            EventType.CAPABILITY_RESOLVED,
            {"node_id": node_id, "provider_id": descriptor.provider_id, "capability": capability_name},
        )
        try:
            result = provider.invoke(dict(node.inputs))
        except TransientCapabilityError as exc:
            retries = run.retry_counts.get(node_id, 0) + 1
            self._append(
                run.id,
                EventType.OBSERVATION_RECORDED,
                {
                    "class": ObservationClass.TRANSIENT_FAILURE.value,
                    "node_id": node_id,
                    "detail": str(exc),
                },
            )
            if retries > run.budget.limit.max_retries_per_node:
                return self._fail(self.get_run(run.id), "CAPABILITY_TIMEOUT: retry limit exceeded")
            self._append(run.id, EventType.RETRY_RECORDED, {"node_id": node_id, "count": retries})
            self._node_status(self.get_run(run.id), node_id, NodeStatus.PENDING)
            return self._transition(self.get_run(run.id), RunState.READY)
        except Exception as exc:
            if is_side_effecting(descriptor.side_effect_class):
                self._append(
                    run.id,
                    EventType.OBSERVATION_RECORDED,
                    {
                        "class": ObservationClass.UNKNOWN_FAILURE.value,
                        "node_id": node_id,
                        "detail": f"ambiguous side effect: {exc}",
                    },
                )
                self._node_status(self.get_run(run.id), node_id, NodeStatus.BLOCKED)
                paused = self.pause_run(run.id, reason="ambiguous side effect")
                return paused
            return self._fail(self.get_run(run.id), f"CAPABILITY_FAILURE: {exc}")
        self._append(
            run.id,
            EventType.CAPABILITY_INVOKED,
            {"node_id": node_id, "output": result.output, "provider_id": descriptor.provider_id},
        )
        budget = self.get_run(run.id).budget
        self._append(
            run.id,
            EventType.BUDGET_UPDATED,
            {
                "spent_cost_usd": budget.spent_cost_usd + result.actual_cost_usd,
                "elapsed_ms": budget.elapsed_ms + descriptor.estimated_elapsed_ms,
                "model_calls": budget.model_calls + descriptor.estimated_model_calls,
                "tool_calls": budget.tool_calls + descriptor.estimated_tool_calls,
            },
        )
        if not self.get_run(run.id).budget.can_spend():
            return self._fail(self.get_run(run.id), "BUDGET_EXHAUSTED")
        run = self._transition(self.get_run(run.id), RunState.VERIFYING)
        run = self._node_status(run, node_id, NodeStatus.VERIFYING)
        verification = self.verifiers.verify(
            node.verification,
            result.output,
            {"run_id": run.id, "node_id": node_id, "provider_id": descriptor.provider_id},
        )
        self._append(
            run.id,
            EventType.VERIFICATION_RECORDED,
            {"node_id": node_id, "result": asdict(verification)},
        )
        if verification.status == VerificationStatus.PASS:
            run = self._node_status(self.get_run(run.id), node_id, NodeStatus.COMPLETED)
            if is_side_effecting(descriptor.side_effect_class):
                intent = authorized_intent
                if intent is None:
                    intent_obj = ActionIntent(
                        id=new_id("intent"),
                        run_id=run.id,
                        node_id=node_id,
                        plan_version=run.plan_version,
                        capability=capability_name,
                        arguments_digest=hashlib.sha256(
                            json.dumps(node.inputs, sort_keys=True, default=str).encode()
                        ).hexdigest(),
                        expected_effect=node.objective,
                        side_effect_class=descriptor.side_effect_class,
                        authorization_state=AuthorizationState.NOT_REQUIRED,
                        reversible=descriptor.reversible,
                    )
                    intent = asdict(intent_obj)
                    self._append(run.id, EventType.ACTION_INTENT_CREATED, {"intent": intent})
                receipt = ActionReceipt(
                    id=new_id("receipt"),
                    intent_id=str(intent["id"]),
                    actual_effects=tuple(result.metadata.get("effects", (node.objective,))),
                    verification=verification,
                    rollback_ref=result.metadata.get("rollback_ref"),
                )
                self._append(run.id, EventType.ACTION_RECEIPT_RECORDED, {"receipt": asdict(receipt)})
            run = self._transition(self.get_run(run.id), RunState.READY)
            if all(status == NodeStatus.COMPLETED for status in run.node_status.values()):
                run = self._transition(run, RunState.VERIFYING)
                self._append(run.id, EventType.RUN_COMPLETED, {"outcome": "success criteria satisfied"})
                return self.get_run(run.id)
            return run
        if verification.status in {VerificationStatus.BLOCKED, VerificationStatus.INCONCLUSIVE}:
            self._node_status(self.get_run(run.id), node_id, NodeStatus.BLOCKED)
            return self.pause_run(run.id, reason=verification.message or "verification blocked")
        return self._fail(self.get_run(run.id), f"VERIFICATION_FAILED: {verification.message}")

    def run_until_blocked(self, run_id: str, *, max_steps: int = 100) -> Run:
        run = self.get_run(run_id)
        for _ in range(max_steps):
            if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.PAUSED}:
                return run
            try:
                run = self.execute_next(run_id)
            except RunBlocked:
                return self.get_run(run_id)
        return self._fail(self.get_run(run_id), "INTERNAL_INVARIANT_VIOLATION: step limit reached")

    def list_pending_gates(self, run_id: str) -> list[dict[str, Any]]:
        opened: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()
        for event in self._events(run_id):
            if event.type == EventType.HUMAN_GATE_OPENED:
                gate = dict(event.payload["gate"])
                opened[str(gate["id"])] = gate
            elif event.type == EventType.HUMAN_GATE_RESOLVED:
                resolved.add(str(event.payload["gate_id"]))
        return [gate for gate_id, gate in opened.items() if gate_id not in resolved]

    def approve_gate(self, run_id: str, gate_id: str, *, decided_by: str = "operator") -> Run:
        gate = next((item for item in self.list_pending_gates(run_id) if item["id"] == gate_id), None)
        if gate is None:
            raise RunBlocked("gate is not pending")
        intent_id = str(gate["requested_action"])
        self._append(
            run_id,
            EventType.HUMAN_GATE_RESOLVED,
            {"gate_id": gate_id, "status": "approved", "decided_by": decided_by},
        )
        self._append(run_id, EventType.ACTION_AUTHORIZED, {"intent_id": intent_id})
        run = self.get_run(run_id)
        if gate.get("node_id") and run.node_status.get(str(gate["node_id"])) == NodeStatus.BLOCKED:
            self._node_status(run, str(gate["node_id"]), NodeStatus.PENDING)
        return self.resume_run(run_id)

    def reject_gate(self, run_id: str, gate_id: str, *, decided_by: str = "operator") -> Run:
        gate = next((item for item in self.list_pending_gates(run_id) if item["id"] == gate_id), None)
        if gate is None:
            raise RunBlocked("gate is not pending")
        self._append(
            run_id,
            EventType.HUMAN_GATE_RESOLVED,
            {"gate_id": gate_id, "status": "rejected", "decided_by": decided_by},
        )
        if gate.get("requested_action"):
            self._append(
                run_id,
                EventType.ACTION_REJECTED,
                {"intent_id": str(gate["requested_action"])},
            )
        self._append(
            run_id,
            EventType.OBSERVATION_RECORDED,
            {"class": ObservationClass.HUMAN_REJECTED.value, "gate_id": gate_id},
        )
        return self.get_run(run_id)

    def _checkpoint_digest(self, run: Run) -> str:
        payload = {
            "run_id": run.id,
            "state": run.state.value,
            "plan_version": run.plan_version,
            "node_status": {key: value.value for key, value in sorted(run.node_status.items())},
            "budget": asdict(run.budget),
            "pending_action_intent_id": run.pending_action_intent_id,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def pause_run(self, run_id: str, *, reason: str = "operator request") -> Run:
        run = self.get_run(run_id)
        if run.state == RunState.PAUSED:
            return run
        self._append(run_id, EventType.PAUSE_REQUESTED, {"reason": reason})
        if run.state != RunState.PAUSING:
            run = self._transition(run, RunState.PAUSING)
        pre_checkpoint = self.get_run(run_id)
        checkpoint = {
            "id": new_id("checkpoint"),
            "run_id": run_id,
            "event_sequence": len(self._events(run_id)),
            "plan_version": pre_checkpoint.plan_version,
            "projected_state_digest": self._checkpoint_digest(pre_checkpoint),
            "working_memory_snapshot_ref": None,
            "budget_state": asdict(pre_checkpoint.budget),
            "unresolved_action_intents": (
                [pre_checkpoint.pending_action_intent_id]
                if pre_checkpoint.pending_action_intent_id
                else []
            ),
            "external_resource_versions": [],
            "created_at": now_iso(),
        }
        self._append(run_id, EventType.CHECKPOINT_CREATED, {"checkpoint": checkpoint})
        run = self._transition(self.get_run(run_id), RunState.PAUSED)
        self._append(run_id, EventType.RUN_PAUSED, {"reason": reason})
        return self.get_run(run_id)

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(event.payload["checkpoint"])
            for event in self._events(run_id)
            if event.type == EventType.CHECKPOINT_CREATED
        ]

    def resume_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state != RunState.PAUSED:
            raise RunBlocked("run is not paused")
        checkpoints = self.list_checkpoints(run_id)
        if checkpoints:
            checkpoint = checkpoints[-1]
            seq = int(checkpoint["event_sequence"])
            projected = project_run(self._events(run_id)[:seq])
            if projected is None or self._checkpoint_digest(projected) != checkpoint["projected_state_digest"]:
                raise RunBlocked("checkpoint digest validation failed")
            unresolved = tuple(checkpoint.get("unresolved_action_intents", ()))
            if unresolved and not self._authorized_or_rejected(run_id, unresolved):
                raise RunBlocked("unsafe resume: unresolved side-effect intent")
        self._append(run_id, EventType.RESUME_REQUESTED, {})
        target = RunState.READY if run.plan is not None else RunState.PLANNING
        run = self._transition(run, target)
        self._append(run_id, EventType.RUN_RESUMED, {"state": target.value})
        return self.get_run(run_id)

    def _authorized_or_rejected(self, run_id: str, intent_ids: tuple[str, ...]) -> bool:
        resolved: set[str] = set()
        for event in self._events(run_id):
            if event.type in {EventType.ACTION_AUTHORIZED, EventType.ACTION_REJECTED}:
                resolved.add(str(event.payload["intent_id"]))
        return set(intent_ids).issubset(resolved)

    def cancel_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            return run
        self._append(run_id, EventType.CANCEL_REQUESTED, {})
        run = self._transition(run, RunState.CANCELLING)
        compensation_failed = False
        if self.compensator is not None:
            for action in reversed(self.list_actions(run_id)):
                receipt = action.get("receipt")
                intent = action.get("intent")
                if not receipt or not intent or not bool(intent.get("reversible")):
                    continue
                self._append(
                    run_id,
                    EventType.COMPENSATION_ATTEMPTED,
                    {"intent_id": intent["id"], "receipt_id": receipt["id"]},
                )
                result = self.compensator.compensate(run_id=run_id, action=action)
                event_type = EventType.COMPENSATION_SUCCEEDED if result.success else EventType.COMPENSATION_FAILED
                self._append(
                    run_id,
                    event_type,
                    {"intent_id": intent["id"], "detail": result.detail, "metadata": result.metadata or {}},
                )
                compensation_failed = compensation_failed or not result.success
        self._append(
            run_id,
            EventType.RUN_CANCELLED,
            {"outcome": "cancelled_with_compensation_failure" if compensation_failed else "cancelled"},
        )
        return self.get_run(run_id)

    def list_actions(self, run_id: str) -> list[dict[str, Any]]:
        actions: dict[str, dict[str, Any]] = {}
        for event in self._events(run_id):
            if event.type == EventType.ACTION_INTENT_CREATED:
                intent = dict(event.payload["intent"])
                actions[str(intent["id"])] = {"intent": intent, "receipt": None}
            elif event.type == EventType.ACTION_RECEIPT_RECORDED:
                receipt = dict(event.payload["receipt"])
                actions.setdefault(str(receipt["intent_id"]), {"intent": None, "receipt": None})[
                    "receipt"
                ] = receipt
        return list(actions.values())

    def list_observations(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(event.payload)
            for event in self._events(run_id)
            if event.type == EventType.OBSERVATION_RECORDED
        ]

    def list_events(self, run_id: str) -> list[Event]:
        return self._events(run_id)

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        pending = self.list_pending_gates(run_id)
        ready: list[str] = []
        if run.plan is not None:
            ready = ready_node_ids(
                run.plan,
                {key: value.value for key, value in run.node_status.items()},
            )
        return {
            "run": run.to_dict(),
            "ready_nodes": ready,
            "pending_gates": pending,
            "actions": self.list_actions(run_id),
            "observations": self.list_observations(run_id)[-10:],
            "checkpoints": self.list_checkpoints(run_id),
            "event_count": len(self._events(run_id)),
        }

    def _fail(self, run: Run, reason: str) -> Run:
        if run.state not in {RunState.FAILED, RunState.COMPLETED, RunState.CANCELLED}:
            if run.state != RunState.FAILED:
                try:
                    run = self._transition(run, RunState.FAILED)
                except Exception:
                    pass
            self._append(run.id, EventType.RUN_FAILED, {"reason": reason})
        return self.get_run(run.id)
