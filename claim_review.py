"""Pure review projection for ad claim candidates and verification results."""

from __future__ import annotations

from typing import List, Sequence

from pydantic import Field

from ad_claims import AdClaimCandidate, DuplicateAdClaimCandidateError
from product import ProductModel
from product_claim_verification import (
    ClaimVerificationStatus,
    ProductClaimVerificationReport,
    resolve_claim_verification_result,
)


class ClaimReviewItem(ProductModel):
    """One candidate with derived corroboration evidence, not approval state."""

    candidate: AdClaimCandidate
    verification_status: ClaimVerificationStatus
    corroborating_source_count: int = Field(ge=0, strict=True)
    conflicting_source_count: int = Field(ge=0, strict=True)
    ambiguous_source_count: int = Field(ge=0, strict=True)


def project_claim_review_items(
    candidates: Sequence[AdClaimCandidate],
    report: ProductClaimVerificationReport,
) -> List[ClaimReviewItem]:
    """Join candidates to exact verification IDs in candidate source order."""

    items = []
    seen_ids = set()
    for candidate in candidates:
        if candidate.id in seen_ids:
            raise DuplicateAdClaimCandidateError(
                f"duplicate ad claim candidate ID: {candidate.id}"
            )
        seen_ids.add(candidate.id)
        verification = resolve_claim_verification_result(report, candidate.id)
        agreeing_sources = {
            observation.requested_url
            for observation in verification.observations
            if observation.comparison == "agrees"
        }
        conflicting_sources = {
            observation.requested_url
            for observation in verification.observations
            if observation.comparison == "conflicts"
        }
        ambiguous_sources = {
            observation.requested_url
            for observation in verification.observations
            if observation.comparison == "ambiguous"
        }
        items.append(
            ClaimReviewItem(
                candidate=candidate.model_copy(deep=True),
                verification_status=verification.status,
                corroborating_source_count=len(agreeing_sources),
                conflicting_source_count=len(conflicting_sources),
                ambiguous_source_count=len(ambiguous_sources),
            )
        )
    return items
