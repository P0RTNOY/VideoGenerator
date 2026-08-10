"""Typed ad-campaign domain and deterministic claim-policy validation."""

from pathlib import Path
import re
from typing import Dict, List, Literal, Optional, Sequence, Union

from pydantic import Field, field_validator, model_validator

from ad_claims import (
    AdClaimCandidate,
    AdClaimSelectionError,
    DuplicateAdClaimCandidateError,
    ProductAdClaimSelection,
    resolve_ad_claim_selection,
)
from claim_review import ClaimReviewItem
from product import NonEmptyStr, Product, ProductModel
from product_claim_verification import ClaimVerificationStatus


AdFormat = Literal["vertical_social"]
AspectRatio = Literal["9:16"]
CreativeStyle = Literal[
    "ugc",
    "lifestyle",
    "premium",
    "minimal",
    "cinematic",
    "product_demo",
]

_LANGUAGE_TAG_PATTERN = re.compile(
    r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
)


class CampaignClaimSnapshot(ProductModel):
    """Minimal selected-claim wording and status frozen for a campaign."""

    claim_id: NonEmptyStr
    display_text: NonEmptyStr
    verification_status_at_selection: ClaimVerificationStatus
    context_text: Optional[NonEmptyStr] = None


class AdCampaign(ProductModel):
    """Product-bound creative intent without generated media or prompts."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    claim_selection: ProductAdClaimSelection
    ad_format: AdFormat
    aspect_ratio: AspectRatio
    target_duration_seconds: int = Field(ge=5, le=60, strict=True)
    creative_style: CreativeStyle
    environment: NonEmptyStr
    language_tag: NonEmptyStr
    creative_brief: Optional[NonEmptyStr] = None
    claim_snapshots: List[CampaignClaimSnapshot] = Field(default_factory=list)
    acknowledged_ambiguous_claim_ids: List[NonEmptyStr] = Field(
        default_factory=list
    )

    @field_validator("language_tag")
    @classmethod
    def require_bcp47_shaped_language_tag(cls, value: str) -> str:
        if not _LANGUAGE_TAG_PATTERN.fullmatch(value):
            raise ValueError("language_tag must be a conservative BCP47-shaped tag")
        return value

    @model_validator(mode="after")
    def require_exact_snapshot_and_acknowledgement_ids(self) -> "AdCampaign":
        selected_ids = self.claim_selection.selected_claim_ids
        snapshot_ids = [snapshot.claim_id for snapshot in self.claim_snapshots]
        if snapshot_ids != selected_ids:
            raise ValueError(
                "claim snapshot IDs must exactly match selected claim IDs in order"
            )

        acknowledged_ids = self.acknowledged_ambiguous_claim_ids
        if len(acknowledged_ids) != len(set(acknowledged_ids)):
            raise ValueError(
                "acknowledged ambiguous claim IDs must not contain duplicates"
            )
        if not set(acknowledged_ids).issubset(set(selected_ids)):
            raise ValueError(
                "acknowledged ambiguous claim IDs must be selected claim IDs"
            )
        return self


class CampaignContextError(ValueError):
    """Raised when campaign data does not match its product claim context."""


def _resolve_selected_in_selection_order(
    candidates: Sequence[AdClaimCandidate],
    selected_ids: Sequence[str],
) -> List[AdClaimCandidate]:
    try:
        resolved = resolve_ad_claim_selection(candidates, selected_ids)
    except (AdClaimSelectionError, DuplicateAdClaimCandidateError) as error:
        raise CampaignContextError(str(error)) from error
    resolved_by_id = {candidate.id: candidate for candidate in resolved}
    return [resolved_by_id[claim_id] for claim_id in selected_ids]


def create_campaign(
    campaign: AdCampaign,
    product: Product,
    candidates: Sequence[AdClaimCandidate],
    review_items: Sequence[ClaimReviewItem],
) -> AdCampaign:
    """Validate campaign claim policy against current context and copy it."""

    selection = campaign.claim_selection
    if selection.product_id != product.id:
        raise CampaignContextError(
            f"selection product ID {selection.product_id} does not match "
            f"Product ID {product.id}"
        )

    selected_candidates = _resolve_selected_in_selection_order(
        candidates,
        selection.selected_claim_ids,
    )

    reviews_by_id: Dict[str, ClaimReviewItem] = {}
    for item in review_items:
        claim_id = item.candidate.id
        if claim_id in reviews_by_id:
            raise CampaignContextError(f"duplicate claim review item: {claim_id}")
        reviews_by_id[claim_id] = item

    selected_reviews: List[ClaimReviewItem] = []
    for candidate in selected_candidates:
        item = reviews_by_id.get(candidate.id)
        if item is None:
            raise CampaignContextError(
                f"missing claim review item for selected claim: {candidate.id}"
            )
        if item.candidate != candidate:
            raise CampaignContextError(
                f"claim review candidate does not match candidate: {candidate.id}"
            )
        selected_reviews.append(item)

    acknowledged_ids = set(campaign.acknowledged_ambiguous_claim_ids)
    ambiguous_ids = {
        item.candidate.id
        for item in selected_reviews
        if item.verification_status == "ambiguous"
    }
    conflicting_ids = [
        item.candidate.id
        for item in selected_reviews
        if item.verification_status == "conflicting"
    ]
    if conflicting_ids:
        raise CampaignContextError(
            "conflicting claims cannot be selected: " + ", ".join(conflicting_ids)
        )
    missing_acknowledgements = ambiguous_ids - acknowledged_ids
    if missing_acknowledgements:
        raise CampaignContextError(
            "ambiguous claims must be explicitly acknowledged: "
            + ", ".join(sorted(missing_acknowledgements))
        )
    invalid_acknowledgements = acknowledged_ids - ambiguous_ids
    if invalid_acknowledgements:
        raise CampaignContextError(
            "only selected ambiguous claims may be acknowledged: "
            + ", ".join(sorted(invalid_acknowledgements))
        )

    expected_snapshots = [
        CampaignClaimSnapshot(
            claim_id=candidate.id,
            display_text=candidate.display_text,
            context_text=candidate.context_text,
            verification_status_at_selection=item.verification_status,
        )
        for candidate, item in zip(selected_candidates, selected_reviews)
    ]
    if campaign.claim_snapshots != expected_snapshots:
        raise CampaignContextError(
            "campaign claim snapshots do not exactly match current claim context"
        )

    return campaign.model_copy(deep=True)


def save_campaign(
    campaign: AdCampaign,
    path: Union[str, Path] = "campaign.json",
) -> Path:
    """Serialize a campaign to readable UTF-8 JSON and return its path."""

    output_path = Path(path)
    json_text = campaign.model_dump_json(
        indent=2,
        ensure_ascii=False,
        exclude_none=True,
    )
    output_path.write_text(json_text + "\n", encoding="utf-8")
    return output_path


def load_campaign(path: Union[str, Path] = "campaign.json") -> AdCampaign:
    """Load and strictly validate a campaign from UTF-8 JSON."""

    input_path = Path(path)
    return AdCampaign.model_validate_json(input_path.read_text(encoding="utf-8"))
