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
from .errors import CheckpointInvalid, FailureCategory, FailureRecord
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
    SideEffectClass,
    VerificationStatus,
    is_side_effecting,
)
from .planner import Planner
from .plans import ready_node_ids, validate_plan
from .policy import PolicyDecision, PolicyDecisionType, PolicyEngine
from .projections import now_iso, project_run
from .state_machine import assert_node_transition, assert_transition
from .store import SQLiteEventStore
from .verification import DefaultVerifier, VerifierRegistry


class RunNotFound(KeyError):
    pass


class RunBlocked(RuntimeError):
    pass


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class ControlPlane:
    """Deterministic control runtime over an append-only event stream."""

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

    def _append(self, run_id: str, kind: EventType, payload: dict[str, Any]) -> Event:
        events = self.store.load(run_id)
        event = Event(
            id=new_id("evt"),
            run_id=run_id,
            type=kind,
            occurred_at=now_iso(),
            sequence=len(events) + 1,
            payload=payload,
        )
        self.store.append(event, expected_sequence=len(events))
        return event

    def get_run(self, run_id: str) -> Run:
        run = project_run(self._events(run_id))
        if run is None:
            raise RunNotFound(run_id)
        return run

    def _transition(self, run: Run, target: RunState) -> Run:
        if run.state == target:
            return run
        assert_transition(run.state, target)
        self._append(run.id, EventType.STATE_CHANGED, {"from": run.state.value, "to": target.value})
        return self.get_run(run.id)

    def _set_node(self, run: Run, node_id: str, target: NodeStatus) -> Run:
        current = run.node_status.get(node_id, NodeStatus.PENDING)
        if current == target:
            return run
        assert_node_transition(current, target)
        self._append(
            run.id,
            EventType.NODE_STATUS_CHANGED,
            {"node_id": node_id, "status": target.value},
        )
        return self.get_run(run.id)

    def create_run(
        self,
        goal: Goal,
        *,
        risk_level: RiskLevel = RiskLevel.LOW,
        budget_limit: BudgetLimit | None = None,
        policy_profile: str = "default",
    ) -> Run:
        run_id = new_id("run")
        created = Event(
            id=new_id("evt"),
            run_id=run_id,
            type=EventType.RUN_CREATED,
            occurred_at=now_iso(),
            sequence=1,
            payload={
                "goal": asdict(goal),
                "risk_level": risk_level.value,
                "budget_limit": asdict(budget_limit or BudgetLimit()),
                "policy_profile": policy_profile,
            },
        )
        self.store.append(created, expected_sequence=0)
        self._append(run_id, EventType.GOAL_NORMALIZED, {"goal": asdict(goal)})
        return self.get_run(run_id)

    def _validate_plan_for_runtime(self, run: Run, plan: Plan) -> None:
        validate_plan(plan, run.goal.success_criteria)
        if plan.run_id != run.id:
            raise ValueError("plan run_id does not match run")
        if plan.version != run.plan_version + 1:
            raise ValueError("plan version is not the next run plan version")
        for node in plan.nodes:
            if len(node.required_capabilities) != 1:
                raise ValueError("MVP requires exactly one capability per plan node")
            name = node.required_capabilities[0]
            descriptors = self.capabilities.descriptors(name)
            if not descriptors:
                raise CapabilityNotFound(name)
            if not any(item.side_effect_class == node.side_effect_class for item in descriptors):
                raise ValueError(
                    f"no provider for {name!r} matches side-effect class "
                    f"{node.side_effect_class.value!r}"
                )

    def plan_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state == RunState.CREATED:
            run = self._transition(run, RunState.PLANNING)
        elif run.state in {RunState.READY, RunState.PAUSED}:
            run = self._transition(run, RunState.PLANNING)
        elif run.state != RunState.PLANNING:
            raise RunBlocked(f"cannot plan from {run.state.value}")
        self._append(run.id, EventType.PLANNING_STARTED, {"version": run.plan_version + 1})
        try:
            plan = self.planner.create_plan(
                run_id=run.id,
                goal=run.goal,
                version=run.plan_version + 1,
            )
            self._validate_plan_for_runtime(run, plan)
        except Exception as exc:
            self._append(run.id, EventType.PLAN_REJECTED, {"reason": str(exc)})
            raise
        payload = {"plan": asdict(plan)}
        self._append(run.id, EventType.PLAN_PROPOSED, payload)
        self._append(run.id, EventType.PLAN_CREATED, payload)
        self._append(run.id, EventType.PLAN_ACCEPTED, {"plan_id": plan.id})
        return self._transition(self.get_run(run.id), RunState.READY)

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
        if invalidated_nodes:
            for node_id in self._dependent_closure(run, invalidated_nodes):
                status = self.get_run(run.id).node_status.get(node_id, NodeStatus.PENDING)
                if status != NodeStatus.INVALIDATED:
                    self._set_node(self.get_run(run.id), node_id, NodeStatus.INVALIDATED)
                    self._append(
                        run.id,
                        EventType.NODE_INVALIDATED,
                        {"node_id": node_id, "reason": reason},
                    )
        run = self.get_run(run.id)
        if run.state != RunState.PLANNING:
            run = self._transition(run, RunState.PLANNING)
        self._append(run.id, EventType.PLAN_REVISION_REQUESTED, {"reason": reason})
        plan = self.planner.revise_plan(run=run, reason=reason, version=run.plan_version + 1)
        self._validate_plan_for_runtime(run, plan)
        invalidated = invalidated_nodes or set()
        closure = self._dependent_closure(run, invalidated) if invalidated else set()
        preserved = sorted(
            node_id
            for node_id, status in run.node_status.items()
            if status == NodeStatus.COMPLETED
            and node_id not in closure
            and any(node.id == node_id for node in plan.nodes)
        )
        self._append(
            run.id,
            EventType.PLAN_REVISED,
            {"plan": asdict(plan), "preserved_completed": preserved, "reason": reason},
        )
        return self._transition(self.get_run(run.id), RunState.READY)

    def invalidate_and_replan(self, run_id: str, node_id: str, reason: str) -> Run:
        self._append(
            run_id,
            EventType.OBSERVATION_RECORDED,
            {
                "class": ObservationClass.ASSUMPTION_INVALIDATED.value,
                "node_id": node_id,
                "detail": reason,
            },
        )
        return self.request_replan(run_id, reason, invalidated_nodes={node_id})

    def _dependent_closure(self, run: Run, roots: set[str]) -> set[str]:
        if run.plan is None:
            return set(roots)
        result = set(roots)
        changed = True
        while changed:
            changed = False
            for node in run.plan.nodes:
                if node.id not in result and any(dep in result for dep in node.dependencies):
                    result.add(node.id)
                    changed = True
        return result

    def _intent_records(self, run_id: str) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for event in self._events(run_id):
            if event.type == EventType.ACTION_INTENT_CREATED:
                intent = dict(event.payload["intent"])
                records[str(intent["id"])] = {
                    "intent": intent,
                    "authorized": intent["authorization_state"] == AuthorizationState.NOT_REQUIRED.value,
                    "rejected": False,
                    "receipt": None,
                }
            elif event.type == EventType.ACTION_AUTHORIZED:
                record = records.get(str(event.payload["intent_id"]))
                if record is not None:
                    record["authorized"] = True
            elif event.type == EventType.ACTION_REJECTED:
                record = records.get(str(event.payload["intent_id"]))
                if record is not None:
                    record["rejected"] = True
            elif event.type == EventType.ACTION_RECEIPT_RECORDED:
                receipt = dict(event.payload["receipt"])
                record = records.get(str(receipt["intent_id"]))
                if record is not None:
                    record["receipt"] = receipt
        return records

    def _open_intent(self, run: Run, node_id: str) -> dict[str, Any] | None:
        for record in self._intent_records(run.id).values():
            intent = record["intent"]
            if (
                intent["node_id"] == node_id
                and int(intent["plan_version"]) == run.plan_version
                and record["receipt"] is None
                and not record["rejected"]
            ):
                return record
        return None

    def _create_intent(
        self,
        run: Run,
        *,
        node_id: str,
        capability_name: str,
        side_effect_class: SideEffectClass,
        reversible: bool,
        authorization: AuthorizationState,
    ) -> dict[str, Any]:
        if run.plan is None:
            raise RunBlocked("run has no plan")
        node = next(item for item in run.plan.nodes if item.id == node_id)
        intent = ActionIntent(
            id=new_id("intent"),
            run_id=run.id,
            node_id=node_id,
            plan_version=run.plan_version,
            capability=capability_name,
            arguments_digest=_json_digest(node.inputs),
            expected_effect=node.objective,
            side_effect_class=side_effect_class,
            authorization_state=authorization,
            reversible=reversible,
        )
        self._append(run.id, EventType.ACTION_INTENT_CREATED, {"intent": asdict(intent)})
        return self._intent_records(run.id)[intent.id]

    def _gate_for_intent(self, run_id: str, intent_id: str) -> dict[str, Any] | None:
        for gate in self.list_pending_gates(run_id):
            if gate.get("requested_action") == intent_id:
                return gate
        return None

    def _open_gate(self, run: Run, record: dict[str, Any], reason: str) -> Run:
        intent = record["intent"]
        existing = self._gate_for_intent(run.id, str(intent["id"]))
        if existing is None:
            gate = HumanGate(
                id=new_id("gate"),
                reason=reason,
                requested_action=str(intent["id"]),
                node_id=str(intent["node_id"]),
            )
            self._append(run.id, EventType.HUMAN_GATE_OPENED, {"gate": asdict(gate)})
        run = self._set_node(self.get_run(run.id), str(intent["node_id"]), NodeStatus.BLOCKED)
        run = self._transition(run, RunState.PAUSING)
        run = self._transition(run, RunState.PAUSED)
        self._append(run.id, EventType.RUN_PAUSED, {"reason": "human_gate"})
        return self.get_run(run.id)

    def _policy_decision(self, run: Run, node_id: str, provider: Any) -> PolicyDecision:
        descriptor = provider.descriptor
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
        return decision

    def execute_next(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state == RunState.CREATED:
            run = self.plan_run(run.id)
        if run.state == RunState.PAUSED:
            raise RunBlocked("run is paused")
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            return run
        if run.plan is None:
            run = self.plan_run(run.id)
        assert run.plan is not None
        ready = ready_node_ids(
            run.plan,
            {node_id: status.value for node_id, status in run.node_status.items()},
        )
        if not ready:
            if run.node_status and all(
                status == NodeStatus.COMPLETED for status in run.node_status.values()
            ):
                return self._complete(run)
            raise RunBlocked("no executable node is ready")
        node_id = ready[0]
        node = next(item for item in run.plan.nodes if item.id == node_id)
        capability_name = node.required_capabilities[0]
        try:
            provider = self.capabilities.resolve(
                capability_name,
                run.budget,
                side_effect_class=node.side_effect_class,
            )
        except CapabilityBudgetExceeded:
            self._append(
                run.id,
                EventType.OBSERVATION_RECORDED,
                {"class": ObservationClass.BUDGET_PRESSURE.value, "node_id": node_id},
            )
            raise RunBlocked("budget pressure: no eligible provider")
        descriptor = provider.descriptor
        decision = self._policy_decision(run, node_id, provider)
        if decision.justification_required and not node.justification:
            raise RunBlocked("policy requires node justification")
        if decision.required_verification and node.verification.kind.value not in set(
            decision.required_verification
        ):
            raise RunBlocked("plan does not satisfy policy-required verification")
        if decision.decision == PolicyDecisionType.DENY:
            raise RunBlocked(f"policy denied action: {decision.reason}")

        record: dict[str, Any] | None = None
        if is_side_effecting(descriptor.side_effect_class):
            record = self._open_intent(run, node_id)
            if record is None:
                auth = (
                    AuthorizationState.PENDING
                    if decision.decision == PolicyDecisionType.REQUIRE_HUMAN
                    else AuthorizationState.NOT_REQUIRED
                )
                record = self._create_intent(
                    run,
                    node_id=node_id,
                    capability_name=capability_name,
                    side_effect_class=descriptor.side_effect_class,
                    reversible=descriptor.reversible,
                    authorization=auth,
                )
            if decision.decision == PolicyDecisionType.REQUIRE_HUMAN and not record["authorized"]:
                return self._open_gate(self.get_run(run.id), record, decision.reason)
            if record["rejected"]:
                raise RunBlocked("action intent was rejected")

        run = self._transition(self.get_run(run.id), RunState.EXECUTING)
        run = self._set_node(run, node_id, NodeStatus.RUNNING)
        self._append(
            run.id,
            EventType.CAPABILITY_RESOLVED,
            {
                "node_id": node_id,
                "capability": capability_name,
                "provider_id": descriptor.provider_id,
            },
        )
        if record is not None:
            self._append(
                run.id,
                EventType.ACTION_STARTED,
                {"intent_id": record["intent"]["id"], "node_id": node_id},
            )
        try:
            result = provider.invoke(dict(node.inputs))
        except TransientCapabilityError as exc:
            if is_side_effecting(descriptor.side_effect_class) and not descriptor.idempotent:
                self._append(
                    run.id,
                    EventType.OBSERVATION_RECORDED,
                    {
                        "class": ObservationClass.UNKNOWN_FAILURE.value,
                        "node_id": node_id,
                        "detail": f"non-idempotent retry risk after invocation: {exc}",
                    },
                )
                self._set_node(self.get_run(run.id), node_id, NodeStatus.BLOCKED)
                return self.pause_run(run.id, reason="ambiguous side effect")
            count = run.retry_counts.get(node_id, 0) + 1
            self._append(
                run.id,
                EventType.OBSERVATION_RECORDED,
                {
                    "class": ObservationClass.TRANSIENT_FAILURE.value,
                    "node_id": node_id,
                    "detail": str(exc),
                },
            )
            if count > run.budget.limit.max_retries_per_node:
                return self._fail(
                    self.get_run(run.id),
                    FailureRecord(
                        FailureCategory.CAPABILITY_TIMEOUT,
                        "retry limit exceeded",
                        node_id=node_id,
                    ),
                )
            self._append(run.id, EventType.RETRY_RECORDED, {"node_id": node_id, "count": count})
            self._set_node(self.get_run(run.id), node_id, NodeStatus.PENDING)
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
                self._set_node(self.get_run(run.id), node_id, NodeStatus.BLOCKED)
                return self.pause_run(run.id, reason="ambiguous side effect")
            return self._fail(
                self.get_run(run.id),
                FailureRecord(FailureCategory.CAPABILITY_FAILURE, str(exc), node_id=node_id),
            )

        self._append(
            run.id,
            EventType.CAPABILITY_INVOKED,
            {"node_id": node_id, "provider_id": descriptor.provider_id, "output": result.output},
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
            return self._fail(
                self.get_run(run.id),
                FailureRecord(FailureCategory.BUDGET_EXHAUSTED, "hard budget exceeded"),
            )

        run = self._transition(self.get_run(run.id), RunState.VERIFYING)
        run = self._set_node(run, node_id, NodeStatus.VERIFYING)
        self._append(run.id, EventType.VERIFICATION_STARTED, {"node_id": node_id})
        verification = self.verifiers.verify(
            node.verification,
            result.output,
            {
                "run_id": run.id,
                "node_id": node_id,
                "provider_id": descriptor.provider_id,
                "intent_id": record["intent"]["id"] if record is not None else None,
            },
        )
        self._append(
            run.id,
            EventType.VERIFICATION_FINISHED,
            {"node_id": node_id, "result": asdict(verification)},
        )
        self._append(
            run.id,
            EventType.VERIFICATION_RECORDED,
            {"node_id": node_id, "result": asdict(verification)},
        )
        if verification.status == VerificationStatus.PASS:
            if record is not None:
                receipt = ActionReceipt(
                    id=new_id("receipt"),
                    intent_id=str(record["intent"]["id"]),
                    actual_effects=tuple(result.metadata.get("effects", (node.objective,))),
                    verification=verification,
                    rollback_ref=result.metadata.get("rollback_ref"),
                )
                self._append(
                    run.id,
                    EventType.ACTION_RECEIPT_RECORDED,
                    {"receipt": asdict(receipt)},
                )
            run = self._set_node(self.get_run(run.id), node_id, NodeStatus.COMPLETED)
            self._append(run.id, EventType.NODE_COMPLETED, {"node_id": node_id})
            run = self._transition(self.get_run(run.id), RunState.READY)
            if run.node_status and all(
                status == NodeStatus.COMPLETED for status in run.node_status.values()
            ):
                return self._complete(run)
            return run
        if verification.status in {VerificationStatus.BLOCKED, VerificationStatus.INCONCLUSIVE}:
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

    def _complete(self, run: Run) -> Run:
        if run.state != RunState.VERIFYING:
            run = self._transition(run, RunState.VERIFYING)
        self._append(run.id, EventType.RUN_COMPLETED, {"outcome": "success criteria satisfied"})
        return self.get_run(run.id)

    def run_until_blocked(self, run_id: str, *, max_steps: int = 100) -> Run:
        run = self.get_run(run_id)
        for _ in range(max_steps):
            if run.state in {
                RunState.PAUSED,
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                return run
            try:
                run = self.execute_next(run_id)
            except RunBlocked:
                return self.get_run(run_id)
        return self._fail(
            self.get_run(run_id),
            FailureRecord(
                FailureCategory.INTERNAL_INVARIANT_VIOLATION,
                "run step limit reached",
            ),
        )

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
        record = self._intent_records(run_id).get(intent_id)
        run = self.get_run(run_id)
        if record is None or int(record["intent"]["plan_version"]) != run.plan_version:
            raise RunBlocked("stale authorization request")
        self._append(
            run_id,
            EventType.HUMAN_GATE_RESOLVED,
            {"gate_id": gate_id, "status": "approved", "decided_by": decided_by},
        )
        self._append(run_id, EventType.ACTION_AUTHORIZED, {"intent_id": intent_id})
        node_id = str(gate.get("node_id") or record["intent"]["node_id"])
        current = self.get_run(run_id)
        if current.node_status.get(node_id) == NodeStatus.BLOCKED:
            self._set_node(current, node_id, NodeStatus.PENDING)
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
        return _json_digest(
            {
                "run_id": run.id,
                "state": run.state.value,
                "plan_version": run.plan_version,
                "node_status": {
                    node_id: status.value for node_id, status in sorted(run.node_status.items())
                },
                "budget": asdict(run.budget),
                "pending_action_intent_id": run.pending_action_intent_id,
            }
        )

    def pause_run(self, run_id: str, *, reason: str = "operator request") -> Run:
        run = self.get_run(run_id)
        if run.state == RunState.PAUSED:
            return run
        self._append(run.id, EventType.PAUSE_REQUESTED, {"reason": reason})
        run = self._transition(self.get_run(run.id), RunState.PAUSING)
        projected = self.get_run(run.id)
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
            "working_memory_snapshot_ref": None,
            "budget_state": asdict(projected.budget),
            "unresolved_action_intents": list(unresolved),
            "external_resource_versions": [],
            "created_at": now_iso(),
        }
        self._append(run.id, EventType.CHECKPOINT_CREATED, {"checkpoint": checkpoint})
        run = self._transition(self.get_run(run.id), RunState.PAUSED)
        self._append(run.id, EventType.RUN_PAUSED, {"reason": reason})
        return self.get_run(run.id)

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
            sequence = int(checkpoint["event_sequence"])
            projected = project_run(self._events(run_id)[:sequence])
            if projected is None:
                raise CheckpointInvalid("checkpoint references an empty projection")
            if self._checkpoint_digest(projected) != checkpoint["projected_state_digest"]:
                raise CheckpointInvalid("checkpoint digest does not match replayed state")
            unresolved = set(str(item) for item in checkpoint.get("unresolved_action_intents", []))
            records = self._intent_records(run_id)
            for intent_id in unresolved:
                record = records.get(intent_id)
                if record is None:
                    raise CheckpointInvalid("checkpoint references unknown action intent")
                if not record["authorized"] and not record["rejected"] and record["receipt"] is None:
                    raise RunBlocked("unsafe resume: unresolved side-effect intent")
                if record["authorized"] and record["receipt"] is None:
                    pending_gate = self._gate_for_intent(run_id, intent_id)
                    if pending_gate is None and record["intent"]["authorization_state"] != "pending":
                        raise RunBlocked("unsafe resume: ambiguous authorized side effect")
        self._append(run.id, EventType.RESUME_REQUESTED, {})
        target = RunState.READY if run.plan is not None else RunState.PLANNING
        run = self._transition(run, target)
        self._append(run.id, EventType.RUN_RESUMED, {"state": target.value})
        return self.get_run(run.id)

    def list_actions(self, run_id: str) -> list[dict[str, Any]]:
        records = self._intent_records(run_id)
        return [
            {"intent": record["intent"], "receipt": record["receipt"]}
            for record in records.values()
        ]

    def list_observations(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(event.payload)
            for event in self._events(run_id)
            if event.type == EventType.OBSERVATION_RECORDED
        ]

    def list_events(self, run_id: str) -> list[Event]:
        return self._events(run_id)

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

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        ready: list[str] = []
        if run.plan is not None:
            ready = ready_node_ids(
                run.plan,
                {node_id: status.value for node_id, status in run.node_status.items()},
            )
        return {
            "run": run.to_dict(),
            "ready_nodes": ready,
            "pending_gates": self.list_pending_gates(run_id),
            "actions": self.list_actions(run_id),
            "observations": self.list_observations(run_id)[-10:],
            "checkpoints": self.list_checkpoints(run_id),
            "event_count": len(self._events(run_id)),
        }

    def _fail(self, run: Run, failure: FailureRecord) -> Run:
        payload = {"failure": asdict(failure), "reason": failure.reason}
        if run.state not in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            run = self._transition(run, RunState.FAILED)
            self._append(run.id, EventType.RUN_FAILED, payload)
        return self.get_run(run.id)
