"""RFC #6 Phase B3: delegation that ends at an actor/HumanGate boundary (Scenario L)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_control import (
    CallbackCapabilitySource,
    CallbackExternalStateObserver,
    CapabilityContract,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilitySourceRegistry,
    ControlPlane,
    DelegationRoute,
    EventType,
    ExternalObservation,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    ResolvedCapabilityProvenance,
    RunBlocked,
    RunState,
    RuntimeRequirement,
    SideEffectClass,
    SourceCheckResult,
    SourceCheckStatus,
    SQLiteEventStore,
    VerificationKind,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
    VerifierRegistry,
    canonical_digest,
)
from agent_control.models import NodeStatus, RiskLevel
from agent_control.policy import PolicyDecisionType, PolicyEngine, PolicyRule

PROVENANCE = ResolvedCapabilityProvenance(
    capability="write", source_ref="git:example/skills#write", digest=canonical_digest(b"w")
)
ACTOR = CapabilityContract(
    provenance=PROVENANCE,
    runtime_requirements=frozenset({RuntimeRequirement.GPU}),
    delegation_permitted=True,
    delegation_route=DelegationRoute.ACTOR,
)
PROVIDER_DELEGATION = CapabilityContract(
    provenance=PROVENANCE,
    runtime_requirements=frozenset({RuntimeRequirement.GPU}),
    delegation_permitted=True,
)
EXTERNAL_ONLY = VerificationSpec(VerificationKind.EXTERNAL_STATE)
MIXED = VerificationSpec(
    VerificationKind.DETERMINISTIC, additional_kinds=(VerificationKind.EXTERNAL_STATE,)
)


class Planner:
    def __init__(self, verification: VerificationSpec, side_effect: SideEffectClass) -> None:
        self.verification = verification
        self.side_effect = side_effect

    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode(
                    "write",
                    "record 42 exists",
                    ("write",),
                    side_effect_class=self.side_effect,
                    verification=self.verification,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


class TrippedRegistry(CapabilityRegistry):
    """A registry whose ``resolve()`` fails the test if the actor route ever reaches it."""

    def __init__(self, calls: list[str], *, trip: bool) -> None:
        super().__init__()
        self.calls = calls
        self.trip = trip

    def resolve(self, name: str, budget: Any, **kwargs: Any) -> Any:
        self.calls.append(f"resolve:{name}")
        if self.trip:
            raise AssertionError("actor route called CapabilityRegistry.resolve()")
        return super().resolve(name, budget, **kwargs)


def _sources(contract: CapabilityContract) -> CapabilitySourceRegistry:
    sources = CapabilitySourceRegistry()
    sources.register(
        "write",
        CallbackCapabilitySource(
            lambda _: contract,
            lambda recorded: SourceCheckResult(
                SourceCheckStatus.MATCH,
                current=ResolvedCapabilityProvenance.from_dict(contract.provenance.to_dict()),
            ),
        ),
    )
    return sources


def _observer(state: Any, contexts: list[dict] | None = None) -> CallbackExternalStateObserver:
    def observe(context: dict) -> ExternalObservation:
        if contexts is not None:
            contexts.append(dict(context))
        return ExternalObservation(
            ref="crm:record/42@etag-7",
            observer="dispatching-run",
            observed_at="2026-11-19T00:00:00Z",
            state=state,
        )

    return CallbackExternalStateObserver(observe)


def _runtime(
    calls: list[str],
    *,
    contract: CapabilityContract = ACTOR,
    verification: VerificationSpec = EXTERNAL_ONLY,
    side_effect: SideEffectClass = SideEffectClass.REVERSIBLE_WRITE,
    observer: Any = None,
    trip: bool = True,
    register_provider: bool = True,
    runtime_environment: frozenset[RuntimeRequirement] = frozenset(),
    store: SQLiteEventStore | None = None,
    policy: PolicyEngine | None = None,
) -> ControlPlane:
    registry = TrippedRegistry(calls, trip=trip)
    if register_provider:
        # A provider is available, so a route that reached it would have something to call.

        def handler(_: dict) -> dict:
            calls.append("invoke")
            if trip:
                raise AssertionError("actor route invoked a capability provider")
            return {"done": True}

        registry.register(
            InProcessCapability(
                CapabilityDescriptor(
                    "write",
                    "1",
                    "local-writer",
                    side_effect_class=side_effect,
                    reversible=True,
                ),
                handler,
            )
        )
    verifiers = VerifierRegistry()

    def external_state(spec: VerificationSpec, subject: Any, context: dict) -> VerificationResult:
        if isinstance(subject, ExternalObservation) and subject.state == {"exists": True}:
            return VerificationResult(VerificationStatus.PASS, "record observed")
        return VerificationResult(VerificationStatus.FAIL, "record not observed")

    verifiers.register(VerificationKind.EXTERNAL_STATE, external_state)
    return ControlPlane(
        store=store if store is not None else SQLiteEventStore(":memory:"),
        planner=Planner(verification, side_effect),
        capabilities=registry,
        policy=policy,
        verifiers=verifiers,
        capability_sources=_sources(contract),
        runtime_environment=runtime_environment,
        external_observer=observer,
    )


def _types(runtime: ControlPlane, run_id: str) -> list[EventType]:
    return [event.type for event in runtime.list_events(run_id)]


def _pre_resolutions(runtime: ControlPlane, run_id: str) -> list[dict]:
    return [
        e.payload
        for e in runtime.list_events(run_id)
        if e.type == EventType.CAPABILITY_PRE_RESOLVED
    ]


def _receipts(runtime: ControlPlane, run_id: str) -> list[dict]:
    return [a["receipt"] for a in runtime.list_actions(run_id) if a["receipt"] is not None]


def _intent_id(runtime: ControlPlane, run_id: str) -> str:
    (action,) = runtime.list_actions(run_id)
    return str(action["intent"]["id"])


def _dispatch_and_return(runtime: ControlPlane, run_id: str, gate: dict) -> Any:
    """Approve (dispatch) the actor gate, then record that the handoff returned."""

    dispatched = runtime.approve_gate(run_id, gate["id"])
    assert dispatched.state == RunState.PAUSED  # approval dispatches; it does not verify
    return runtime.complete_actor_handoff(run_id, _intent_id(runtime, run_id))


def _gate_paused(runtime: ControlPlane) -> tuple[str, dict]:
    run = runtime.create_run(Goal("delegate", ("done",)))
    paused = runtime.run_until_blocked(run.id)
    assert paused.state == RunState.PAUSED
    (gate,) = runtime.list_pending_gates(run.id)
    return run.id, gate


# --- the route marker --------------------------------------------------------------------


def test_route_marker_defaults_to_provider_and_has_no_relay_value() -> None:
    assert CapabilityContract(provenance=PROVENANCE).delegation_route == DelegationRoute.PROVIDER
    assert {item.value for item in DelegationRoute} == {"provider", "actor"}
    with pytest.raises(ValueError, match="delegation_permitted"):
        CapabilityContract(provenance=PROVENANCE, delegation_route=DelegationRoute.ACTOR)
    coerced = CapabilityContract(
        provenance=PROVENANCE, delegation_permitted=True, delegation_route="actor"  # type: ignore[arg-type]
    )
    assert coerced.delegation_route is DelegationRoute.ACTOR


# --- Scenario L --------------------------------------------------------------------------


def test_scenario_l_actor_route_records_intent_opens_gate_and_pauses() -> None:
    calls: list[str] = []
    runtime = _runtime(calls)
    run_id, gate = _gate_paused(runtime)

    assert calls == []  # neither resolve() nor the provider's invoke() was reached
    run = runtime.get_run(run_id)
    assert run.node_status["write"] == NodeStatus.BLOCKED
    events = runtime.list_events(run_id)
    created = next(e for e in events if e.type == EventType.ACTION_INTENT_CREATED)
    opened = next(e for e in events if e.type == EventType.HUMAN_GATE_OPENED)
    assert created.sequence < opened.sequence
    intent = created.payload["intent"]
    assert intent["delegated"] is True
    assert intent["authorization_state"] == "pending"
    assert intent["reversible"] is False
    assert gate["requested_action"] == intent["id"]
    assert "actor delegation" in gate["reason"]
    pre = next(e for e in events if e.type == EventType.CAPABILITY_PRE_RESOLVED)
    assert pre.payload["pre_resolution"]["state"] == "delegation_required"
    assert pre.payload["delegation_route"] == "actor"
    types = _types(runtime, run_id)
    for absent in (
        EventType.CAPABILITY_RESOLVED,
        EventType.CAPABILITY_INVOKED,
        EventType.BUDGET_RESERVED,
        EventType.ACTION_STARTED,
    ):
        assert absent not in types
    # Policy is evaluated on node-level inputs before the intent and the gate (B3-R2).
    policy = next(e for e in events if e.type == EventType.POLICY_EVALUATED)
    assert policy.sequence < created.sequence
    assert intent["delegation_route"] == "actor"
    providers = runtime.capabilities.list()
    assert not any("relay" in d.name or "relay" in d.provider_id for d in providers)


def test_scenario_l_actor_route_needs_no_registered_provider() -> None:
    calls: list[str] = []
    runtime = _runtime(calls, register_provider=False, observer=_observer({"exists": True}))
    run_id, gate = _gate_paused(runtime)
    assert _dispatch_and_return(runtime, run_id, gate).state == RunState.COMPLETED
    assert calls == []
    assert runtime.capabilities.list() == ()


# --- approval needs s13 evidence ---------------------------------------------------------


def _raising_observer(context: dict) -> ExternalObservation | None:
    raise ConnectionError("observer offline")


@pytest.mark.parametrize(
    "observer",
    [None, CallbackExternalStateObserver(lambda _: None), CallbackExternalStateObserver(
        _raising_observer
    )],
    ids=["no-observer", "no-observation", "observer-error"],
)
def test_actor_approval_without_evidence_writes_no_pass_receipt(observer: Any) -> None:
    calls: list[str] = []
    runtime = _runtime(calls, observer=observer)
    run_id, gate = _gate_paused(runtime)
    paused = _dispatch_and_return(runtime, run_id, gate)

    assert calls == []
    assert paused.state == RunState.PAUSED
    assert paused.node_status["write"] == NodeStatus.BLOCKED
    assert _receipts(runtime, run_id) == []
    recorded = [
        e.payload["result"]
        for e in runtime.list_events(run_id)
        if e.type == EventType.VERIFICATION_RECORDED
    ]
    assert recorded and recorded[-1]["status"] == "blocked"
    assert recorded[-1]["evidence"]["delegated_commit"] is True
    # The started action is ambiguous until independent evidence arrives.
    with pytest.raises(RunBlocked, match="started side effect"):
        runtime.resume_run(run_id)


def test_actor_reconcile_without_evidence_writes_no_receipt_then_evidence_passes() -> None:
    """The s14 reading: a delegated reconcile without evidence writes no receipt at all."""

    calls: list[str] = []
    runtime = _runtime(calls)
    run_id, gate = _gate_paused(runtime)
    _dispatch_and_return(runtime, run_id, gate)
    intent_id = _intent_id(runtime, run_id)

    with pytest.raises(RunBlocked, match="independent external-state evidence"):
        runtime.reconcile_action(run_id, intent_id, occurred=True)
    assert _receipts(runtime, run_id) == []

    runtime.external_observer = _observer({"exists": True})
    reconciled = runtime.reconcile_action(run_id, intent_id, occurred=True)
    assert reconciled.node_status["write"] == NodeStatus.COMPLETED
    (receipt,) = _receipts(runtime, run_id)
    assert receipt["verification"]["status"] == "pass"
    assert receipt["verification"]["evidence"]["delegated_commit"] is True
    assert calls == []


def test_actor_approval_with_independent_evidence_mints_pass_receipt() -> None:
    calls: list[str] = []
    contexts: list[dict] = []
    runtime = _runtime(calls, observer=_observer({"exists": True}, contexts))
    run_id, gate = _gate_paused(runtime)
    assert contexts == []  # nothing is observed before the handoff returns
    runtime.approve_gate(run_id, gate["id"])
    dispatched = runtime.list_events(run_id)
    assert dispatched[-1].type == EventType.ACTION_STARTED  # approval = dispatch
    assert dispatched[-2].type == EventType.ACTION_AUTHORIZED
    assert contexts == []  # still nothing observed: the handoff has not returned
    with pytest.raises(RunBlocked, match="paused"):
        runtime.execute_next(run_id)
    final = runtime.complete_actor_handoff(run_id, _intent_id(runtime, run_id))

    assert calls == []
    assert final.state == RunState.COMPLETED
    (receipt,) = _receipts(runtime, run_id)
    assert receipt["verification"]["status"] == "pass"
    evidence = receipt["verification"]["evidence"]
    assert evidence["delegated_commit"] is True
    assert set(evidence["verifiers"]) == {"external_state"}
    external = evidence["verifiers"]["external_state"]["evidence"]
    assert external["independent_observation"]["ref"] == "crm:record/42@etag-7"
    assert contexts and all("output" not in context for context in contexts)
    types = _types(runtime, run_id)
    assert EventType.CAPABILITY_RESOLVED not in types
    assert EventType.CAPABILITY_INVOKED not in types
    authorized = types.index(EventType.ACTION_AUTHORIZED)
    assert authorized < types.index(EventType.ACTION_STARTED)
    assert types.index(EventType.ACTION_STARTED) < types.index(EventType.ACTION_RECEIPT_RECORDED)
    started = next(e for e in dispatched if e.type == EventType.ACTION_STARTED)
    assert receipt["started_at"] == started.occurred_at  # dated from dispatch, not return


def test_actor_approval_with_evidence_of_absence_fails_without_receipt() -> None:
    calls: list[str] = []
    runtime = _runtime(calls, observer=_observer({"exists": False}))
    run_id, gate = _gate_paused(runtime)
    assert _dispatch_and_return(runtime, run_id, gate).state == RunState.FAILED
    assert _receipts(runtime, run_id) == []
    assert calls == []


# --- rejection and an undecided gate follow the existing HumanGate semantics -------------


def _provider_gate_runtime(calls: list[str]) -> ControlPlane:
    # The existing gate: a destructive local write that the default policy sends to a human.
    return _runtime(
        calls,
        contract=CapabilityContract(provenance=PROVENANCE),
        verification=VerificationSpec(VerificationKind.DETERMINISTIC),
        side_effect=SideEffectClass.DESTRUCTIVE,
        trip=False,
    )


def _rejected_outcome(runtime: ControlPlane) -> dict[str, Any]:
    run_id, gate = _gate_paused(runtime)
    runtime.reject_gate(run_id, gate["id"])
    after_reject = runtime.get_run(run_id)
    actions = runtime.list_actions(run_id)
    outcome = {
        "state": after_reject.state,
        "node": after_reject.node_status["write"],
        "receipts": _receipts(runtime, run_id),
        "pending_gates": runtime.list_pending_gates(run_id),
        "rejected": EventType.ACTION_REJECTED in _types(runtime, run_id),
        "human_rejected": any(
            o.get("class") == "human_rejected" for o in runtime.list_observations(run_id)
        ),
        "intents": len(actions),
    }
    runtime.resume_run(run_id)
    with pytest.raises(RunBlocked, match="no executable node"):
        runtime.execute_next(run_id)
    outcome["after_resume"] = runtime.get_run(run_id).node_status["write"]
    return outcome


def test_actor_rejection_matches_the_existing_gate_semantics() -> None:
    actor_calls: list[str] = []
    provider_calls: list[str] = []
    actor = _rejected_outcome(_runtime(actor_calls))
    existing = _rejected_outcome(_provider_gate_runtime(provider_calls))

    assert actor == existing
    assert actor["state"] == RunState.PAUSED
    assert actor["node"] == NodeStatus.BLOCKED
    assert actor["receipts"] == [] and actor["pending_gates"] == []
    assert actor["rejected"] and actor["human_rejected"]
    assert actor_calls == []
    assert "invoke" not in provider_calls  # the existing gate also never invoked


def test_actor_gate_left_undecided_stays_paused_without_effects() -> None:
    """The runtime has no gate timeout: an unanswered gate keeps the run paused (Scenario R)."""

    calls: list[str] = []
    runtime = _runtime(calls, observer=_observer({"exists": True}))
    run_id, gate = _gate_paused(runtime)

    with pytest.raises(RunBlocked, match="paused"):
        runtime.execute_next(run_id)
    assert runtime.run_until_blocked(run_id).state == RunState.PAUSED
    runtime.resume_run(run_id)
    with pytest.raises(RunBlocked, match="no executable node"):
        runtime.execute_next(run_id)
    assert [g["id"] for g in runtime.list_pending_gates(run_id)] == [gate["id"]]
    assert not [e for e in runtime.list_events(run_id) if e.type == EventType.RUN_FAILED]

    cancelled = runtime.cancel_run(run_id)
    assert cancelled.state == RunState.CANCELLED
    assert _receipts(runtime, run_id) == []
    assert EventType.COMPENSATION_ATTEMPTED not in _types(runtime, run_id)
    assert calls == []


# --- the route admits only independent verification --------------------------------------


@pytest.mark.parametrize(
    ("verification", "side_effect", "message"),
    [
        (
            VerificationSpec(VerificationKind.DETERMINISTIC),
            SideEffectClass.NONE,
            "requires independent external_state",
        ),
        (MIXED, SideEffectClass.REVERSIBLE_WRITE, "no executor output to verify: deterministic"),
    ],
    ids=["read-only-without-external-state", "mixed-kinds"],
)
def test_actor_route_blocks_before_intent_without_independent_only_verification(
    verification: VerificationSpec, side_effect: SideEffectClass, message: str
) -> None:
    calls: list[str] = []
    runtime = _runtime(calls, verification=verification, side_effect=side_effect)
    run = runtime.create_run(Goal("delegate", ("done",)))
    with pytest.raises(RunBlocked, match=message):
        runtime.execute_next(run.id)
    assert calls == []
    assert runtime.get_run(run.id).node_status["write"] == NodeStatus.BLOCKED
    assert runtime.list_actions(run.id) == []
    assert runtime.list_pending_gates(run.id) == []


# --- capabilities without the actor route are unchanged ----------------------------------


def test_provider_route_delegation_still_resolves_and_invokes() -> None:
    calls: list[str] = []
    runtime = _runtime(
        calls,
        contract=PROVIDER_DELEGATION,
        verification=MIXED,
        observer=_observer({"exists": True}),
        trip=False,
    )
    run = runtime.create_run(Goal("delegate", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    assert "invoke" in calls and "resolve:write" in calls
    assert runtime.list_pending_gates(run.id) == []
    pre = _pre_resolutions(runtime, run.id)
    assert pre[-1]["delegation_route"] == "provider"


@pytest.mark.parametrize("contract", [ACTOR, CapabilityContract(provenance=PROVENANCE)])
def test_locally_satisfied_capability_takes_the_provider_path(
    contract: CapabilityContract,
) -> None:
    calls: list[str] = []
    runtime = _runtime(
        calls,
        contract=contract,
        verification=VerificationSpec(VerificationKind.DETERMINISTIC),
        trip=False,
        runtime_environment=frozenset({RuntimeRequirement.GPU}),
    )
    run = runtime.create_run(Goal("local", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    assert "invoke" in calls
    pre = _pre_resolutions(runtime, run.id)
    assert pre[-1]["pre_resolution"]["state"] == "satisfied"
    assert pre[-1]["delegation_route"] is None
    assert runtime.list_actions(run.id)[0]["intent"]["delegated"] is False


# --- review round 1: policy before the gate (B3-R2) --------------------------------------


def _policy(rule: PolicyRule) -> PolicyEngine:
    return PolicyEngine((rule, PolicyRule("allow", PolicyDecisionType.ALLOW, "ok")))


def test_actor_route_honours_policy_deny_before_the_gate() -> None:
    calls: list[str] = []
    deny = _policy(
        PolicyRule(
            "no-destructive",
            PolicyDecisionType.DENY,
            "destructive denied",
            side_effect_classes=(SideEffectClass.DESTRUCTIVE,),
        )
    )
    runtime = _runtime(calls, side_effect=SideEffectClass.DESTRUCTIVE, policy=deny)
    run = runtime.create_run(Goal("delegate", ("done",)))
    with pytest.raises(RunBlocked, match="policy denied action"):
        runtime.execute_next(run.id)
    types = _types(runtime, run.id)
    assert EventType.POLICY_EVALUATED in types
    assert runtime.list_actions(run.id) == []
    assert runtime.list_pending_gates(run.id) == []
    assert calls == []


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (
            PolicyRule(
                "justify",
                PolicyDecisionType.ALLOW,
                "explain yourself",
                justification_required=True,
            ),
            "justification",
        ),
        (
            PolicyRule(
                "human-check",
                PolicyDecisionType.ALLOW,
                "needs human verification",
                required_verification=("human",),
            ),
            "policy-required verification",
        ),
    ],
    ids=["justification", "required-verification"],
)
def test_actor_route_honours_policy_requirements_before_the_gate(
    rule: PolicyRule, message: str
) -> None:
    calls: list[str] = []
    runtime = _runtime(calls, policy=_policy(rule))
    run = runtime.create_run(Goal("delegate", ("done",)))
    with pytest.raises(RunBlocked, match=message):
        runtime.execute_next(run.id)
    assert runtime.list_actions(run.id) == []
    assert runtime.list_pending_gates(run.id) == []
    assert calls == []


def test_actor_policy_sees_node_level_inputs() -> None:
    seen: list[dict] = []

    class Recording(PolicyEngine):
        def evaluate_action(self, **kwargs: Any) -> Any:
            seen.append(dict(kwargs))
            return super().evaluate_action(**kwargs)

    runtime = _runtime([], policy=Recording())
    run = runtime.create_run(Goal("delegate", ("done",)))
    runtime.run_until_blocked(run.id)
    assert seen and seen[-1]["side_effect_class"] == SideEffectClass.REVERSIBLE_WRITE
    assert seen[-1]["reversible"] is False
    assert seen[-1]["run_risk"] == runtime.get_run(run.id).risk_level
    assert seen[-1]["capability_risk"] == RiskLevel.LOW


# --- review round 1: the intent is bound to its route (B3-R1, B3-R3) ---------------------


def test_dispatched_actor_intent_is_never_executed_on_the_provider_path(tmp_path: Any) -> None:
    """B3-R1 regression: a host that HAS the facility must not invoke a dispatched handoff."""

    db = str(tmp_path / "runs.sqlite")
    calls: list[str] = []
    first = _runtime(calls, store=SQLiteEventStore(db))
    run_id, gate = _gate_paused(first)
    first.approve_gate(run_id, gate["id"])

    gpu = _runtime(
        calls,
        store=SQLiteEventStore(db),
        runtime_environment=frozenset({RuntimeRequirement.GPU}),
        observer=_observer({"exists": True}),
    )
    gpu.resume_run(run_id)
    with pytest.raises(RunBlocked):
        gpu.execute_next(run_id)
    # Force the node back to pending: the route binding still refuses the provider path.
    gpu._set_node(gpu.get_run(run_id), "write", NodeStatus.PENDING)
    with pytest.raises(RunBlocked, match="bound to the actor route"):
        gpu.execute_next(run_id)
    assert calls == []
    assert gpu.get_run(run_id).node_status["write"] == NodeStatus.BLOCKED
    assert gpu.complete_actor_handoff(run_id, _intent_id(gpu, run_id)).state == RunState.COMPLETED
    assert calls == []
    assert len(gpu.list_actions(run_id)) == 1


def test_same_host_refuses_to_re_dispatch_an_authorized_actor_intent() -> None:
    calls: list[str] = []
    runtime = _runtime(calls)
    run_id, gate = _gate_paused(runtime)
    runtime.approve_gate(run_id, gate["id"])
    runtime.resume_run(run_id)
    runtime._set_node(runtime.get_run(run_id), "write", NodeStatus.PENDING)
    with pytest.raises(RunBlocked, match="already dispatched"):
        runtime.execute_next(run_id)
    assert runtime.list_pending_gates(run_id) == []
    assert calls == []


def test_complete_actor_handoff_refuses_undispatched_and_provider_intents() -> None:
    runtime = _runtime([])
    run_id, _ = _gate_paused(runtime)
    with pytest.raises(RunBlocked, match="never dispatched"):
        runtime.complete_actor_handoff(run_id, _intent_id(runtime, run_id))
    provider_calls: list[str] = []
    provider = _provider_gate_runtime(provider_calls)
    provider_run, _ = _gate_paused(provider)
    with pytest.raises(RunBlocked, match="not an actor-route handoff"):
        provider.complete_actor_handoff(provider_run, _intent_id(provider, provider_run))


def test_cancel_after_dispatch_leaves_the_effect_reconcilable() -> None:
    """B3-R3: after dispatch the effect stays started and reconcilable, never 'never started'."""

    calls: list[str] = []
    runtime = _runtime(calls)
    run_id, gate = _gate_paused(runtime)
    runtime.approve_gate(run_id, gate["id"])
    with pytest.raises(RunBlocked, match="not pending"):
        runtime.reject_gate(run_id, gate["id"])  # the decision is already recorded
    assert runtime.cancel_run(run_id).state == RunState.CANCELLED
    assert any(
        o.get("class") == "unknown_failure" and "dispatched actor handoff" in o.get("detail", "")
        for o in runtime.list_observations(run_id)
    )
    intent_id = _intent_id(runtime, run_id)
    assert runtime.list_actions(run_id)[0]["receipt"] is None
    runtime.external_observer = _observer({"exists": True})
    runtime.reconcile_action(run_id, intent_id, occurred=True)
    (receipt,) = _receipts(runtime, run_id)
    assert receipt["verification"]["status"] == "pass"
    assert calls == []


def test_reject_before_dispatch_starts_nothing() -> None:
    runtime = _runtime([])
    run_id, gate = _gate_paused(runtime)
    runtime.reject_gate(run_id, gate["id"])
    assert EventType.ACTION_STARTED not in _types(runtime, run_id)
    with pytest.raises(RunBlocked):
        runtime.complete_actor_handoff(run_id, _intent_id(runtime, run_id))


# --- review round 1: process restart (B3-R6) ---------------------------------------------


@pytest.mark.parametrize(
    "environment",
    [frozenset(), frozenset({RuntimeRequirement.GPU})],
    ids=["same-facilities", "host-has-facility"],
)
def test_restart_with_pending_actor_gate(tmp_path: Any, environment: frozenset) -> None:
    db = str(tmp_path / "runs.sqlite")
    calls: list[str] = []
    run_id, gate = _gate_paused(_runtime(calls, store=SQLiteEventStore(db)))
    restarted = _runtime(
        calls,
        store=SQLiteEventStore(db),
        runtime_environment=environment,
        observer=_observer({"exists": True}),
    )
    assert [g["id"] for g in restarted.list_pending_gates(run_id)] == [gate["id"]]
    final = _dispatch_and_return(restarted, run_id, gate)
    assert final.state == RunState.COMPLETED
    assert calls == []


def test_restart_with_dispatched_unreturned_handoff(tmp_path: Any) -> None:
    db = str(tmp_path / "runs.sqlite")
    calls: list[str] = []
    first = _runtime(calls, store=SQLiteEventStore(db))
    run_id, gate = _gate_paused(first)
    first.approve_gate(run_id, gate["id"])
    restarted = _runtime(
        calls, store=SQLiteEventStore(db), observer=_observer({"exists": True})
    )
    assert restarted.get_run(run_id).state == RunState.PAUSED
    assert restarted.list_pending_gates(run_id) == []
    final = restarted.complete_actor_handoff(run_id, _intent_id(restarted, run_id))
    assert final.state == RunState.COMPLETED
    (receipt,) = _receipts(restarted, run_id)
    assert receipt["verification"]["status"] == "pass"
    assert calls == []


def test_restart_with_blocked_actor_verification(tmp_path: Any) -> None:
    db = str(tmp_path / "runs.sqlite")
    calls: list[str] = []
    first = _runtime(calls, store=SQLiteEventStore(db))
    run_id, gate = _gate_paused(first)
    assert _dispatch_and_return(first, run_id, gate).state == RunState.PAUSED
    restarted = _runtime(
        calls,
        store=SQLiteEventStore(db),
        runtime_environment=frozenset({RuntimeRequirement.GPU}),
        observer=_observer({"exists": True}),
    )
    with pytest.raises(RunBlocked, match="started side effect"):
        restarted.resume_run(run_id)
    reconciled = restarted.reconcile_action(run_id, _intent_id(restarted, run_id), occurred=True)
    assert reconciled.node_status["write"] == NodeStatus.COMPLETED
    assert [r["verification"]["status"] for r in _receipts(restarted, run_id)] == ["pass"]
    assert calls == []
