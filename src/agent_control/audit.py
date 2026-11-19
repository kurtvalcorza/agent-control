from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import Plan

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "token",
        "private_key",
    }
)
_SECRET_REF_PREFIXES = ("secret://", "env://", "vault://")
_TEXT_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\s*[=:]\s*[^\s,;]+"),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
)


def _is_sensitive_key(key: object) -> bool:
    return str(key).lower().replace("-", "_") in SENSITIVE_KEYS


def _is_secret_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_SECRET_REF_PREFIXES)


def redact_text(value: str) -> str:
    result = value
    result = _TEXT_PATTERNS[0].sub(r"\1<redacted>", result)
    result = _TEXT_PATTERNS[1].sub(lambda match: f"{match.group(1)}=<redacted>", result)
    result = _TEXT_PATTERNS[2].sub("<redacted>", result)
    return result


def redact_for_audit(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                result[str(key)] = item if _is_secret_ref(item) else "<redacted>"
            else:
                result[str(key)] = redact_for_audit(item)
        return result
    if isinstance(value, tuple):
        return tuple(redact_for_audit(item) for item in value)
    if isinstance(value, list):
        return [redact_for_audit(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _validate_value(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            if _is_sensitive_key(key) and not _is_secret_ref(item):
                raise ValueError(
                    f"inline secret material is forbidden at {child}; use a secret reference"
                )
            _validate_value(item, child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_value(item, f"{path}[{index}]")


def validate_plan_secrets(plan: Plan) -> None:
    for node in plan.nodes:
        _validate_value(node.inputs, f"plan.nodes[{node.id}].inputs")
