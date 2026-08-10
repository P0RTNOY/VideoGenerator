"""In-memory Soundcore Campaign and Storyboard domain demonstration."""

import json
from typing import List, Tuple

from ad_claims import (
    AdClaimCandidate,
    ProductAdClaimSelection,
    generate_ad_claim_candidates,
)
from campaign import AdCampaign, CampaignClaimSnapshot, create_campaign
from claim_review import ClaimReviewItem
from product import Product, ProductFeature, ProductSpec
from storyboard import (
    Storyboard,
    StoryboardShot,
    StoryboardTextOverlay,
    validate_storyboard,
)


_SELECTED_CLAIM_IDS = [
    "spec:water_resistance",
    "spec:playback_time",
    "feature:floating-design",
]


def _supported_reviews(
    candidates: List[AdClaimCandidate],
) -> List[ClaimReviewItem]:
    return [
        ClaimReviewItem(
            candidate=candidate.model_copy(deep=True),
            verification_status="supported",
            corroborating_source_count=0,
            conflicting_source_count=0,
            ambiguous_source_count=0,
        )
        for candidate in candidates
    ]


def build_soundcore_campaign_storyboard() -> Tuple[AdCampaign, Storyboard]:
    """Build and context-validate the approved manual Soundcore domain demo."""

    product = Product(
        id="soundcore-com-group-products-a31x1021",
        name="Select 4 Go",
        brand="Soundcore",
        category="Portable Bluetooth Speaker",
        specs=[
            ProductSpec(
                key="water_resistance",
                label="Water resistance",
                value="IP67",
            ),
            ProductSpec(
                key="playback_time",
                label="Playback time",
                value="Up to 20",
                unit="hours",
            ),
        ],
        features=[
            ProductFeature(
                id="floating-design",
                name="Floating design",
            )
        ],
    )
    candidates = generate_ad_claim_candidates(product)
    candidates_by_id = {candidate.id: candidate for candidate in candidates}
    reviews = _supported_reviews(candidates)
    reviews_by_id = {item.candidate.id: item for item in reviews}
    selection = ProductAdClaimSelection(
        product_id=product.id,
        selected_claim_ids=_SELECTED_CLAIM_IDS,
    )
    proposed_campaign = AdCampaign(
        id="soundcore-select-4-go-summer",
        claim_selection=selection,
        ad_format="vertical_social",
        aspect_ratio="9:16",
        target_duration_seconds=8,
        creative_style="ugc",
        environment="Sunny poolside",
        language_tag="en",
        claim_snapshots=[
            CampaignClaimSnapshot(
                claim_id=claim_id,
                display_text=candidates_by_id[claim_id].display_text,
                context_text=candidates_by_id[claim_id].context_text,
                verification_status_at_selection=(
                    reviews_by_id[claim_id].verification_status
                ),
            )
            for claim_id in selection.selected_claim_ids
        ],
    )
    campaign = create_campaign(
        proposed_campaign,
        product,
        candidates,
        reviews,
    )

    proposed_storyboard = Storyboard(
        id="soundcore-select-4-go-summer-board",
        campaign_id=campaign.id,
        shots=[
            StoryboardShot(
                id="hook",
                purpose="hook",
                duration_seconds=2.5,
                selected_claim_ids=[],
                visual_description="Speaker revealed in a sunny poolside setting.",
                text_overlays=[
                    StoryboardTextOverlay(
                        kind="creative",
                        text="SMALL SPEAKER. BIG ENERGY.",
                    )
                ],
            ),
            StoryboardShot(
                id="water-feature",
                purpose="feature",
                duration_seconds=2.5,
                selected_claim_ids=[
                    "spec:water_resistance",
                    "feature:floating-design",
                ],
                visual_description="Speaker floats at the pool surface.",
                text_overlays=[
                    StoryboardTextOverlay(
                        kind="creative",
                        text="MADE FOR SUMMER.",
                    )
                ],
            ),
            StoryboardShot(
                id="poolside-lifestyle",
                purpose="lifestyle",
                duration_seconds=3.0,
                selected_claim_ids=["spec:playback_time"],
                visual_description="Friends listen together beside the pool.",
                text_overlays=[
                    StoryboardTextOverlay(
                        kind="claim",
                        claim_id="spec:playback_time",
                    ),
                    StoryboardTextOverlay(
                        kind="creative",
                        text="TAKE IT ANYWHERE.",
                    ),
                    StoryboardTextOverlay(kind="product_name"),
                ],
            ),
        ],
    )
    storyboard = validate_storyboard(proposed_storyboard, campaign)
    return campaign, storyboard


def main() -> None:
    campaign, storyboard = build_soundcore_campaign_storyboard()
    print(
        json.dumps(
            {
                "campaign": campaign.model_dump(exclude_none=True),
                "storyboard": storyboard.model_dump(exclude_none=True),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
