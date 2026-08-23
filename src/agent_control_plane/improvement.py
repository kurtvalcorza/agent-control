from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class ProposalKind(StrEnum):
    POLICY = "policy"
    SKILL = "skill"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROMOTED = "promoted"


@dataclass(frozen=True, slots=True)
class ImprovementProposal:
    id: str
    kind: ProposalKind
    source_run_ids: tuple[str, ...]
    rationale: str
    candidate: dict[str, Any]
    status: ProposalStatus = ProposalStatus.DRAFT
    review_note: str | None = None


class ProposalRegistry:
    """Human-reviewed proposal workflow; it never mutates runtime policy or skills itself."""

    def __init__(self) -> None:
        self._items: dict[str, ImprovementProposal] = {}

    def create(
        self,
        *,
        kind: ProposalKind,
        source_run_ids: tuple[str, ...],
        rationale: str,
        candidate: dict[str, Any],
    ) -> ImprovementProposal:
        proposal = ImprovementProposal(
            id=f"proposal_{uuid.uuid4().hex}",
            kind=kind,
            source_run_ids=source_run_ids,
            rationale=rationale,
            candidate=candidate,
        )
        self._items[proposal.id] = proposal
        return proposal

    def validate(self, proposal_id: str, *, note: str | None = None) -> ImprovementProposal:
        return self._transition(proposal_id, ProposalStatus.VALIDATED, note)

    def approve(self, proposal_id: str, *, note: str | None = None) -> ImprovementProposal:
        item = self._items[proposal_id]
        if item.status != ProposalStatus.VALIDATED:
            raise ValueError("only validated proposals may be approved")
        return self._transition(proposal_id, ProposalStatus.APPROVED, note)

    def reject(self, proposal_id: str, *, note: str | None = None) -> ImprovementProposal:
        return self._transition(proposal_id, ProposalStatus.REJECTED, note)

    def promote(self, proposal_id: str) -> ImprovementProposal:
        item = self._items[proposal_id]
        if item.status != ProposalStatus.APPROVED:
            raise ValueError("promotion requires explicit approval")
        return self._transition(proposal_id, ProposalStatus.PROMOTED, item.review_note)

    def _transition(
        self, proposal_id: str, status: ProposalStatus, note: str | None
    ) -> ImprovementProposal:
        item = self._items[proposal_id]
        updated = replace(item, status=status, review_note=note)
        self._items[proposal_id] = updated
        return updated
