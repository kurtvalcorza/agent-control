"""RFC #6 Phase B2: blinded source re-derivation through ``derive(locator)`` (AC6-R3, D2)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest
from test_capability_provenance import (
    DIGEST_A,
    DIGEST_B,
    MutableSource,
    _contract,
    _events,
    _paused_after_first,
    _prov,
    _runtime,
)

from agent_control import (
    CallbackDerivingCapabilitySource,
    CapabilityContract,
    CapabilitySourceLocator,
    EventType,
    ResolvedCapabilityProvenance,
    RunBlocked,
    RunState,
    SourceCheckMethod,
    SourceCheckResult,
    SourceCheckStatus,
    SQLiteEventStore,
    canonical_digest,
    check_capability_source,
)
from agent_control.models import NodeStatus

PAYLOAD_A = b"capability payload A"
PAYLOAD_B = b"capability payload B"
assert canonical_digest(PAYLOAD_A) == DIGEST_A


class DerivingSource:
    """Source double that only re-derives blinded, from a backing payload."""

    def __init__(self, payload: bytes = PAYLOAD_A, revision: str | None = "abc123") -> None:
        self.payload = payload
        self.revision = revision
        self.received: list[Any] = []

    def load(self, capability: str) -> CapabilityContract:
        return _contract()

    def derive(self, locator: CapabilitySourceLocator) -> ResolvedCapabilityProvenance | None:
        self.received.append(locator)
        return ResolvedCapabilityProvenance(
            capability=locator.capability,
            source_ref=locator.source_ref,
            digest=canonical_digest(self.payload),
            revision=self.revision,
        )


def _restart_with(source: object) -> tuple[Any, str, list[str]]:
    """Pause after the first node with a check() source, then restart with ``source``."""

    store = SQLiteEventStore(":memory:")
    _, run_id = _paused_after_first(MutableSource(_contract()), store)
    calls = ["invoked"]  # the first node ran in the pre-restart runtime
    restarted = _runtime(source, store=store, calls=calls)  # type: ignore[arg-type]
    return restarted, run_id, calls


def _last_check(runtime: Any, run_id: str) -> dict:
    return _events(runtime, run_id, EventType.CAPABILITY_SOURCE_CHECKED)[-1]


# --- the digest cannot reach derive(), by construction --------------------------------------


def test_locator_type_has_no_digest_field() -> None:
    assert [item.name for item in fields(CapabilitySourceLocator)] == [
        "capability",
        "source_ref",
        "revision",
    ]
    locator = CapabilitySourceLocator.of(_prov())
    assert not hasattr(locator, "digest")
    assert not hasattr(locator, "__dict__")
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        locator.digest = DIGEST_A  # type: ignore[attr-defined]


def test_derive_receives_only_a_locator_without_the_recorded_digest() -> None:
    source = DerivingSource()
    runtime, run_id, _ = _restart_with(source)
    assert runtime.resume_run(run_id).state == RunState.READY

    assert len(source.received) == 1
    (locator,) = source.received
    # Fails if the runtime ever hands derive() the recorded provenance (or anything carrying it).
    assert type(locator) is CapabilitySourceLocator
    assert locator == CapabilitySourceLocator("work", "git:example/skills#work", "abc123")
    visible = [repr(locator), str(locator.to_dict())]
    visible += [repr(getattr(locator, name)) for name in dir(locator) if not name.startswith("__")]
    assert not any(DIGEST_A in item for item in visible)


def test_cheating_derive_cannot_recover_the_recorded_digest() -> None:
    class Cheater(DerivingSource):
        def derive(self, locator: CapabilitySourceLocator) -> ResolvedCapabilityProvenance:
            recorded_digest = getattr(locator, "digest", None) or locator.to_dict().get("digest")
            return ResolvedCapabilityProvenance(
                capability=locator.capability,
                source_ref=locator.source_ref,
                digest=str(recorded_digest),
                revision=locator.revision,
            )

    runtime, run_id, _ = _restart_with(Cheater())
    with pytest.raises(RunBlocked, match="cannot be revalidated"):
        runtime.resume_run(run_id)
    check = _last_check(runtime, run_id)
    assert check["status"] == "unavailable"
    assert check["method"] == "derive"


# --- the runtime compares the derived result ------------------------------------------------


def test_blinded_derive_match_resumes_and_completes() -> None:
    runtime, run_id, calls = _restart_with(DerivingSource())
    assert runtime.resume_run(run_id).state == RunState.READY
    check = _last_check(runtime, run_id)
    assert check["status"] == "match"
    assert check["method"] == "derive"
    assert check["current"] == _prov().to_dict()
    assert runtime.run_until_blocked(run_id).state == RunState.COMPLETED
    assert calls == ["invoked", "invoked"]


def test_derive_returning_wrong_digest_blocks_with_input_changed() -> None:
    runtime, run_id, calls = _restart_with(DerivingSource(payload=PAYLOAD_B))
    with pytest.raises(RunBlocked, match="capability source changed"):
        runtime.resume_run(run_id)
    run = runtime.get_run(run_id)
    assert run.state == RunState.PAUSED
    assert run.node_status["second"] != NodeStatus.COMPLETED
    assert calls == ["invoked"]
    check = _last_check(runtime, run_id)
    assert (check["status"], check["method"]) == ("mismatch", "derive")
    assert check["current"]["digest"] == DIGEST_B
    assert any(
        item.get("class") == "input_changed" and item.get("capabilities") == ["work"]
        for item in runtime.list_observations(run_id)
    )


def test_derive_returning_moved_revision_is_mismatch() -> None:
    runtime, run_id, _ = _restart_with(DerivingSource(revision="def456"))
    with pytest.raises(RunBlocked, match="capability source changed"):
        runtime.resume_run(run_id)
    assert _last_check(runtime, run_id)["status"] == "mismatch"


class RaisingDerive(DerivingSource):
    def derive(self, locator: CapabilitySourceLocator) -> ResolvedCapabilityProvenance:
        raise ConnectionError("dns failure")


class NoneDerive(DerivingSource):
    def derive(self, locator: CapabilitySourceLocator) -> None:
        return None


class UntypedDerive(DerivingSource):
    def derive(self, locator: CapabilitySourceLocator) -> Any:
        return DIGEST_A


class OtherSourceDerive(DerivingSource):
    def derive(self, locator: CapabilitySourceLocator) -> ResolvedCapabilityProvenance:
        return ResolvedCapabilityProvenance(
            capability="work", source_ref="git:elsewhere#work", digest=DIGEST_A, revision="abc123"
        )


@pytest.mark.parametrize(
    "source_type", [RaisingDerive, NoneDerive, UntypedDerive, OtherSourceDerive]
)
def test_unusable_derive_blocks_resume(source_type: type) -> None:
    runtime, run_id, calls = _restart_with(source_type())
    with pytest.raises(RunBlocked, match="cannot be revalidated"):
        runtime.resume_run(run_id)
    run = runtime.get_run(run_id)
    assert run.state == RunState.PAUSED
    assert calls == ["invoked"]
    check = _last_check(runtime, run_id)
    assert (check["status"], check["method"]) == ("unavailable", "derive")


def test_derive_failure_reason_is_recorded() -> None:
    result = check_capability_source(RaisingDerive(), _prov())
    assert result.status == SourceCheckStatus.UNAVAILABLE
    assert result.method == SourceCheckMethod.DERIVE
    assert "dns failure" in result.reason


# --- check()-only, both, and callback sources ------------------------------------------------


def test_check_only_source_still_works_under_the_obligation() -> None:
    runtime, run_id, _ = _restart_with(MutableSource(_contract()))
    assert runtime.resume_run(run_id).state == RunState.READY
    check = _last_check(runtime, run_id)
    assert (check["status"], check["method"]) == ("match", "check")


class BothSource(DerivingSource):
    def __init__(
        self, payload: bytes = PAYLOAD_A, check_status: SourceCheckStatus = SourceCheckStatus.MATCH
    ) -> None:
        super().__init__(payload)
        self.check_status = check_status
        self.checks = 0

    def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
        self.checks += 1
        if self.check_status == SourceCheckStatus.UNAVAILABLE:
            raise ConnectionError("check path is down")
        return SourceCheckResult(self.check_status, current=_prov())


def test_source_with_both_uses_derive_and_never_calls_check() -> None:
    source = BothSource(check_status=SourceCheckStatus.UNAVAILABLE)
    runtime, run_id, _ = _restart_with(source)
    assert runtime.resume_run(run_id).state == RunState.READY
    assert source.checks == 0
    assert len(source.received) == 1
    assert _last_check(runtime, run_id)["method"] == "derive"


def test_source_with_both_cannot_launder_drift_through_check() -> None:
    # check() would claim MATCH for the recorded identity; derive() sees the payload changed.
    source = BothSource(payload=PAYLOAD_B, check_status=SourceCheckStatus.MATCH)
    runtime, run_id, _ = _restart_with(source)
    with pytest.raises(RunBlocked, match="capability source changed"):
        runtime.resume_run(run_id)
    assert source.checks == 0


def test_check_cannot_self_report_the_blinded_method() -> None:
    class Claims(MutableSource):
        def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
            return SourceCheckResult(
                SourceCheckStatus.MATCH, current=_prov(), method=SourceCheckMethod.DERIVE
            )

    result = check_capability_source(Claims(_contract()), _prov())
    assert (result.status, result.method) == (SourceCheckStatus.MATCH, SourceCheckMethod.CHECK)


def test_source_with_neither_derive_nor_check_is_unavailable() -> None:
    class LoadOnly:
        def load(self, capability: str) -> CapabilityContract:
            return _contract()

    result = check_capability_source(LoadOnly(), _prov())  # type: ignore[arg-type]
    assert result.status == SourceCheckStatus.UNAVAILABLE


def test_callback_deriving_source() -> None:
    received: list[CapabilitySourceLocator] = []

    def derive(locator: CapabilitySourceLocator) -> ResolvedCapabilityProvenance:
        received.append(locator)
        return ResolvedCapabilityProvenance(
            locator.capability, locator.source_ref, canonical_digest(PAYLOAD_A), locator.revision
        )

    source = CallbackDerivingCapabilitySource(lambda capability: _contract(), derive)
    runtime, run_id, _ = _restart_with(source)
    assert runtime.resume_run(run_id).state == RunState.READY
    assert received == [CapabilitySourceLocator.of(_prov())]
