"""RFC #6 Phase B1: reference-based observations are re-fetched by a host resolver (Scenario P)."""

from __future__ import annotations

from typing import Any

import pytest
from test_delegated_verification import _ambiguous, _receipts
from test_delegated_verification import _runtime as _delegated_runtime

from agent_control import (
    CallbackExternalObservationResolver,
    CallbackExternalStateObserver,
    ControlPlane,
    ExternalObservation,
    Goal,
    ObservationResolution,
    RunBlocked,
    RunState,
)
from agent_control.models import NodeStatus

REF = "crm:record/42@etag-7"
EXECUTOR_SENTINEL = "executor-claim-7f3a"


def _reference_only_observer(contexts: list[dict] | None = None) -> CallbackExternalStateObserver:
    def observe(context: dict) -> ExternalObservation:
        if contexts is not None:
            contexts.append(dict(context))
        return ExternalObservation(
            ref=REF,
            observer="dispatching-run",
            observed_at="2026-11-19T00:00:00Z",
            verification_method="refetch",
        )

    return CallbackExternalStateObserver(observe)


def _resolver(
    state: Any, calls: list[Any] | None = None, *, method: str = "crm-api-refetch"
) -> CallbackExternalObservationResolver:
    def resolve(reference: dict) -> ObservationResolution:
        if calls is not None:
            calls.append(reference)
        return ObservationResolution(
            state=state,
            resolver="host-crm-resolver",
            resolved_at="2026-11-19T00:00:05Z",
            method=method,
        )

    return CallbackExternalObservationResolver(resolve)


def _self_reporting(_: dict) -> dict:
    return {"verified": True, "effects": [EXECUTOR_SENTINEL], "state": {"exists": True}}


def _ref_runtime(
    handler: Any = _self_reporting,
    *,
    resolver: Any = None,
    subjects: list[Any] | None = None,
    contexts: list[dict] | None = None,
) -> ControlPlane:
    runtime = _delegated_runtime(
        handler,
        observer=_reference_only_observer(contexts),
        seen_subjects=subjects,
    )
    runtime.observation_resolver = resolver
    return runtime


def _external_evidence(receipt: dict) -> dict:
    return receipt["verification"]["evidence"]["verifiers"]["external_state"]["evidence"]


def test_scenario_p_resolved_reference_reaches_verifier_and_passes() -> None:
    subjects: list[Any] = []
    base = _ref_runtime(subjects=subjects)
    runtime = ControlPlane(
        store=base.store,
        planner=base.planner,
        capabilities=base.capabilities,
        verifiers=base.verifiers,
        capability_sources=base.capability_sources,
        external_observer=base.external_observer,
        observation_resolver=_resolver({"exists": True}),
    )
    run = runtime.create_run(Goal("ref", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    assert len(subjects) == 1
    subject = subjects[0]
    assert isinstance(subject, ExternalObservation)
    assert subject.ref == REF
    assert subject.state == {"exists": True}
    receipt = _receipts(runtime, run.id)[-1]
    assert receipt["verification"]["status"] == "pass"
    evidence = _external_evidence(receipt)
    assert evidence["independent_observation"]["ref"] == REF
    assert evidence["observation_resolution"] == {
        "resolver": "host-crm-resolver",
        "resolved_at": "2026-11-19T00:00:05Z",
        "method": "crm-api-refetch",
        "metadata": {},
    }


def test_scenario_p_resolution_metadata_reaches_evidence() -> None:
    def resolve(_: dict) -> ObservationResolution:
        return ObservationResolution(
            state={"exists": True},
            resolver="host-crm-resolver",
            resolved_at="2026-11-19T00:00:05Z",
            method="crm-api-refetch",
            metadata={"etag": "etag-7", "http_status": 200},
        )

    runtime = _ref_runtime(resolver=CallbackExternalObservationResolver(resolve))
    run = runtime.create_run(Goal("ref", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    evidence = _external_evidence(_receipts(runtime, run.id)[-1])
    assert evidence["observation_resolution"]["metadata"] == {"etag": "etag-7", "http_status": 200}


def _raising(_: dict) -> ObservationResolution:
    raise TimeoutError("crm unreachable")


@pytest.mark.parametrize(
    ("resolver", "reason"),
    [
        (None, "no external observation resolver"),
        (CallbackExternalObservationResolver(_raising), "resolution failed"),
        (CallbackExternalObservationResolver(lambda _: None), "no resolved state"),
        (_resolver(None), "no resolved state"),
    ],
    ids=["no-resolver", "resolver-raises", "resolver-returns-none", "resolution-without-state"],
)
def test_scenario_p_unresolved_reference_blocks_without_pass_receipt(
    resolver: Any, reason: str
) -> None:
    subjects: list[Any] = []
    runtime = _ref_runtime(resolver=resolver, subjects=subjects)
    run = runtime.create_run(Goal("ref", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.PAUSED
    assert subjects == []
    # The run path keeps its existing receipt-of-the-attempt, but it is never an authoritative
    # PASS: its verification is BLOCKED and records the unresolved reference.
    receipts = _receipts(runtime, run.id)
    assert [r["verification"]["status"] for r in receipts] == ["blocked"]
    external = receipts[0]["verification"]["evidence"]["verifiers"]["external_state"]
    assert external["status"] == "blocked"
    assert reason in external["message"]
    assert external["evidence"]["independent_observation"]["ref"] == REF
    assert external["evidence"]["observation_resolution"] is None


class _ObserverAndResolver:
    """One object with a working observe() and a working resolve() (B1-R1)."""

    def observe(self, context: dict) -> ExternalObservation:
        del context
        return ExternalObservation(ref=REF, observer="dispatching-run", observed_at="t")

    def resolve(self, reference: dict) -> ObservationResolution:
        del reference
        return ObservationResolution(
            state={"exists": True}, resolver="same-object", resolved_at="t", method="m"
        )


def test_scenario_p_observer_cannot_double_as_resolver() -> None:
    subjects: list[Any] = []
    both = _ObserverAndResolver()
    runtime = _delegated_runtime(_self_reporting, observer=both, seen_subjects=subjects)
    runtime.observation_resolver = both
    run = runtime.create_run(Goal("ref", ("done",)))
    # Without the identity tripwire this object would resolve its own reference and PASS.
    assert runtime.run_until_blocked(run.id).state == RunState.PAUSED
    assert subjects == []
    receipts = _receipts(runtime, run.id)
    assert [r["verification"]["status"] for r in receipts] == ["blocked"]
    external = receipts[0]["verification"]["evidence"]["verifiers"]["external_state"]
    assert external["message"] == (
        "external observation resolver must be separate from the observer"
    )


def test_scenario_p_resolved_state_contradicting_the_effect_fails() -> None:
    runtime = _ref_runtime(resolver=_resolver({"exists": False}))
    run = runtime.create_run(Goal("ref", ("done",)))
    result = runtime.run_until_blocked(run.id)
    assert result.state != RunState.COMPLETED
    receipts = _receipts(runtime, run.id)
    assert all(r["verification"]["status"] != "pass" for r in receipts)
    failed = [r for r in receipts if r["verification"]["status"] == "fail"]
    assert failed, "a contradicting resolved state must surface as FAIL"
    evidence = _external_evidence(failed[-1])
    assert evidence["observation_resolution"]["resolver"] == "host-crm-resolver"


def test_scenario_p_resolver_never_receives_executor_output() -> None:
    calls: list[Any] = []
    contexts: list[dict] = []
    runtime = _ref_runtime(resolver=_resolver({"exists": True}, calls), contexts=contexts)
    run = runtime.create_run(Goal("ref", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    assert calls == [
        {
            "ref": REF,
            "observer": "dispatching-run",
            "observed_at": "2026-11-19T00:00:00Z",
            "verification_method": "refetch",
        }
    ]
    assert EXECUTOR_SENTINEL not in repr(calls)
    assert contexts and all("output" not in context for context in contexts)


def test_scenario_p_direct_observation_does_not_consult_resolver() -> None:
    from test_delegated_verification import _observer

    calls: list[Any] = []
    runtime = _delegated_runtime(_self_reporting, observer=_observer({"exists": True}))
    runtime.observation_resolver = _resolver({"exists": False}, calls)
    run = runtime.create_run(Goal("direct", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    assert calls == []
    evidence = _external_evidence(_receipts(runtime, run.id)[-1])
    assert "observation_resolution" not in evidence


# --- delegated reconciliation shares the rule ------------------------------------------------


def _ambiguous_ref(resolver: Any = None) -> tuple[ControlPlane, str, str]:
    runtime = _ref_runtime(_ambiguous, resolver=resolver)
    run = runtime.create_run(Goal("delegate", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.PAUSED
    return runtime, run.id, runtime.list_actions(run.id)[0]["intent"]["id"]


@pytest.mark.parametrize(
    "resolver",
    [None, CallbackExternalObservationResolver(_raising), _resolver(None)],
    ids=["no-resolver", "resolver-raises", "resolution-without-state"],
)
def test_reconcile_unresolved_reference_blocks_without_receipt(resolver: Any) -> None:
    runtime, run_id, intent_id = _ambiguous_ref(resolver)
    with pytest.raises(RunBlocked, match="independent external-state evidence"):
        runtime.reconcile_action(run_id, intent_id, occurred=True)
    assert runtime.list_actions(run_id)[0]["receipt"] is None
    assert runtime.get_run(run_id).node_status["write"] == NodeStatus.BLOCKED


def test_reconcile_resolved_reference_confirms_operator_claim() -> None:
    calls: list[Any] = []
    runtime, run_id, intent_id = _ambiguous_ref(_resolver({"exists": True}, calls))
    reconciled = runtime.reconcile_action(run_id, intent_id, occurred=True)
    assert reconciled.node_status["write"] == NodeStatus.COMPLETED
    assert [call["ref"] for call in calls] == [REF]
    receipt = runtime.list_actions(run_id)[0]["receipt"]
    assert receipt["verification"]["status"] == "pass"
    external = receipt["verification"]["evidence"]["external_state"]
    assert external["evidence"]["independent_observation"]["ref"] == REF
    assert external["evidence"]["observation_resolution"]["method"] == "crm-api-refetch"


@pytest.mark.parametrize(("occurred", "observed"), [(False, True), (True, False)])
def test_reconcile_resolved_reference_keeps_r2_contradiction_guard(
    occurred: bool, observed: bool
) -> None:
    runtime, run_id, intent_id = _ambiguous_ref(_resolver({"exists": observed}))
    with pytest.raises(RunBlocked, match="contradicts"):
        runtime.reconcile_action(run_id, intent_id, occurred=occurred)
    assert runtime.list_actions(run_id)[0]["receipt"] is None
    assert runtime.get_run(run_id).node_status["write"] == NodeStatus.BLOCKED


def test_observation_resolution_requires_provenance_fields() -> None:
    with pytest.raises(ValueError):
        ObservationResolution(state={"exists": True}, resolver=" ", resolved_at="t", method="m")
    with pytest.raises(ValueError):
        ObservationResolution(state={"exists": True}, resolver="r", resolved_at="t", method="")
