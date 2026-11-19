"""RFC #6 Phase A: capability provenance, source checks, pre-resolution and clause IDs."""

from __future__ import annotations

from dataclasses import fields

import pytest

from agent_control import (
    CURRENT_EVENT_SCHEMA_VERSION,
    SUPPORTED_EVENT_SCHEMA_VERSIONS,
    CapabilityClause,
    CapabilityContract,
    CapabilityDescriptor,
    CapabilityPreResolutionState,
    CapabilityRegistry,
    CapabilitySourceRegistry,
    ControlPlane,
    EventType,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    ResolvedCapabilityProvenance,
    RunBlocked,
    RunState,
    RuntimeRequirement,
    SourceCheckResult,
    SourceCheckStatus,
    SQLiteEventStore,
    VerificationKind,
    VerificationSpec,
    canonical_digest,
    pre_resolve,
)
from agent_control.models import BudgetLimit, NodeStatus

DIGEST_A = canonical_digest(b"capability payload A")
DIGEST_B = canonical_digest(b"capability payload B")


def _prov(digest: str = DIGEST_A, revision: str | None = "abc123") -> ResolvedCapabilityProvenance:
    return ResolvedCapabilityProvenance(
        capability="work", source_ref="git:example/skills#work", digest=digest, revision=revision
    )


def _contract(**overrides: object) -> CapabilityContract:
    values: dict[str, object] = {"provenance": _prov()}
    values.update(overrides)
    return CapabilityContract(**values)  # type: ignore[arg-type]


class MutableSource:
    """Test source adapter: the current snapshot can change while a run is paused."""

    def __init__(self, contract: CapabilityContract) -> None:
        self.contract = contract
        self.available = True
        self.loads = 0

    def load(self, capability: str) -> CapabilityContract:
        assert capability == self.contract.capability
        self.loads += 1
        return self.contract

    def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
        if not self.available:
            return SourceCheckResult(SourceCheckStatus.UNAVAILABLE, reason="source unreachable")
        current = self.contract.provenance
        status = (
            SourceCheckStatus.MATCH
            if current.same_identity(recorded)
            else SourceCheckStatus.MISMATCH
        )
        return SourceCheckResult(status, current=current)


class TwoStepPlanner:
    def __init__(self, verification: VerificationSpec | None = None) -> None:
        self.verification = verification or VerificationSpec(VerificationKind.DETERMINISTIC)

    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode("first", "first step", ("work",), verification=self.verification),
                PlanNode(
                    "second",
                    "second step",
                    ("work",),
                    dependencies=("first",),
                    verification=self.verification,
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


def _runtime(
    source: MutableSource | None,
    *,
    planner: TwoStepPlanner | None = None,
    store: SQLiteEventStore | None = None,
    environment: frozenset[RuntimeRequirement] = frozenset(),
    calls: list[str] | None = None,
    descriptor: CapabilityDescriptor | None = None,
) -> ControlPlane:
    registry = CapabilityRegistry()
    seen = calls if calls is not None else []
    registry.register(
        InProcessCapability(
            descriptor or CapabilityDescriptor("work", "1", "local"),
            lambda args: seen.append("invoked") or True,
        )
    )
    sources = None
    if source is not None:
        sources = CapabilitySourceRegistry()
        sources.register("work", source)
    return ControlPlane(
        store=store or SQLiteEventStore(":memory:"),
        planner=planner or TwoStepPlanner(),
        capabilities=registry,
        capability_sources=sources,
        runtime_environment=environment,
    )


def _events(runtime: ControlPlane, run_id: str, kind: EventType) -> list[dict]:
    return [dict(e.payload) for e in runtime.list_events(run_id) if e.type == kind]


# --- digest identity (RFC §4/§5; F18 item 1) ---------------------------------------------


def test_canonical_digest_is_deterministic_and_content_sensitive() -> None:
    assert canonical_digest(b"same bytes") == canonical_digest(b"same bytes")
    assert canonical_digest(b"same bytes") != canonical_digest(b"same bytez")
    assert len(DIGEST_A) == 64 and DIGEST_A.lower() == DIGEST_A
    assert _prov().identity == f"sha256:{DIGEST_A}"
    with pytest.raises(TypeError):
        canonical_digest("not bytes")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"digest": DIGEST_A.upper()},
        {"digest": DIGEST_A[:-1]},
        {"digest": "sha256:" + DIGEST_A},
        {"digest": "latest"},
        {"digest_algorithm": "md5"},
        {"source_ref": " "},
        {"revision": ""},
    ],
)
def test_provenance_rejects_untyped_or_non_sha256_identity(kwargs: dict) -> None:
    values = {"capability": "work", "source_ref": "src", "digest": DIGEST_A, **kwargs}
    with pytest.raises(ValueError):
        ResolvedCapabilityProvenance(**values)


def test_provenance_round_trips_and_is_not_on_the_descriptor() -> None:
    assert ResolvedCapabilityProvenance.from_dict(_prov().to_dict()) == _prov()
    descriptor_fields = {item.name for item in fields(CapabilityDescriptor)}
    assert not {"source_ref", "revision", "digest", "provenance"} & descriptor_fields


# --- vocabulary and pre-resolution (RFC §7/§8; scenarios G, H, K, R) -------------------


def test_runtime_vocabulary_is_small_and_excludes_human_availability() -> None:
    assert {item.name for item in RuntimeRequirement} == {
        "FILESYSTEM",
        "NETWORK",
        "LOCAL_PROCESS",
        "BROWSER_CONTEXT",
        "GPU",
        "PERSISTENT_STATE",
    }
    assert "HUMAN_AVAILABLE" not in RuntimeRequirement.__members__


def test_pre_resolution_satisfied() -> None:
    result = pre_resolve(
        _contract(runtime_requirements=frozenset({RuntimeRequirement.FILESYSTEM})),
        {RuntimeRequirement.FILESYSTEM},
    )
    assert result.state == CapabilityPreResolutionState.SATISFIED
    assert result.satisfied_requirements == ("filesystem",)
    assert result.missing_requirements == ()


def test_scenario_g_missing_required_governance_is_unsatisfied() -> None:
    result = pre_resolve(
        _contract(required_governance=("citation-policy",), present_governance=()), set()
    )
    assert result.state == CapabilityPreResolutionState.UNSATISFIED
    assert "citation-policy" in result.reason


def test_incomplete_closure_is_unsatisfied() -> None:
    result = pre_resolve(
        _contract(closure_complete=False, missing_dependencies=("shared/protocol.md",)), set()
    )
    assert result.state == CapabilityPreResolutionState.UNSATISFIED
    with pytest.raises(ValueError):
        _contract(closure_complete=True, missing_dependencies=("x",))


def test_scenario_h_optional_clause_degrades_explicitly() -> None:
    result = pre_resolve(
        _contract(
            clauses=(
                CapabilityClause("citation.bibliographic-existence"),
                CapabilityClause(
                    "citation.retraction-check",
                    optional=True,
                    runtime_requirements=frozenset({RuntimeRequirement.NETWORK}),
                ),
            )
        ),
        set(),
    )
    assert result.state == CapabilityPreResolutionState.DEGRADED
    assert result.dropped_clause_ids == ("citation.retraction-check",)
    assert result.satisfied_clause_ids == ("citation.bibliographic-existence",)
    assert set(result.satisfied_clause_ids) == set(result.declared_clause_ids) - set(
        result.dropped_clause_ids
    )


def test_scenario_k_runtime_mismatch_requires_delegation_only_when_permitted() -> None:
    contract = _contract(
        runtime_requirements=frozenset({RuntimeRequirement.GPU}), delegation_permitted=True
    )
    delegated = pre_resolve(contract, {RuntimeRequirement.FILESYSTEM})
    assert delegated.state == CapabilityPreResolutionState.DELEGATION_REQUIRED
    assert delegated.missing_requirements == ("gpu",)
    refused = pre_resolve(_contract(runtime_requirements=frozenset({"gpu"})), set())
    assert refused.state == CapabilityPreResolutionState.UNSATISFIED


def test_subset_rule_blocks_dropped_and_unknown_clause_ids() -> None:
    result = pre_resolve(
        _contract(
            clauses=(
                CapabilityClause("a"),
                CapabilityClause("b", optional=True, dependencies_present=False),
            )
        ),
        set(),
    )
    assert result.unsatisfied_clause_ids(["a"]) == ()
    assert result.unsatisfied_clause_ids(["b"]) == ("b",)
    assert result.unsatisfied_clause_ids(["typo"]) == ("typo",)
    with pytest.raises(ValueError):
        CapabilityClause("has space")


# --- runtime integration ------------------------------------------------------------------


def test_scenario_a_provenance_is_auditable_in_run_history() -> None:
    source = MutableSource(_contract())
    runtime = _runtime(source)
    run = runtime.create_run(Goal("provenance", ("done",)))
    final = runtime.run_until_blocked(run.id)
    assert final.state == RunState.COMPLETED
    resolved = _events(runtime, run.id, EventType.CAPABILITY_RESOLVED)
    assert resolved and all(item["source_provenance"] == _prov().to_dict() for item in resolved)
    assert all(item["pre_resolution_state"] == "satisfied" for item in resolved)
    pre = _events(runtime, run.id, EventType.CAPABILITY_PRE_RESOLVED)
    assert pre[0]["provenance"]["digest_algorithm"] == "sha256"
    assert pre[0]["provenance"]["revision"] == "abc123"
    assert runtime.inspect_run(run.id)["capability_pre_resolutions"]
    assert all(
        e.schema_version == CURRENT_EVENT_SCHEMA_VERSION for e in runtime.list_events(run.id)
    )
    assert {"1", CURRENT_EVENT_SCHEMA_VERSION} <= SUPPORTED_EVENT_SCHEMA_VERSIONS


def test_unsourced_capability_keeps_legacy_behaviour() -> None:
    runtime = _runtime(None)
    run = runtime.create_run(Goal("legacy", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    assert not _events(runtime, run.id, EventType.CAPABILITY_PRE_RESOLVED)
    assert all(
        item["source_provenance"] is None
        for item in _events(runtime, run.id, EventType.CAPABILITY_RESOLVED)
    )


def _paused_after_first(source: MutableSource, store: SQLiteEventStore) -> tuple[ControlPlane, str]:
    runtime = _runtime(source, store=store)
    run = runtime.create_run(Goal("pause", ("done",)))
    runtime.execute_next(run.id)
    runtime.pause_run(run.id)
    checkpoint = runtime.list_checkpoints(run.id)[-1]
    assert checkpoint["capability_source_versions"] == [_prov().to_dict()]
    return runtime, run.id


def test_scenario_b_resume_with_matching_source_needs_no_resource_provider() -> None:
    store = SQLiteEventStore(":memory:")
    source = MutableSource(_contract())
    runtime, run_id = _paused_after_first(source, store)
    assert runtime.resource_versions is None
    assert runtime.resume_run(run_id).state == RunState.READY
    checks = _events(runtime, run_id, EventType.CAPABILITY_SOURCE_CHECKED)
    assert [item["status"] for item in checks] == ["match"]
    assert runtime.run_until_blocked(run_id).state == RunState.COMPLETED


def test_scenario_c_no_source_check_path_blocks_resume() -> None:
    store = SQLiteEventStore(":memory:")
    _, run_id = _paused_after_first(MutableSource(_contract()), store)
    restarted = _runtime(None, store=store)
    with pytest.raises(RunBlocked, match="cannot be revalidated"):
        restarted.resume_run(run_id)
    assert restarted.get_run(run_id).state == RunState.PAUSED
    checks = _events(restarted, run_id, EventType.CAPABILITY_SOURCE_CHECKED)
    assert checks[-1]["status"] == "unavailable"


class EchoingSource(MutableSource):
    def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
        return SourceCheckResult(SourceCheckStatus.MATCH)


class RaisingSource(MutableSource):
    def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
        raise ConnectionError("dns failure")


@pytest.mark.parametrize("source_type", [EchoingSource, RaisingSource])
def test_scenario_d_unavailable_or_unverifiable_source_blocks(source_type: type) -> None:
    store = SQLiteEventStore(":memory:")
    _, run_id = _paused_after_first(MutableSource(_contract()), store)
    restarted = _runtime(source_type(_contract()), store=store)
    with pytest.raises(RunBlocked, match="cannot be revalidated"):
        restarted.resume_run(run_id)
    assert restarted.get_run(run_id).state == RunState.PAUSED


def test_scenario_d_unreachable_source_blocks() -> None:
    store = SQLiteEventStore(":memory:")
    source = MutableSource(_contract())
    runtime, run_id = _paused_after_first(source, store)
    source.available = False
    with pytest.raises(RunBlocked, match="cannot be revalidated"):
        runtime.resume_run(run_id)


def test_scenario_e_source_drift_while_paused_blocks_with_input_changed() -> None:
    store = SQLiteEventStore(":memory:")
    source = MutableSource(_contract())
    runtime, run_id = _paused_after_first(source, store)
    source.contract = _contract(provenance=_prov(DIGEST_B, revision="def456"))
    with pytest.raises(RunBlocked, match="capability source changed"):
        runtime.resume_run(run_id)
    observations = runtime.list_observations(run_id)
    assert any(
        item.get("class") == "input_changed" and item.get("capabilities") == ["work"]
        for item in observations
    )


def test_checker_claiming_match_for_different_identity_is_mismatch() -> None:
    class LyingSource(MutableSource):
        def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
            return SourceCheckResult(SourceCheckStatus.MATCH, current=_prov(DIGEST_B))

    store = SQLiteEventStore(":memory:")
    _, run_id = _paused_after_first(MutableSource(_contract()), store)
    restarted = _runtime(LyingSource(_contract()), store=store)
    with pytest.raises(RunBlocked, match="capability source changed"):
        restarted.resume_run(run_id)


def test_in_run_source_drift_blocks_until_replan() -> None:
    source = MutableSource(_contract())
    runtime = _runtime(source)
    run = runtime.create_run(Goal("drift", ("done",)))
    runtime.execute_next(run.id)
    source.contract = _contract(provenance=_prov(DIGEST_B))
    with pytest.raises(RunBlocked, match="capability source changed"):
        runtime.execute_next(run.id)
    runtime.request_replan(run.id, "adopt new capability source")
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED


def test_scenario_f_satisfied_pre_resolution_does_not_override_budget() -> None:
    calls: list[str] = []
    runtime = _runtime(
        MutableSource(_contract()),
        calls=calls,
        descriptor=CapabilityDescriptor("work", "1", "local", estimated_cost_usd=5.0),
    )
    run = runtime.create_run(Goal("budget", ("done",)), budget_limit=BudgetLimit(max_cost_usd=1.0))
    with pytest.raises(RunBlocked, match="budget pressure"):
        runtime.execute_next(run.id)
    pre = _events(runtime, run.id, EventType.CAPABILITY_PRE_RESOLVED)
    assert pre[-1]["pre_resolution"]["state"] == "satisfied"
    assert calls == []


def test_unsatisfied_pre_resolution_fails_as_capability_unavailable() -> None:
    calls: list[str] = []
    runtime = _runtime(
        MutableSource(_contract(required_governance=("policy",))), calls=calls
    )
    run = runtime.create_run(Goal("gov", ("done",)))
    failed = runtime.execute_next(run.id)
    assert failed.state == RunState.FAILED
    assert calls == []
    failure = _events(runtime, run.id, EventType.RUN_FAILED)[-1]
    assert failure["failure"]["category"] == "capability_unavailable"
    assert not _events(runtime, run.id, EventType.BUDGET_RESERVED)


def test_scenario_i_dropped_required_clause_blocks_before_invocation() -> None:
    calls: list[str] = []
    contract = _contract(
        clauses=(
            CapabilityClause(
                "citation.retraction-check",
                optional=True,
                runtime_requirements=frozenset({RuntimeRequirement.NETWORK}),
            ),
        )
    )
    planner = TwoStepPlanner(
        VerificationSpec(
            VerificationKind.DETERMINISTIC, requires_clause_ids=("citation.retraction-check",)
        )
    )
    runtime = _runtime(MutableSource(contract), planner=planner, calls=calls)
    run = runtime.create_run(Goal("clauses", ("done",)))
    with pytest.raises(RunBlocked, match="citation.retraction-check"):
        runtime.execute_next(run.id)
    assert calls == []
    assert runtime.get_run(run.id).node_status["first"] == NodeStatus.BLOCKED
    assert not _events(runtime, run.id, EventType.BUDGET_RESERVED)


def test_degraded_mode_runs_when_verification_needs_only_satisfied_clauses() -> None:
    contract = _contract(
        clauses=(
            CapabilityClause("core"),
            CapabilityClause("extra", optional=True, dependencies_present=False),
        )
    )
    planner = TwoStepPlanner(
        VerificationSpec(VerificationKind.DETERMINISTIC, requires_clause_ids=("core",))
    )
    runtime = _runtime(MutableSource(contract), planner=planner)
    run = runtime.create_run(Goal("degraded", ("done",)))
    assert runtime.run_until_blocked(run.id).state == RunState.COMPLETED
    pre = _events(runtime, run.id, EventType.CAPABILITY_PRE_RESOLVED)[0]["pre_resolution"]
    assert pre["state"] == "degraded" and pre["dropped_clause_ids"] == ["extra"]


@pytest.mark.parametrize("source", [MutableSource(_contract()), None])
def test_scenario_j_unknown_required_clause_blocks(source: MutableSource | None) -> None:
    calls: list[str] = []
    planner = TwoStepPlanner(
        VerificationSpec(VerificationKind.DETERMINISTIC, requires_clause_ids=("citaton.typo",))
    )
    runtime = _runtime(source, planner=planner, calls=calls)
    run = runtime.create_run(Goal("typo", ("done",)))
    with pytest.raises(RunBlocked, match="citaton.typo"):
        runtime.execute_next(run.id)
    assert calls == []
    assert not _events(runtime, run.id, EventType.BUDGET_RESERVED)


def test_requires_clause_ids_survive_event_replay() -> None:
    planner = TwoStepPlanner(
        VerificationSpec(VerificationKind.DETERMINISTIC, requires_clause_ids=("core",))
    )
    runtime = _runtime(
        MutableSource(_contract(clauses=(CapabilityClause("core"),))), planner=planner
    )
    run = runtime.create_run(Goal("replay", ("done",)))
    planned = runtime.plan_run(run.id)
    assert planned.plan is not None
    assert planned.plan.nodes[0].verification.requires_clause_ids == ("core",)
