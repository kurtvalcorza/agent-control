from __future__ import annotations

import re
import uuid
from typing import NewType

RunId = NewType("RunId", str)
EventId = NewType("EventId", str)
PlanId = NewType("PlanId", str)
PlanNodeId = NewType("PlanNodeId", str)
ActionIntentId = NewType("ActionIntentId", str)
ActionReceiptId = NewType("ActionReceiptId", str)
CheckpointId = NewType("CheckpointId", str)
GateId = NewType("GateId", str)
MemoryId = NewType("MemoryId", str)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*_[0-9a-f]{32}$")


def new_id(prefix: str) -> str:
    if not prefix or not prefix.replace("_", "").isalnum() or not prefix[0].isalpha():
        raise ValueError("invalid id prefix")
    return f"{prefix}_{uuid.uuid4().hex}"


def validate_id(value: str, *, prefix: str | None = None) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid canonical id: {value!r}")
    if prefix is not None and not value.startswith(f"{prefix}_"):
        raise ValueError(f"expected {prefix}_ id, got {value!r}")
    return value
