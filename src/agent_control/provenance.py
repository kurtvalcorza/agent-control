"""Capability source provenance and runtime pre-resolution (RFC #6, Phase A).

These are normalized *control semantics*. They do not define how portable skills or capability
bundles serialize themselves: an external source adapter translates whatever representation it
consumes into a :class:`CapabilityContract` and re-derives a recorded source's identity, either
blinded through :meth:`DerivingCapabilitySource.derive` (Phase B2) or through
:meth:`CapabilitySource.check` under the documented adapter obligation.

Pre-resolution is a precondition to ``CapabilityRegistry.resolve()``. It decides only semantic
closure, required governance, runtime requirements, delegation permission and clause
satisfaction. Budget, permissions, risk, policy and provider selection stay with the registry and
policy engine.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

SHA256_ALGORITHM = "sha256"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_CLAUSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


def canonical_digest(payload: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 of a source adapter's canonical payload bytes."""

    if not isinstance(payload, bytes | bytearray | memoryview):
        raise TypeError("canonical capability payload must be bytes")
    return hashlib.sha256(bytes(payload)).hexdigest()


class RuntimeRequirement(StrEnum):
    """The deliberately small Phase A runtime-requirement vocabulary.

    ``HUMAN_AVAILABLE`` is intentionally absent: human participation is modelled by ``HumanGate``
    and ``VerificationKind.HUMAN``, never as a pre-resolution availability predicate.
    """

    FILESYSTEM = "filesystem"
    NETWORK = "network"
    LOCAL_PROCESS = "local_process"
    BROWSER_CONTEXT = "browser_context"
    GPU = "gpu"
    PERSISTENT_STATE = "persistent_state"


@dataclass(frozen=True, slots=True)
class ResolvedCapabilityProvenance:
    """Per-run identity of the capability source that governed one resolution.

    Never stored on the process-global ``CapabilityDescriptor``.
    """

    capability: str
    source_ref: str
    digest: str
    revision: str | None = None
    digest_algorithm: str = SHA256_ALGORITHM

    def __post_init__(self) -> None:
        if not isinstance(self.capability, str) or not self.capability.strip():
            raise ValueError("provenance capability must be a non-empty string")
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise ValueError("provenance source_ref must be a non-empty string")
        if self.digest_algorithm != SHA256_ALGORITHM:
            raise ValueError("provenance digest_algorithm must be 'sha256' in Phase A")
        if not isinstance(self.digest, str) or not _SHA256_HEX.fullmatch(self.digest):
            raise ValueError("provenance digest must be 64 lowercase hexadecimal characters")
        if self.revision is not None and (
            not isinstance(self.revision, str) or not self.revision.strip()
        ):
            raise ValueError("provenance revision must be a non-empty string when present")

    @property
    def identity(self) -> str:
        """Typed digest identity, ``sha256:<64 lowercase hex>``."""

        return f"{self.digest_algorithm}:{self.digest}"

    def same_identity(self, other: ResolvedCapabilityProvenance) -> bool:
        return (
            self.capability == other.capability
            and self.source_ref == other.source_ref
            and self.revision == other.revision
            and self.digest_algorithm == other.digest_algorithm
            and self.digest == other.digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "source_ref": self.source_ref,
            "revision": self.revision,
            "digest_algorithm": self.digest_algorithm,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResolvedCapabilityProvenance:
        revision = payload.get("revision")
        return cls(
            capability=str(payload["capability"]),
            source_ref=str(payload["source_ref"]),
            digest=str(payload["digest"]),
            revision=None if revision is None else str(revision),
            digest_algorithm=str(payload.get("digest_algorithm", SHA256_ALGORITHM)),
        )


@dataclass(frozen=True, slots=True)
class CapabilitySourceLocator:
    """Where a recorded capability source lives, without the digest it was recorded with.

    This is the only argument :meth:`DerivingCapabilitySource.derive` receives. It has no digest
    field (and ``slots`` admits no extra attributes), so the runtime never hands the recorded
    digest to ``derive()``, and the runtime compares the result itself (RFC #6 §22 B2, AC6-R3).
    This is a contract on the call, not an isolation boundary: in-process host code that goes
    looking for the digest (frame introspection, the event store) is outside what it can stop.
    """

    capability: str
    source_ref: str
    revision: str | None = None

    @classmethod
    def of(cls, recorded: ResolvedCapabilityProvenance) -> CapabilitySourceLocator:
        return cls(
            capability=recorded.capability,
            source_ref=recorded.source_ref,
            revision=recorded.revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "source_ref": self.source_ref,
            "revision": self.revision,
        }


class SourceCheckMethod(StrEnum):
    """How a recorded source identity was re-derived on resume."""

    DERIVE = "derive"
    CHECK = "check"


class SourceCheckStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SourceCheckResult:
    status: SourceCheckStatus
    current: ResolvedCapabilityProvenance | None = None
    reason: str = ""
    method: SourceCheckMethod | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "current": None if self.current is None else self.current.to_dict(),
            "reason": self.reason,
            "method": None if self.method is None else self.method.value,
        }


def _validate_clause_id(value: str) -> None:
    if not isinstance(value, str) or not _CLAUSE_ID.fullmatch(value):
        raise ValueError(f"invalid stable clause id: {value!r}")


@dataclass(frozen=True, slots=True)
class CapabilityClause:
    """A stable, machine-readable clause of a capability contract."""

    id: str
    optional: bool = False
    runtime_requirements: frozenset[RuntimeRequirement] = frozenset()
    dependencies_present: bool = True

    def __post_init__(self) -> None:
        _validate_clause_id(self.id)
        object.__setattr__(
            self,
            "runtime_requirements",
            frozenset(RuntimeRequirement(item) for item in self.runtime_requirements),
        )


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    """Normalized evidence a source adapter supplies for one capability source snapshot."""

    provenance: ResolvedCapabilityProvenance
    closure_complete: bool = True
    missing_dependencies: tuple[str, ...] = ()
    required_governance: tuple[str, ...] = ()
    present_governance: tuple[str, ...] = ()
    runtime_requirements: frozenset[RuntimeRequirement] = frozenset()
    clauses: tuple[CapabilityClause, ...] = ()
    delegation_permitted: bool = False

    def __post_init__(self) -> None:
        if self.missing_dependencies and self.closure_complete:
            raise ValueError("closure cannot be complete while dependencies are missing")
        ids = [clause.id for clause in self.clauses]
        if len(ids) != len(set(ids)):
            raise ValueError("capability clause ids must be unique")
        object.__setattr__(
            self,
            "runtime_requirements",
            frozenset(RuntimeRequirement(item) for item in self.runtime_requirements),
        )

    @property
    def capability(self) -> str:
        return self.provenance.capability


class CapabilityPreResolutionState(StrEnum):
    SATISFIED = "satisfied"
    DEGRADED = "degraded"
    DELEGATION_REQUIRED = "delegation_required"
    UNSATISFIED = "unsatisfied"


@dataclass(frozen=True, slots=True)
class CapabilityPreResolution:
    capability: str
    state: CapabilityPreResolutionState
    satisfied_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    declared_clause_ids: tuple[str, ...] = ()
    dropped_clause_ids: tuple[str, ...] = ()
    satisfied_clause_ids: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        declared = set(self.declared_clause_ids)
        dropped = set(self.dropped_clause_ids)
        if not dropped.issubset(declared):
            raise ValueError("dropped clause ids must be declared")
        if set(self.satisfied_clause_ids) != declared - dropped:
            raise ValueError("satisfied_clause_ids must equal declared minus dropped")

    def unsatisfied_clause_ids(self, required: Iterable[str]) -> tuple[str, ...]:
        """Required clause IDs that violate ``requires ⊆ satisfied`` (dropped *or* unknown)."""

        return tuple(sorted(set(required) - set(self.satisfied_clause_ids)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "state": self.state.value,
            "satisfied_requirements": list(self.satisfied_requirements),
            "missing_requirements": list(self.missing_requirements),
            "declared_clause_ids": list(self.declared_clause_ids),
            "dropped_clause_ids": list(self.dropped_clause_ids),
            "satisfied_clause_ids": list(self.satisfied_clause_ids),
            "reason": self.reason,
        }


def unsatisfied_clause_ids(
    pre_resolution: CapabilityPreResolution | None, required: Iterable[str]
) -> tuple[str, ...]:
    """Fail-closed subset check; an unsourced capability satisfies no clause."""

    if pre_resolution is None:
        return tuple(sorted(set(required)))
    return pre_resolution.unsatisfied_clause_ids(required)


def pre_resolve(
    contract: CapabilityContract,
    available: Iterable[RuntimeRequirement],
) -> CapabilityPreResolution:
    """Negotiate closure, governance, runtime requirements and clauses for this environment."""

    local = frozenset(RuntimeRequirement(item) for item in available)
    blocking: list[str] = []
    if not contract.closure_complete:
        missing = ", ".join(contract.missing_dependencies) or "undeclared"
        blocking.append(f"semantic closure incomplete: {missing}")
    missing_governance = [
        item for item in contract.required_governance if item not in contract.present_governance
    ]
    if missing_governance:
        blocking.append("required governance missing: " + ", ".join(missing_governance))

    mandatory_runtime = set(contract.runtime_requirements)
    all_runtime = set(contract.runtime_requirements)
    dropped: list[str] = []
    for clause in contract.clauses:
        all_runtime.update(clause.runtime_requirements)
        clause_runtime_missing = bool(clause.runtime_requirements - local)
        if clause.optional:
            if clause_runtime_missing or not clause.dependencies_present:
                dropped.append(clause.id)
            continue
        mandatory_runtime.update(clause.runtime_requirements)
        if not clause.dependencies_present:
            blocking.append(f"mandatory clause dependencies missing: {clause.id}")

    missing_mandatory = sorted(item.value for item in mandatory_runtime - local)
    declared = tuple(clause.id for clause in contract.clauses)
    satisfied_clauses = tuple(item for item in declared if item not in set(dropped))

    if blocking:
        state = CapabilityPreResolutionState.UNSATISFIED
        reason = "; ".join(blocking)
    elif missing_mandatory and contract.delegation_permitted:
        state = CapabilityPreResolutionState.DELEGATION_REQUIRED
        reason = "required runtime facilities absent locally: " + ", ".join(missing_mandatory)
    elif missing_mandatory:
        state = CapabilityPreResolutionState.UNSATISFIED
        reason = (
            "required runtime facilities absent and delegation not permitted: "
            + ", ".join(missing_mandatory)
        )
    elif dropped:
        state = CapabilityPreResolutionState.DEGRADED
        reason = "optional clauses dropped: " + ", ".join(dropped)
    else:
        state = CapabilityPreResolutionState.SATISFIED
        reason = "closure, governance and runtime requirements satisfied"

    return CapabilityPreResolution(
        capability=contract.capability,
        state=state,
        satisfied_requirements=tuple(sorted(item.value for item in all_runtime & local)),
        missing_requirements=tuple(sorted(item.value for item in all_runtime - local)),
        declared_clause_ids=declared,
        dropped_clause_ids=tuple(dropped),
        satisfied_clause_ids=satisfied_clauses,
        reason=reason,
    )


class CapabilitySource(Protocol):
    """Control-plane boundary to an external source adapter for one capability.

    ``check()`` sees the recorded provenance, digest included, so it is trusted to re-derive
    rather than copy it (the documented adapter obligation). Prefer
    :class:`DerivingCapabilitySource`, whose ``derive()`` never sees the recorded digest.
    """

    def load(self, capability: str) -> CapabilityContract: ...

    def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult: ...


class DerivingCapabilitySource(Protocol):
    """A source adapter that re-derives identity blinded (RFC #6 §22 B2).

    ``derive()`` receives only a :class:`CapabilitySourceLocator` (capability, source ref and
    revision) and returns the source's current provenance, or ``None`` when the source is
    unavailable. The runtime, not the adapter, compares it with the recorded identity. A source
    may also implement ``check()``; when ``derive()`` is present the runtime uses it alone.
    """

    def load(self, capability: str) -> CapabilityContract: ...

    def derive(self, locator: CapabilitySourceLocator) -> ResolvedCapabilityProvenance | None: ...


AnyCapabilitySource = CapabilitySource | DerivingCapabilitySource


@dataclass(slots=True)
class CallbackCapabilitySource:
    load_handler: Callable[[str], CapabilityContract]
    check_handler: Callable[[ResolvedCapabilityProvenance], SourceCheckResult]

    def load(self, capability: str) -> CapabilityContract:
        return self.load_handler(capability)

    def check(self, recorded: ResolvedCapabilityProvenance) -> SourceCheckResult:
        return self.check_handler(recorded)


@dataclass(slots=True)
class CallbackDerivingCapabilitySource:
    load_handler: Callable[[str], CapabilityContract]
    derive_handler: Callable[[CapabilitySourceLocator], ResolvedCapabilityProvenance | None]

    def load(self, capability: str) -> CapabilityContract:
        return self.load_handler(capability)

    def derive(self, locator: CapabilitySourceLocator) -> ResolvedCapabilityProvenance | None:
        return self.derive_handler(locator)


class CapabilitySourceRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, AnyCapabilitySource] = {}

    def register(self, capability: str, source: AnyCapabilitySource) -> None:
        if capability in self._sources:
            raise ValueError(f"capability source already registered: {capability!r}")
        self._sources[capability] = source

    def get(self, capability: str) -> AnyCapabilitySource | None:
        return self._sources.get(capability)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))


_MISSING = object()


def _exact_str(value: object) -> bool:
    return type(value) is str


def _rebuild_derived(derived: object) -> ResolvedCapabilityProvenance | None:
    """Rebuild a derived provenance from plain ``str`` values, or ``None`` if it is not plain.

    Subclasses of :class:`ResolvedCapabilityProvenance` and ``str`` subclasses in any field are
    rejected, so no adapter-controlled method takes part in the comparison (B2-R1).
    """

    if type(derived) is not ResolvedCapabilityProvenance:
        return None
    values = {
        name: object.__getattribute__(derived, name)
        for name in ("capability", "source_ref", "digest", "revision", "digest_algorithm")
    }
    if not all(
        _exact_str(value)
        for name, value in values.items()
        if not (name == "revision" and value is None)
    ):
        return None
    try:
        return ResolvedCapabilityProvenance(
            capability=str(values["capability"]),
            source_ref=str(values["source_ref"]),
            digest=str(values["digest"]),
            revision=None if values["revision"] is None else str(values["revision"]),
            digest_algorithm=str(values["digest_algorithm"]),
        )
    except ValueError:
        return None


def _identity_fields(item: ResolvedCapabilityProvenance) -> tuple[object, ...]:
    return (item.capability, item.source_ref, item.revision, item.digest_algorithm, item.digest)


def derive_capability_source(
    source: DerivingCapabilitySource,
    recorded: ResolvedCapabilityProvenance,
) -> SourceCheckResult:
    """Blinded re-derivation: ``derive()`` gets a locator, the runtime compares the result.

    The returned provenance is rebuilt on the runtime side from plain ``str`` values and compared
    field by field; no method of the returned object is called. A raise, ``None``, a value that
    is not a plain :class:`ResolvedCapabilityProvenance` (subclasses and ``str`` subclasses
    included), or a provenance for another capability or source ref is ``UNAVAILABLE``. A
    derived identity that differs from the recorded one (digest, revision or algorithm) is
    ``MISMATCH``; an equal one is ``MATCH``. ``current`` is always the rebuilt object.
    """

    method = SourceCheckMethod.DERIVE
    locator = CapabilitySourceLocator.of(recorded)
    try:
        derived = source.derive(locator)
    except Exception as exc:
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE, reason=f"source derive failed: {exc}", method=method
        )
    if derived is None:
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE,
            reason="capability source unavailable: derive returned no identity",
            method=method,
        )
    try:
        current = _rebuild_derived(derived)
    except Exception:
        current = None
    if current is None:
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE,
            reason="source derive returned an untyped result",
            method=method,
        )
    plain_recorded = (
        recorded.capability,
        recorded.source_ref,
        recorded.revision,
        recorded.digest_algorithm,
        recorded.digest,
    )
    if not all(_exact_str(value) or value is None for value in plain_recorded):
        recorded = ResolvedCapabilityProvenance.from_dict(recorded.to_dict())
    if (current.capability, current.source_ref) != (locator.capability, locator.source_ref):
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE,
            current=current,
            reason="source derive returned an identity for another capability source",
            method=method,
        )
    if _identity_fields(current) != _identity_fields(recorded):
        return SourceCheckResult(
            SourceCheckStatus.MISMATCH,
            current=current,
            reason="derived identity differs from recorded provenance",
            method=method,
        )
    return SourceCheckResult(SourceCheckStatus.MATCH, current=current, method=method)


def _defines_derive(source: object) -> bool:
    """Route on the class: a ``derive`` found only via ``__getattr__`` or Mock does not count."""

    found = inspect.getattr_static(type(source), "derive", _MISSING)
    return found is not _MISSING and found is not None


def check_capability_source(
    source: AnyCapabilitySource | None,
    recorded: ResolvedCapabilityProvenance,
) -> SourceCheckResult:
    """Normalize a source check; the runtime, not the checker, decides MATCH.

    A source whose class defines ``derive()`` is re-derived blinded
    (:func:`derive_capability_source`) and its ``check()``, if any, is not called. Otherwise
    ``check()`` is looked up on the instance, as before B2, and runs under the adapter
    obligation: a missing checker, a checker error, a MATCH without a freshly derived identity,
    or any unrecognized result is ``UNAVAILABLE``, and a claimed MATCH whose derived identity
    differs from the recorded one is ``MISMATCH``. An exception while probing the source is
    ``UNAVAILABLE``; nothing escapes to ``resume_run``.
    """

    if source is None:
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE,
            reason=f"no source checker registered for {recorded.capability!r}",
        )
    try:
        derives = _defines_derive(source)
    except Exception as exc:
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE, reason=f"source probe failed: {exc}"
        )
    if derives:
        deriving: DerivingCapabilitySource = source  # type: ignore[assignment]
        return derive_capability_source(deriving, recorded)
    try:
        check = getattr(source, "check", None)
    except Exception as exc:
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE,
            reason=f"source check failed: {exc}",
            method=SourceCheckMethod.CHECK,
        )
    if not callable(check):
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE,
            reason=f"capability source for {recorded.capability!r} has neither derive nor check",
        )
    return replace(_check_capability_source(check, recorded), method=SourceCheckMethod.CHECK)


def _check_capability_source(
    check: Callable[[ResolvedCapabilityProvenance], object],
    recorded: ResolvedCapabilityProvenance,
) -> SourceCheckResult:
    try:
        result = check(recorded)
    except Exception as exc:
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE, reason=f"source check failed: {exc}"
        )
    if not isinstance(result, SourceCheckResult):
        return SourceCheckResult(
            SourceCheckStatus.UNAVAILABLE, reason="source checker returned an untyped result"
        )
    if result.status == SourceCheckStatus.MATCH:
        if result.current is recorded:
            return SourceCheckResult(
                SourceCheckStatus.UNAVAILABLE,
                reason="source checker echoed the recorded provenance instead of re-deriving it",
            )
        if result.current is None:
            return SourceCheckResult(
                SourceCheckStatus.UNAVAILABLE,
                reason="source checker did not re-derive the current identity",
            )
        if not result.current.same_identity(recorded):
            return SourceCheckResult(
                SourceCheckStatus.MISMATCH,
                current=result.current,
                reason="re-derived identity differs from recorded provenance",
            )
        return result
    if result.status == SourceCheckStatus.MISMATCH:
        return result
    return SourceCheckResult(
        SourceCheckStatus.UNAVAILABLE,
        current=result.current,
        reason=result.reason or "capability source unavailable",
    )


@dataclass(frozen=True, slots=True)
class ExternalObservation:
    """External state acquired by the dispatching run, independently of a delegated executor."""

    ref: str
    observer: str
    observed_at: str
    state: Any = None
    verification_method: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ref.strip() or not self.observer.strip() or not self.observed_at.strip():
            raise ValueError("external observation needs ref, observer and observed_at")

    def reference(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "observer": self.observer,
            "observed_at": self.observed_at,
            "verification_method": self.verification_method,
        }


class ExternalStateObserver(Protocol):
    """Host-supplied observer trusted by the dispatching run.

    It receives only the run/node/intent context, never the delegated executor's output.
    """

    def observe(self, context: dict[str, Any]) -> ExternalObservation | None: ...


@dataclass(slots=True)
class CallbackExternalStateObserver:
    handler: Callable[[dict[str, Any]], ExternalObservation | None]

    def observe(self, context: dict[str, Any]) -> ExternalObservation | None:
        return self.handler(context)


@dataclass(frozen=True, slots=True)
class ObservationResolution:
    """External state re-fetched for an observation reference by a host resolver (RFC #6 P).

    ``method`` records how the reference was resolved (for example ``"crm-api-refetch"``). It and
    ``metadata`` are copied into verification evidence next to the reference; ``state`` is not.
    """

    state: Any
    resolver: str
    resolved_at: str
    method: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.resolver.strip() or not self.resolved_at.strip() or not self.method.strip():
            raise ValueError("observation resolution needs resolver, resolved_at and method")

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolver": self.resolver,
            "resolved_at": self.resolved_at,
            "method": self.method,
            "metadata": dict(self.metadata),
        }


class ExternalObservationResolver(Protocol):
    """Host-registered resolver for reference-only observations (``state=None``).

    It is separate from :class:`ExternalStateObserver` and receives only the observation reference
    (``ExternalObservation.reference()``): never run context and never the delegated executor's
    output. Because it needs only the reference, a later reconciliation can re-fetch the same one.
    Returning ``None`` or a resolution without state means the reference could not be resolved.
    """

    def resolve(self, reference: dict[str, Any]) -> ObservationResolution | None: ...


@dataclass(slots=True)
class CallbackExternalObservationResolver:
    handler: Callable[[dict[str, Any]], ObservationResolution | None]

    def resolve(self, reference: dict[str, Any]) -> ObservationResolution | None:
        return self.handler(reference)
