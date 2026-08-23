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
    """Provider-neutral verifier registry with fail-closed composite verification."""

    _STATUS_PRECEDENCE: tuple[VerificationStatus, ...] = (
        VerificationStatus.FAIL,
        VerificationStatus.BLOCKED,
        VerificationStatus.REPLAN,
        VerificationStatus.RETRY,
        VerificationStatus.INCONCLUSIVE,
        VerificationStatus.PASS,
    )

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
        results: list[tuple[VerificationKind, VerificationResult]] = []
        for kind in spec.kinds:
            verifier = self._verifiers.get(kind)
            single_spec = VerificationSpec(
                kind=kind,
                criteria=spec.criteria,
                required=spec.required,
            )
            if verifier is None:
                result = VerificationResult(
                    VerificationStatus.BLOCKED,
                    f"no verifier registered for {kind.value}",
                    {"missing_verifier": kind.value, "required": spec.required},
                )
            else:
                result = verifier(single_spec, output, context)
            results.append((kind, result))

        if len(results) == 1:
            return results[0][1]

        statuses = {result.status for _, result in results}
        aggregate = next(
            status for status in self._STATUS_PRECEDENCE if status in statuses
        )
        evidence = {
            "verifiers": {
                kind.value: {
                    "status": result.status.value,
                    "message": result.message,
                    "evidence": result.evidence,
                }
                for kind, result in results
            }
        }
        if aggregate == VerificationStatus.PASS:
            message = "all required verifiers passed"
        else:
            failing = [
                f"{kind.value}={result.status.value}"
                for kind, result in results
                if result.status != VerificationStatus.PASS
            ]
            message = "composite verification: " + ", ".join(failing)
        return VerificationResult(aggregate, message, evidence)

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
