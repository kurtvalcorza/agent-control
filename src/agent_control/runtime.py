"""Public control-plane runtime facade."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, replace
from typing import Any

from .audit import redact_for_audit, validate_plan_secrets
from .capabilities import CapabilityRegistry
from .compensation import Compensator
from .completion import require_goal_coverage
from .controller import ControlPlane as _ControlPlane
from .controller import RunBlocked, RunNotFound
from .errors import CheckpointInvalid, FailureCategory, FailureRecord
from .events import Event, EventType
from .ids import new_id
from .memory import WorkingMemory
from .models import (
    ActionReceipt,
    AuthorizationState,
    BudgetReservation,
    NodeStatus,
    ObservationClass,
    Plan,
    PlanNode,
    Run,
    RunState,
    VerificationKind,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
    is_side_effecting,
)
from .planner import Planner
from .plans import ready_node_ids
from .policy import PolicyDecision, PolicyEngine
from .projections import now_iso, project_run
from .provenance import (
    CapabilityContract,
    CapabilityPreResolution,
    CapabilityPreResolutionState,
    CapabilitySourceRegistry,
    DelegationRoute,
    ExternalObservation,
    ExternalObservationResolver,
    ExternalStateObserver,
    ObservationResolution,
    ResolvedCapabilityProvenance,
    RuntimeRequirement,
    SourceCheckStatus,
    check_capability_source,
    pre_resolve,
)
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
        capability_sources: CapabilitySourceRegistry | None = None,
        runtime_environment: Iterable[RuntimeRequirement] = (),
        external_observer: ExternalStateObserver | None = None,
        observation_resolver: ExternalObservationResolver | None = None,
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
        self.capability_sources = capability_sources
        # Fail-closed default: a host that declares nothing provides no runtime facility.
        self.runtime_environment = frozenset(
            RuntimeRequirement(item) for item in runtime_environment
        )
        self.external_observer = external_observer
        # RFC #6 Scenario P: a separate host resolver re-fetches reference-only observations.
        self.observation_resolver = observation_resolver

    def _append(self, run_id: str, kind: EventType, payload: dict[str, Any]) -> Event:
        if kind == EventType.CAPABILITY_RESOLVED:
            payload = self._with_resolution_provenance(run_id, payload)
        elif kind == EventType.ACTION_INTENT_CREATED and "intent" in payload:
            intent = dict(payload["intent"])
            intent["delegated"] = bool(intent.get("delegated")) or self._node_is_delegated(
                run_id, str(intent["node_id"]), int(intent["plan_version"])
            )
            payload = {**payload, "intent": intent}
        return super()._append(run_id, kind, redact_for_audit(payload))

    # --- RFC #6 Phase A: provenance and pre-resolution -------------------------------------

    def _pre_resolution_record(
        self, run_id: str, node_id: str, plan_version: int
    ) -> dict[str, Any] | None:
        for event in reversed(self._events(run_id)):
            if (
                event.type == EventType.CAPABILITY_PRE_RESOLVED
                and event.payload.get("node_id") == node_id
                and int(event.payload.get("plan_version", -1)) == plan_version
            ):
                return dict(event.payload)
        return None

    def _node_is_delegated(self, run_id: str, node_id: str, plan_version: int) -> bool:
        record = self._pre_resolution_record(run_id, node_id, plan_version)
        return bool(
            record is not None
            and record["pre_resolution"]["state"]
            == CapabilityPreResolutionState.DELEGATION_REQUIRED.value
        )

    def _node_is_actor_routed(self, run_id: str, node_id: str, plan_version: int) -> bool:
        """True when this node's recorded pre-resolution took the actor route (RFC #6 B3)."""

        record = self._pre_resolution_record(run_id, node_id, plan_version)
        return bool(
            record is not None
            and record["pre_resolution"]["state"]
            == CapabilityPreResolutionState.DELEGATION_REQUIRED.value
            and record.get("delegation_route") == DelegationRoute.ACTOR.value
        )

    def _with_resolution_provenance(
        self, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        enriched = dict(payload)
        record = None
        node_id = payload.get("node_id")
        if node_id is not None:
            run = self.get_run(run_id)
            record = self._pre_resolution_record(run_id, str(node_id), run.plan_version)
        enriched["source_provenance"] = None if record is None else record["provenance"]
        enriched["pre_resolution_state"] = (
            None if record is None else record["pre_resolution"]["state"]
        )
        return enriched

    def _recorded_capability_sources(
        self, run_id: str, *, plan_version: int | None = None
    ) -> dict[str, dict[str, Any]]:
        """Latest recorded provenance per capability, optionally for one plan version."""

        recorded: dict[str, dict[str, Any]] = {}
        for event in self._events(run_id):
            if event.type != EventType.CAPABILITY_PRE_RESOLVED:
                continue
            if plan_version is not None and int(event.payload["plan_version"]) != plan_version:
                continue
            provenance = dict(event.payload["provenance"])
            recorded[str(provenance["capability"])] = provenance
        return recorded

    def _load_capability_contract(self, run: Run, node: PlanNode) -> CapabilityContract | None:
        capability = node.required_capabilities[0]
        source = (
            self.capability_sources.get(capability)
            if self.capability_sources is not None
            else None
        )
        if source is None:
            return None
        try:
            contract = source.load(capability)
            if not isinstance(contract, CapabilityContract) or contract.capability != capability:
                raise ValueError("source adapter returned a contract for another capability")
        except Exception as exc:
            self._append(
                run.id,
                EventType.OBSERVATION_RECORDED,
                {
                    "class": ObservationClass.CAPABILITY_FAILURE.value,
                    "node_id": node.id,
                    "detail": f"capability source could not be loaded: {exc}",
                },
            )
            raise RunBlocked(f"capability source could not be loaded: {capability}") from exc
        return contract

    def _block_node_before_invocation(self, run: Run, node: PlanNode, detail: str) -> None:
        self._append(
            run.id,
            EventType.OBSERVATION_RECORDED,
            {
                "class": ObservationClass.POLICY_BLOCK.value,
                "node_id": node.id,
                "detail": detail,
            },
        )
        current = self.get_run(run.id)
        if current.node_status.get(node.id) in {NodeStatus.PENDING, NodeStatus.READY}:
            self._set_node(current, node.id, NodeStatus.BLOCKED)
        raise RunBlocked(detail)

    def _pre_resolve_node(self, run: Run, node: PlanNode) -> Run | None:
        """Pre-resolution precondition to ``CapabilityRegistry.resolve()``.

        Returns a failed run for ``UNSATISFIED``; raises ``RunBlocked`` for drift, unsatisfied
        clause dependencies or an unverifiable delegated commit; returns ``None`` to continue.
        """

        capability = node.required_capabilities[0]
        required_clauses = node.verification.requires_clause_ids
        contract = self._load_capability_contract(run, node)
        if contract is None:
            if required_clauses:
                self._block_node_before_invocation(
                    run,
                    node,
                    "verification requires clauses an unsourced capability cannot satisfy: "
                    + ", ".join(sorted(set(required_clauses))),
                )
            return None

        provenance = contract.provenance
        prior = self._recorded_capability_sources(run.id, plan_version=run.plan_version).get(
            capability
        )
        if prior is not None and not ResolvedCapabilityProvenance.from_dict(prior).same_identity(
            provenance
        ):
            self._append(
                run.id,
                EventType.OBSERVATION_RECORDED,
                {
                    "class": ObservationClass.INPUT_CHANGED.value,
                    "node_id": node.id,
                    "detail": "capability source changed during run",
                    "capabilities": [capability],
                    "recorded": prior,
                    "current": provenance.to_dict(),
                },
            )
            raise RunBlocked("capability source changed during run; replan required")

        resolution: CapabilityPreResolution = pre_resolve(contract, self.runtime_environment)
        delegated = resolution.state == CapabilityPreResolutionState.DELEGATION_REQUIRED
        self._append(
            run.id,
            EventType.CAPABILITY_PRE_RESOLVED,
            {
                "node_id": node.id,
                "plan_version": run.plan_version,
                "capability": capability,
                "provenance": provenance.to_dict(),
                "pre_resolution": resolution.to_dict(),
                "required_clause_ids": list(required_clauses),
                # RFC #6 B3: which route resolves the delegation; null when none is required.
                "delegation_route": contract.delegation_route.value if delegated else None,
            },
        )
        if resolution.state == CapabilityPreResolutionState.UNSATISFIED:
            return self._fail(
                self.get_run(run.id),
                FailureRecord(
                    FailureCategory.CAPABILITY_UNAVAILABLE,
                    f"capability pre-resolution unsatisfied: {resolution.reason}",
                    node_id=node.id,
                ),
            )
        missing_clauses = resolution.unsatisfied_clause_ids(required_clauses)
        if missing_clauses:
            self._block_node_before_invocation(
                run,
                node,
                "verification requires unsatisfied capability clauses: "
                + ", ".join(missing_clauses),
            )
        if (
            resolution.state == CapabilityPreResolutionState.DELEGATION_REQUIRED
            and is_side_effecting(node.side_effect_class)
            and VerificationKind.EXTERNAL_STATE not in node.verification.kinds
        ):
            self._block_node_before_invocation(
                run,
                node,
                "delegated commit requires independent external_state verification",
            )
        if delegated and contract.delegation_route == DelegationRoute.ACTOR:
            kinds = node.verification.kinds
            if VerificationKind.EXTERNAL_STATE not in kinds:
                self._block_node_before_invocation(
                    run,
                    node,
                    "actor-routed delegation requires independent external_state verification",
                )
            others = sorted(kind.value for kind in kinds if kind != VerificationKind.EXTERNAL_STATE)
            if others:
                # The actor route captures no executor output, so these kinds have no subject.
                self._block_node_before_invocation(
                    run,
                    node,
                    "actor-routed delegation has no executor output to verify: "
                    + ", ".join(others),
                )
        return None

    def _plan_time_actor_routed(self, node: PlanNode) -> bool:
        """Plan-time view of the B3 actor route; it emits no events and leans to providers.

        A source that cannot be loaded here counts as provider-routed, so plan validation keeps
        requiring a provider and execution surfaces the load failure as before.
        """

        capability = node.required_capabilities[0]
        source = (
            self.capability_sources.get(capability)
            if self.capability_sources is not None
            else None
        )
        if source is None:
            return False
        try:
            contract = source.load(capability)
        except Exception:
            return False
        if not isinstance(contract, CapabilityContract) or contract.capability != capability:
            return False
        return (
            contract.delegation_route == DelegationRoute.ACTOR
            and pre_resolve(contract, self.runtime_environment).state
            == CapabilityPreResolutionState.DELEGATION_REQUIRED
        )

    def _node_needs_provider(self, node: PlanNode) -> bool:
        return not self._plan_time_actor_routed(node)

    def _execute_actor_route(self, run: Run, node: PlanNode) -> Run:
        """RFC #6 s12 / Scenario L: delegation that ends at an actor/``HumanGate`` boundary.

        The dispatching run records the ``ActionIntent`` first, then opens a gate and pauses for
        the other agent or session. Nothing is resolved, invoked or budgeted, and no relay
        provider exists. Approving the gate records that the handoff returned; the action is then
        marked started and verified only through the independent ``EXTERNAL_STATE`` path (s13),
        so an approval alone never mints a PASS receipt.
        """

        capability = node.required_capabilities[0]
        record = self._open_intent(run, node.id)
        if record is None:
            # No provider descriptor exists on this route: the intent takes the node's declared
            # side-effect class and is conservatively irreversible (never auto-compensated).
            record = self._create_intent(
                run,
                node_id=node.id,
                capability_name=capability,
                side_effect_class=node.side_effect_class,
                reversible=False,
                authorization=AuthorizationState.PENDING,
            )
        if not record["authorized"]:
            return self._open_gate(
                self.get_run(run.id),
                record,
                f"actor delegation: hand off {capability!r} to another agent or session; "
                "approve when the handoff returns (a PASS receipt still needs independent "
                "external-state evidence)",
            )
        intent_id = str(record["intent"]["id"])
        current = self._transition(self.get_run(run.id), RunState.EXECUTING)
        current = self._set_node(current, node.id, NodeStatus.RUNNING)
        self._append(
            current.id, EventType.ACTION_STARTED, {"intent_id": intent_id, "node_id": node.id}
        )
        current = self._transition(self.get_run(current.id), RunState.VERIFYING)
        current = self._set_node(current, node.id, NodeStatus.VERIFYING)
        self._append(current.id, EventType.VERIFICATION_STARTED, {"node_id": node.id})
        verification = self._verify_node_output(
            current,
            node,
            None,
            {"run_id": current.id, "node_id": node.id, "intent_id": intent_id},
        )
        return self._record_verification_outcome(
            current,
            node,
            record,
            verification,
            effects=(node.objective,),
            rollback_ref=None,
        )

    def _independent_external_verification(
        self, spec: VerificationSpec, context: dict[str, Any]
    ) -> VerificationResult:
        """EXTERNAL_STATE verification over an observation the dispatching run acquired itself.

        The delegated executor's output is never the subject; it is not even in ``context``.
        """

        if self.external_observer is None:
            return VerificationResult(
                VerificationStatus.BLOCKED,
                "no independent external-state observer is registered",
                {"independent_observation": None},
            )
        try:
            observation = self.external_observer.observe(dict(context))
        except Exception as exc:
            return VerificationResult(
                VerificationStatus.BLOCKED,
                f"independent external-state observation failed: {exc}",
                {"independent_observation": None},
            )
        if not isinstance(observation, ExternalObservation):
            return VerificationResult(
                VerificationStatus.BLOCKED,
                "no independent external-state observation is available",
                {"independent_observation": None},
            )
        resolution: ObservationResolution | None = None
        if observation.state is None:
            resolved = self._resolve_observation_reference(observation)
            if isinstance(resolved, VerificationResult):
                return resolved
            resolution = resolved
            observation = replace(observation, state=resolution.state)
        result = self.verifiers.verify(
            VerificationSpec(
                VerificationKind.EXTERNAL_STATE, criteria=spec.criteria, required=spec.required
            ),
            observation,
            {**context, "independent_observation": True},
        )
        evidence = dict(result.evidence)
        evidence["independent_observation"] = observation.reference()
        if resolution is not None:
            evidence["observation_resolution"] = resolution.to_dict()
        return VerificationResult(result.status, result.message, evidence)

    def _resolve_observation_reference(
        self, observation: ExternalObservation
    ) -> ObservationResolution | VerificationResult:
        """Re-fetch a reference-only observation through the host resolver (RFC #6 Scenario P).

        The resolver receives only the reference, never run context or executor output. Every
        failure to produce state is ``BLOCKED`` before any verifier sees the observation.
        """

        reference = observation.reference()

        def blocked(message: str) -> VerificationResult:
            return VerificationResult(
                VerificationStatus.BLOCKED,
                message,
                {"independent_observation": reference, "observation_resolution": None},
            )

        resolver = self.observation_resolver
        if resolver is None:
            return blocked(
                "reference-only external observation and no external observation resolver "
                "is registered"
            )
        observer: object = self.external_observer
        if resolver is observer:
            # D3 tripwire: catches accidental reuse of the observer instance. It is not proof of
            # independence (a wrapper bypasses it); independence rests on the resolver receiving
            # only the reference.
            return blocked("external observation resolver must be separate from the observer")
        try:
            resolution = resolver.resolve(dict(reference))
        except Exception as exc:
            return blocked(f"external observation reference resolution failed: {exc}")
        if not isinstance(resolution, ObservationResolution) or resolution.state is None:
            return blocked("external observation reference has no resolved state")
        return resolution

    def _verify_node_output(
        self,
        run: Run,
        node: PlanNode,
        output: Any,
        context: dict[str, Any],
    ) -> VerificationResult:
        if VerificationKind.EXTERNAL_STATE not in node.verification.kinds or (
            not self._node_is_delegated(run.id, node.id, run.plan_version)
        ):
            return super()._verify_node_output(run, node, output, context)
        others = tuple(
            kind for kind in node.verification.kinds if kind != VerificationKind.EXTERNAL_STATE
        )
        results: dict[str, VerificationResult] = {}
        if others:
            results["executor_output"] = self.verifiers.verify(
                VerificationSpec(
                    others[0],
                    criteria=node.verification.criteria,
                    required=node.verification.required,
                    additional_kinds=others[1:],
                ),
                output,
                context,
            )
        results[VerificationKind.EXTERNAL_STATE.value] = self._independent_external_verification(
            node.verification, context
        )
        statuses = {item.status for item in results.values()}
        aggregate = next(
            status for status in VerifierRegistry._STATUS_PRECEDENCE if status in statuses
        )
        message = (
            "delegated commit verified by independent external-state evidence"
            if aggregate == VerificationStatus.PASS
            else "delegated commit lacks passing independent evidence: "
            + ", ".join(
                f"{name}={item.status.value}"
                for name, item in results.items()
                if item.status != VerificationStatus.PASS
            )
        )
        return VerificationResult(
            aggregate,
            message,
            {
                "delegated_commit": True,
                "verifiers": {name: asdict(item) for name, item in results.items()},
            },
        )

    def _validate_plan_for_runtime(self, run: Run, plan: Plan) -> None:
        validate_plan_secrets(plan)
        super()._validate_plan_for_runtime(run, plan)
        require_goal_coverage(run.goal, plan)
        for node in plan.nodes:
            if not self._node_needs_provider(node):
                continue
            self.capabilities.resolve(
                node.required_capabilities[0],
                run.budget,
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
                "policy_version": decision.policy_version,
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
            {"plan": asdict(plan), "preserved_completed": preserved, "reason": reason},
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
        failed = self._pre_resolve_node(run, node)
        if failed is not None:
            return failed
        if self._node_is_actor_routed(run.id, node.id, run.plan_version):
            return self._execute_actor_route(self.get_run(run.id), node)
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
                    {"reservation_id": reservation.id, "reason": "execution did not start"},
                )
            raise
        new_events = [
            event for event in self._events(run.id) if event.sequence > reserved_event.sequence
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
        transient_retry = any(
            event.type == EventType.OBSERVATION_RECORDED
            and event.payload.get("node_id") == node.id
            and event.payload.get("class") == "transient_failure"
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
        elif started and transient_retry and descriptor.idempotent:
            self.resolve_budget_reservation(
                run.id,
                reservation.id,
                consumed=True,
                actual_cost_usd=descriptor.estimated_cost_usd,
                elapsed_ms=descriptor.estimated_elapsed_ms,
                model_calls=descriptor.estimated_model_calls,
                tool_calls=descriptor.estimated_tool_calls,
                reason="conservative accounting for idempotent failed attempt",
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
            "capability_source_versions": list(
                self._recorded_capability_sources(run.id).values()
            ),
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
        if intent.get("delegated"):
            verification = self._reconcile_delegated(run_id, intent, occurred=occurred)
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

    def _reconcile_delegated(
        self, run_id: str, intent: dict[str, Any], *, occurred: bool
    ) -> VerificationResult:
        """Operator knowledge alone never mints an authoritative delegated PASS (RFC #6 s14)."""

        run = self.get_run(run_id)
        node_id = str(intent["node_id"])
        node = (
            next((item for item in run.plan.nodes if item.id == node_id), None)
            if run.plan is not None
            else None
        )
        spec = (
            node.verification
            if node is not None
            else VerificationSpec(VerificationKind.EXTERNAL_STATE)
        )
        external = self._independent_external_verification(
            spec,
            {
                "run_id": run_id,
                "node_id": node_id,
                "intent_id": intent["id"],
                "reconciliation": True,
            },
        )
        # The EXTERNAL_STATE contract: PASS = the expected effect is observed, FAIL = it is
        # observed absent. Anything else is no evidence either way.
        if external.status in {VerificationStatus.PASS, VerificationStatus.FAIL}:
            observed = external.status == VerificationStatus.PASS
            if observed != occurred:
                self._append(
                    run_id,
                    EventType.OBSERVATION_RECORDED,
                    {
                        "class": ObservationClass.VERIFICATION_FAILURE.value,
                        "node_id": node_id,
                        "intent_id": intent["id"],
                        "detail": "operator reconciliation contradicts independent "
                        "external-state evidence",
                        "operator_acknowledged": True,
                        "operator_claim_occurred": occurred,
                        "effect_observed": observed,
                        "external_state": asdict(external),
                    },
                )
                raise RunBlocked(
                    "operator reconciliation contradicts independent external-state evidence"
                )
            return VerificationResult(
                VerificationStatus.PASS,
                "operator reconciliation confirmed by independent external-state evidence",
                {
                    "operator_reconciled": True,
                    "occurred": occurred,
                    "effect_observed": observed,
                    "delegated_commit": True,
                    "external_state": asdict(external),
                },
            )
        self._append(
            run_id,
            EventType.OBSERVATION_RECORDED,
            {
                "class": ObservationClass.VERIFICATION_FAILURE.value,
                "node_id": node_id,
                "intent_id": intent["id"],
                "detail": "delegated reconciliation acknowledged without independent "
                "external-state evidence",
                "operator_acknowledged": True,
                "operator_claim_occurred": occurred,
                "external_state": asdict(external),
            },
        )
        raise RunBlocked(
            "delegated action requires independent external-state evidence to reconcile"
        )

    def _revalidate_capability_sources(
        self, run: Run, checkpoint: dict[str, Any] | None
    ) -> None:
        """Unconditional resume-time source revalidation; no host configuration disables it.

        Runs on every resume, with or without a checkpoint (a human-gate pause writes none), over
        the union of the provenance the run recorded and the latest checkpoint's records.
        """

        raw = list(checkpoint.get("capability_source_versions", [])) if checkpoint else []
        try:
            from_checkpoint = [ResolvedCapabilityProvenance.from_dict(dict(item)) for item in raw]
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointInvalid("checkpoint capability provenance is malformed") from exc
        from_history = [
            ResolvedCapabilityProvenance.from_dict(item)
            for item in self._recorded_capability_sources(run.id).values()
        ]
        recorded: list[ResolvedCapabilityProvenance] = []
        for candidate in (*from_checkpoint, *from_history):
            if not any(candidate.same_identity(item) for item in recorded):
                recorded.append(candidate)
        changed: list[str] = []
        unavailable: list[str] = []
        for item in recorded:
            source = (
                self.capability_sources.get(item.capability)
                if self.capability_sources is not None
                else None
            )
            result = check_capability_source(source, item)
            self._append(
                run.id,
                EventType.CAPABILITY_SOURCE_CHECKED,
                {"capability": item.capability, "recorded": item.to_dict(), **result.to_dict()},
            )
            if result.status == SourceCheckStatus.MISMATCH:
                changed.append(item.capability)
            elif result.status != SourceCheckStatus.MATCH:
                unavailable.append(item.capability)
        if changed:
            self._append(
                run.id,
                EventType.OBSERVATION_RECORDED,
                {
                    "class": ObservationClass.INPUT_CHANGED.value,
                    "detail": "capability source changed while paused",
                    "capabilities": sorted(changed),
                },
            )
            raise RunBlocked("capability source changed while paused; replan required")
        if unavailable:
            raise RunBlocked(
                "capability source cannot be revalidated: " + ", ".join(sorted(unavailable))
            )

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
                str(item) for item in checkpoint.get("external_resource_versions", [])
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
                    raise RunBlocked("external resource versions changed; replan required")
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
        self._revalidate_capability_sources(run, checkpoints[-1] if checkpoints else None)
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
            self.working_memory.snapshot_ref() if self.working_memory is not None else None
        )
        result["capability_health"] = self.capabilities.health()
        events = self._events(run_id)
        result["capability_pre_resolutions"] = [
            dict(event.payload)
            for event in events
            if event.type == EventType.CAPABILITY_PRE_RESOLVED
        ]
        result["capability_source_checks"] = [
            dict(event.payload)
            for event in events
            if event.type == EventType.CAPABILITY_SOURCE_CHECKED
        ]
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
