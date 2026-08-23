from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import MemoryItem


class PersistentMemoryProvider(Protocol):
    def query(self, query: str, *, limit: int = 20) -> list[MemoryItem]: ...


@dataclass(frozen=True, slots=True)
class ContextSelection:
    items: tuple[MemoryItem, ...]
    reasons: dict[str, str]
    total_chars: int


class WorkingMemory:
    """Run-local memory with provenance, expiry and supersession-aware selection."""

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}

    def add(self, item: MemoryItem) -> None:
        self._items[item.id] = item

    def promote(self, provider: PersistentMemoryProvider, query: str, *, limit: int = 20) -> int:
        items = provider.query(query, limit=limit)
        for item in items:
            self.add(item)
        return len(items)

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(asdict(self._items[item_id]) for item_id in sorted(self._items))

    def snapshot_ref(self) -> str:
        encoded = json.dumps(
            self.snapshot(),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return "memory:sha256:" + hashlib.sha256(encoded).hexdigest()

    def restore(self, snapshot: tuple[dict[str, Any], ...]) -> None:
        restored: dict[str, MemoryItem] = {}
        for raw in snapshot:
            item = MemoryItem(
                id=str(raw["id"]),
                content=raw.get("content"),
                source=str(raw["source"]),
                created_at=str(raw["created_at"]),
                relevance=float(raw.get("relevance", 0.0)),
                confidence=float(raw.get("confidence", 1.0)),
                expires_at=(str(raw["expires_at"]) if raw.get("expires_at") else None),
                supersedes=tuple(str(value) for value in raw.get("supersedes", [])),
                tags=tuple(str(value) for value in raw.get("tags", [])),
            )
            restored[item.id] = item
        self._items = restored

    def select(self, *, limit: int = 20, tags: set[str] | None = None) -> list[MemoryItem]:
        return list(self.select_context(max_items=limit, tags=tags).items)

    def select_context(
        self,
        *,
        max_items: int = 20,
        max_chars: int | None = None,
        tags: set[str] | None = None,
    ) -> ContextSelection:
        now = datetime.now(UTC)
        superseded = {old_id for item in self._items.values() for old_id in item.supersedes}
        candidates: list[MemoryItem] = []
        for item in self._items.values():
            if item.id in superseded:
                continue
            if item.expires_at is not None:
                expiry = datetime.fromisoformat(item.expires_at.replace("Z", "+00:00"))
                if expiry <= now:
                    continue
            if tags and not tags.intersection(item.tags):
                continue
            candidates.append(item)
        candidates.sort(
            key=lambda item: (item.relevance, item.confidence, item.created_at),
            reverse=True,
        )
        selected: list[MemoryItem] = []
        reasons: dict[str, str] = {}
        total_chars = 0
        for item in candidates:
            if len(selected) >= max_items:
                break
            encoded = json.dumps(item.content, sort_keys=True, default=str)
            size = len(encoded)
            if max_chars is not None and total_chars + size > max_chars:
                continue
            selected.append(item)
            total_chars += size
            reasons[item.id] = (
                f"selected: relevance={item.relevance:.3f}, confidence={item.confidence:.3f}, "
                f"source={item.source}, chars={size}"
            )
        return ContextSelection(tuple(selected), reasons, total_chars)
