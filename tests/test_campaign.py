import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from ad_claims import (
    AdClaimCandidate,
    ProductAdClaimSelection,
    generate_ad_claim_candidates,
)
from campaign import (
    AdCampaign,
    CampaignClaimSnapshot,
    CampaignContextError,
    create_campaign,
    load_campaign,
    save_campaign,
)
from claim_review import ClaimReviewItem
from product import Product, ProductFeature, ProductSpec


def sample_product() -> Product:
    return Product(
        id="speaker-green",
        name="Select 4 Go",
        brand="Soundcore",
        category="Portable Bluetooth Speaker",
        specs=[
            ProductSpec(
                key="playback_time",
                label="Playback time",
                value="Up to 20",
                unit="hours",
            ),
            ProductSpec(
                key="water_resistance",
                label="Water resistance",
                value="IP67",
            ),
        ],
        features=[
            ProductFeature(
                id="floating-design",
                name="Floating design",
                description="Designed to float in water.",
            )
        ],
    )


def review(candidate: AdClaimCandidate, status: str) -> ClaimReviewItem:
    return ClaimReviewItem(
        candidate=candidate.model_copy(deep=True),
        verification_status=status,
        corroborating_source_count=1 if status == "corroborated" else 0,
        conflicting_source_count=1 if status == "conflicting" else 0,
        ambiguous_source_count=1 if status == "ambiguous" else 0,
    )


def campaign_for(
    selection: ProductAdClaimSelection,
    candidates: list,
    reviews: list,
    **overrides: object,
) -> AdCampaign:
    candidates_by_id = {candidate.id: candidate for candidate in candidates}
    reviews_by_id = {item.candidate.id: item for item in reviews}
    values = {
        "id": "summer-social",
        "claim_selection": selection,
        "ad_format": "vertical_social",
        "aspect_ratio": "9:16",
        "target_duration_seconds": 8,
        "creative_style": "ugc",
        "environment": "Sunny poolside",
        "language_tag": "en-US",
        "creative_brief": "Energetic and factual.",
        "claim_snapshots": [
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
        "acknowledged_ambiguous_claim_ids": [
            claim_id
            for claim_id in selection.selected_claim_ids
            if reviews_by_id[claim_id].verification_status == "ambiguous"
        ],
    }
    values.update(overrides)
    return AdCampaign.model_validate(values)


class CampaignDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product = sample_product()
        self.candidates = generate_ad_claim_candidates(self.product)
        self.by_id = {candidate.id: candidate for candidate in self.candidates}

    def selection(self, *claim_ids: str) -> ProductAdClaimSelection:
        return ProductAdClaimSelection(
            product_id=self.product.id,
            selected_claim_ids=list(claim_ids),
        )

    def test_create_campaign_accepts_supported_and_corroborated_claims(self) -> None:
        selection = self.selection(
            "feature:floating-design",
            "spec:playback_time",
        )
        reviews = [
            review(self.by_id["spec:playback_time"], "corroborated"),
            review(self.by_id["feature:floating-design"], "supported"),
        ]
        proposed = campaign_for(selection, self.candidates, reviews)

        created = create_campaign(
            proposed,
            self.product,
            self.candidates,
            reviews,
        )

        self.assertEqual(created, proposed)
        self.assertIsNot(created, proposed)
        self.assertEqual(
            [snapshot.claim_id for snapshot in created.claim_snapshots],
            selection.selected_claim_ids,
        )
        self.assertEqual(
            created.claim_snapshots[0].context_text,
            "Designed to float in water.",
        )

    def test_context_requires_matching_product_and_selected_candidates(self) -> None:
        selection = self.selection("spec:playback_time")
        reviews = [review(self.by_id["spec:playback_time"], "supported")]
        proposed = campaign_for(selection, self.candidates, reviews)

        wrong_product = self.product.model_copy(
            update={"id": "different-product"}, deep=True
        )
        with self.assertRaises(CampaignContextError):
            create_campaign(proposed, wrong_product, self.candidates, reviews)

        missing_candidates = [self.by_id["feature:floating-design"]]
        with self.assertRaises(CampaignContextError):
            create_campaign(proposed, self.product, missing_candidates, reviews)

    def test_context_requires_complete_unique_matching_review_items(self) -> None:
        selection = self.selection("spec:playback_time")
        selected = self.by_id["spec:playback_time"]
        reviews = [review(selected, "supported")]
        proposed = campaign_for(selection, self.candidates, reviews)

        with self.assertRaisesRegex(CampaignContextError, "review"):
            create_campaign(proposed, self.product, self.candidates, [])
        with self.assertRaisesRegex(CampaignContextError, "review"):
            create_campaign(
                proposed,
                self.product,
                self.candidates,
                [reviews[0], reviews[0].model_copy(deep=True)],
            )

        mismatched_candidate = selected.model_copy(
            update={"display_text": "Changed upstream text"}, deep=True
        )
        with self.assertRaisesRegex(CampaignContextError, "candidate"):
            create_campaign(
                proposed,
                self.product,
                self.candidates,
                [review(mismatched_candidate, "supported")],
            )

    def test_conflicting_claims_are_always_rejected(self) -> None:
        selection = self.selection("spec:playback_time")
        reviews = [review(self.by_id["spec:playback_time"], "conflicting")]
        proposed = campaign_for(selection, self.candidates, reviews)

        with self.assertRaisesRegex(CampaignContextError, "conflicting"):
            create_campaign(proposed, self.product, self.candidates, reviews)

    def test_ambiguous_claim_requires_exact_acknowledgement(self) -> None:
        claim_id = "spec:playback_time"
        selection = self.selection(claim_id)
        reviews = [review(self.by_id[claim_id], "ambiguous")]
        acknowledged = campaign_for(selection, self.candidates, reviews)

        self.assertEqual(
            create_campaign(
                acknowledged,
                self.product,
                self.candidates,
                reviews,
            ),
            acknowledged,
        )
        unacknowledged = acknowledged.model_copy(
            update={"acknowledged_ambiguous_claim_ids": []}, deep=True
        )
        with self.assertRaisesRegex(CampaignContextError, "acknowledged"):
            create_campaign(
                unacknowledged,
                self.product,
                self.candidates,
                reviews,
            )

    def test_acknowledgements_cannot_cover_supported_or_unselected_claims(self) -> None:
        selected_id = "spec:playback_time"
        selection = self.selection(selected_id)
        reviews = [review(self.by_id[selected_id], "supported")]
        with self.assertRaises(ValidationError):
            campaign_for(
                selection,
                self.candidates,
                reviews,
                acknowledged_ambiguous_claim_ids=["spec:water_resistance"],
            )

        proposed = campaign_for(
            selection,
            self.candidates,
            reviews,
            acknowledged_ambiguous_claim_ids=[selected_id],
        )
        with self.assertRaisesRegex(CampaignContextError, "ambiguous"):
            create_campaign(proposed, self.product, self.candidates, reviews)

    def test_snapshot_ids_must_exactly_match_selection_in_order(self) -> None:
        selection = self.selection(
            "spec:playback_time",
            "feature:floating-design",
        )
        reviews = [
            review(self.by_id[claim_id], "supported")
            for claim_id in selection.selected_claim_ids
        ]
        valid = campaign_for(selection, self.candidates, reviews).model_dump()

        for snapshots in (
            valid["claim_snapshots"][:1],
            valid["claim_snapshots"] + [valid["claim_snapshots"][0]],
            list(reversed(valid["claim_snapshots"])),
        ):
            malformed = deepcopy(valid)
            malformed["claim_snapshots"] = snapshots
            with self.subTest(snapshots=snapshots):
                with self.assertRaises(ValidationError):
                    AdCampaign.model_validate(malformed)

    def test_snapshot_content_and_status_must_match_current_context(self) -> None:
        claim_id = "feature:floating-design"
        selection = self.selection(claim_id)
        reviews = [review(self.by_id[claim_id], "supported")]
        valid = campaign_for(selection, self.candidates, reviews)

        changes = (
            {"display_text": "Drifted display"},
            {"context_text": "Drifted context"},
            {"verification_status_at_selection": "corroborated"},
        )
        for change in changes:
            snapshot = valid.claim_snapshots[0].model_copy(update=change, deep=True)
            proposed = valid.model_copy(
                update={"claim_snapshots": [snapshot]}, deep=True
            )
            with self.subTest(change=change):
                with self.assertRaisesRegex(CampaignContextError, "snapshot"):
                    create_campaign(
                        proposed,
                        self.product,
                        self.candidates,
                        reviews,
                    )

    def test_campaign_fields_are_strict_and_bounded(self) -> None:
        selection = self.selection()
        base = campaign_for(selection, self.candidates, [])
        data = base.model_dump()

        for duration in (5, 60):
            valid = deepcopy(data)
            valid["target_duration_seconds"] = duration
            self.assertEqual(
                AdCampaign.model_validate(valid).target_duration_seconds,
                duration,
            )
        for duration in (4, 61, 8.0, True):
            invalid = deepcopy(data)
            invalid["target_duration_seconds"] = duration
            with self.subTest(duration=duration):
                with self.assertRaises(ValidationError):
                    AdCampaign.model_validate(invalid)

        invalid_values = {
            "ad_format": "landscape",
            "aspect_ratio": "16:9",
            "creative_style": "documentary",
            "environment": "   ",
        }
        for field, value in invalid_values.items():
            invalid = deepcopy(data)
            invalid[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    AdCampaign.model_validate(invalid)

    def test_language_tags_are_conservative_and_creative_brief_is_optional(self) -> None:
        selection = self.selection()
        base = campaign_for(selection, self.candidates, []).model_dump()
        for language_tag in ("en", "en-US", "he", "he-IL"):
            data = deepcopy(base)
            data["language_tag"] = language_tag
            data.pop("creative_brief", None)
            self.assertIsNone(AdCampaign.model_validate(data).creative_brief)
        for language_tag in ("", " ", "en_US", "english", "en-", "-US"):
            data = deepcopy(base)
            data["language_tag"] = language_tag
            with self.subTest(language_tag=language_tag):
                with self.assertRaises(ValidationError):
                    AdCampaign.model_validate(data)

    def test_json_persistence_is_readable_utf8_and_strict(self) -> None:
        selection = self.selection("feature:floating-design")
        reviews = [review(self.by_id["feature:floating-design"], "supported")]
        original = campaign_for(
            selection,
            self.candidates,
            reviews,
            environment="בריכה שטופת שמש",
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.json"
            returned_path = save_campaign(original, path)
            raw = path.read_text(encoding="utf-8")
            restored = load_campaign(path)

            self.assertEqual(returned_path, path)
            self.assertIn("\n  \"schema_version\"", raw)
            self.assertIn("בריכה שטופת שמש", raw)
            self.assertEqual(restored, original)

            malformed = json.loads(raw)
            malformed["unexpected"] = True
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_campaign(path)

            for malformed in (
                {**original.model_dump(), "schema_version": 2},
                {
                    **original.model_dump(),
                    "claim_snapshots": [
                        {
                            **original.claim_snapshots[0].model_dump(),
                            "confidence": 1.0,
                        }
                    ],
                },
            ):
                with self.subTest(malformed=malformed):
                    with self.assertRaises(ValidationError):
                        AdCampaign.model_validate(malformed)

    def test_creation_does_not_mutate_any_input_or_legacy_claim_models(self) -> None:
        selection = self.selection("spec:playback_time")
        reviews = [review(self.by_id["spec:playback_time"], "supported")]
        proposed = campaign_for(selection, self.candidates, reviews)
        before = deepcopy((self.product, self.candidates, reviews, proposed))

        created = create_campaign(
            proposed,
            self.product,
            self.candidates,
            reviews,
        )

        self.assertEqual((self.product, self.candidates, reviews, proposed), before)
        created.claim_snapshots[0].display_text = "Changed result"
        self.assertNotEqual(
            created.claim_snapshots[0].display_text,
            proposed.claim_snapshots[0].display_text,
        )
        restored_selection = ProductAdClaimSelection.model_validate_json(
            selection.model_dump_json()
        )
        restored_candidate = AdClaimCandidate.model_validate_json(
            self.candidates[0].model_dump_json()
        )
        self.assertEqual(restored_selection, selection)
        self.assertEqual(restored_candidate, self.candidates[0])


if __name__ == "__main__":
    unittest.main()
