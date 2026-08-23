from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .models import VerificationKind, VerificationResult, VerificationSpec, VerificationStatus


class Verifier(Protocol):
    def verify(
        self, spec: VerificationSpec, output: Any, context: dict[str, Any]
    ) -> VerificationResult: ...


VerifierFunction = Callable[[VerificationSpec, Any, dict[str, Any]], VerificationResult]


class VerifierRegistry:
    """Provider-neutral verifier registry with fail-closed missing-verifier behavior."""

    def __init__(self) -> None:
        self._verifiers: dict[VerificationKind, VerifierFunction] = {}
        self.register(VerificationKind.DETERMINISTIC, self._deterministic)
        self.register(VerificationKind.HUMAN, self._human)

    def register(self, kind: VerificationKind, verifier: VerifierFunction) -> None:
        self._verifiers[kind] = verifier

    def unregister(self, kind: VerificationKind) -> None:
        self._verifiers.pop(kind, None)

    def verify(
        self,
        spec: VerificationSpec,
        output: Any,
        context: dict[str, Any],
    ) -> VerificationResult:
        verifier = self._verifiers.get(spec.kind)
        if verifier is None:
            return VerificationResult(
                VerificationStatus.BLOCKED,
                f"no verifier registered for {spec.kind.value}",
                {"missing_verifier": spec.kind.value, "required": spec.required},
            )
        return verifier(spec, output, context)

    @staticmethod
    def _deterministic(
        spec: VerificationSpec,
        output: Any,
        context: dict[str, Any],
    ) -> VerificationResult:
        del context
        if output is True:
            return VerificationResult(
                VerificationStatus.PASS,
                "deterministic predicate passed",
            )
        if isinstance(output, dict) and output.get("verified") is True:
            return VerificationResult(
                VerificationStatus.PASS,
                "output carries verified=true",
            )
        if not spec.criteria and output is not None:
            return VerificationResult(
                VerificationStatus.PASS,
                "non-null output accepted",
            )
        return VerificationResult(
            VerificationStatus.FAIL,
            "deterministic verification criteria were not satisfied",
        )

    @staticmethod
    def _human(
        spec: VerificationSpec,
        output: Any,
        context: dict[str, Any],
    ) -> VerificationResult:
        del spec, output, context
        return VerificationResult(
            VerificationStatus.BLOCKED,
            "human verification required",
            {"human_gate": True},
        )


class DefaultVerifier(VerifierRegistry):
    """Default deterministic + human verifier set."""
