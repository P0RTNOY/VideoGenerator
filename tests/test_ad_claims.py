import json
import unittest
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import ValidationError

from ad_claims import (
    AdClaimCandidate,
    AdClaimSelectionError,
    DuplicateAdClaimCandidateError,
    generate_ad_claim_candidates,
    resolve_ad_claim_selection,
)
from product import ClaimEvidence, Product, ProductFeature, ProductSpec


def sample_product(
    *,
    specs: Optional[List[ProductSpec]] = None,
    features: Optional[List[ProductFeature]] = None,
) -> Product:
    return Product(
        id="example-speaker",
        name="Example Speaker",
        brand="Example Audio",
        category="Portable Speakers",
        specs=specs or [],
        features=features or [],
    )


class AdClaimCandidateTests(unittest.TestCase):
    def test_specs_and_features_use_stable_namespaced_source_ids(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="IP67",
                    source_url="https://example.com/speaker",
                )
            ],
            features=[
                ProductFeature(
                    id="floating-design",
                    name="Floating design",
                    source_url="https://example.com/speaker",
                )
            ],
        )

        candidates = generate_ad_claim_candidates(product)

        self.assertEqual(
            [candidate.id for candidate in candidates],
            ["spec:water_resistance", "feature:floating-design"],
        )
        self.assertEqual(candidates[0].claim_type, "spec")
        self.assertEqual(candidates[0].source_id, "water_resistance")
        self.assertEqual(candidates[0].source_url, "https://example.com/speaker")
        self.assertEqual(candidates[1].claim_type, "feature")
        self.assertEqual(candidates[1].source_id, "floating-design")
        self.assertEqual(candidates[1].source_url, "https://example.com/speaker")

    def test_common_spec_display_text_preserves_qualifications(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="IP67",
                ),
                ProductSpec(
                    key="playback_time",
                    label="Playback Time",
                    value="up to 20 hours",
                ),
                ProductSpec(
                    key="output_power",
                    label="Output Power",
                    value=5,
                    unit="W",
                ),
                ProductSpec(
                    key="immersion_protection",
                    label="Immersion Protection",
                    value="up to 3.3 feet for 30 minutes",
                ),
            ]
        )

        candidates = generate_ad_claim_candidates(product)

        self.assertEqual(
            [candidate.display_text for candidate in candidates],
            [
                "IP67 water resistance",
                "Up to 20 hours playback",
                "5W output",
                "Immersion protection: up to 3.3 feet for 30 minutes",
            ],
        )

    def test_playback_formatter_changes_only_the_initial_character(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(
                    key="playback_time",
                    label="Playback Time",
                    value="approximately 8 HOURS with TWS",
                )
            ]
        )

        candidate = generate_ad_claim_candidates(product)[0]

        self.assertEqual(
            candidate.display_text,
            "Approximately 8 HOURS with TWS playback",
        )

    def test_generic_spec_values_are_formatted_without_marketing_rewrites(self) -> None:
        cases = [
            ("Material", "recycled plastic", None, "Material: recycled plastic"),
            ("Driver Count", 2, None, "Driver Count: 2"),
            ("Weight", 1.5, "kg", "Weight: 1.5 kg"),
            ("Voice Assistant", True, None, "Voice Assistant: Yes"),
            ("Voice Assistant", False, None, "Voice Assistant: No"),
        ]

        for index, (label, value, unit, expected) in enumerate(cases):
            with self.subTest(value=value):
                product = sample_product(
                    specs=[
                        ProductSpec(
                            key=f"generic_{index}",
                            label=label,
                            value=value,
                            unit=unit,
                        )
                    ]
                )

                candidate = generate_ad_claim_candidates(product)[0]

                self.assertEqual(candidate.display_text, expected)

    def test_feature_name_and_description_become_display_and_context(self) -> None:
        description = (
            "Connect two speakers for stereo separation. "
            "Hold the pairing button for 2 seconds."
        )
        product = sample_product(
            features=[
                ProductFeature(
                    id="true-wireless-stereo",
                    name="True Wireless Stereo",
                    description=description,
                ),
                ProductFeature(
                    id="floating-design",
                    name="Floating design",
                ),
            ]
        )

        candidates = generate_ad_claim_candidates(product)

        self.assertEqual(candidates[0].display_text, "True Wireless Stereo")
        self.assertEqual(candidates[0].context_text, description)
        self.assertEqual(candidates[1].display_text, "Floating design")
        self.assertIsNone(candidates[1].context_text)

    def test_confidence_and_verification_are_copied_without_inference(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(
                    key="known",
                    label="Known",
                    value="value",
                    confidence=0.65,
                    verified=False,
                ),
                ProductSpec(
                    key="unknown-confidence",
                    label="Unknown Confidence",
                    value="value",
                    confidence=None,
                    verified=True,
                ),
            ]
        )

        candidates = generate_ad_claim_candidates(product)

        self.assertEqual(candidates[0].confidence, 0.65)
        self.assertFalse(candidates[0].verified)
        self.assertIsNone(candidates[1].confidence)
        self.assertTrue(candidates[1].verified)

    def test_evidence_is_deep_copied_with_timestamp_preserved(self) -> None:
        timestamp = datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc)
        evidence = ClaimEvidence(
            raw_text="Playback: Up to 20 hours",
            source_url="https://example.com/speaker",
            extraction_method="primary_product_claim_list",
            extracted_at=timestamp,
        )
        product = sample_product(
            specs=[
                ProductSpec(
                    key="playback_time",
                    label="Playback Time",
                    value="up to 20 hours",
                    evidence=evidence,
                )
            ]
        )

        candidate = generate_ad_claim_candidates(product)[0]

        self.assertEqual(candidate.evidence, product.specs[0].evidence)
        self.assertIsNot(candidate.evidence, product.specs[0].evidence)
        self.assertEqual(candidate.evidence.extracted_at, timestamp)

        candidate.evidence.raw_text = "changed candidate evidence"
        self.assertEqual(
            product.specs[0].evidence.raw_text,
            "Playback: Up to 20 hours",
        )

    def test_source_url_is_preserved_when_evidence_is_missing(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(
                    key="legacy",
                    label="Legacy Fact",
                    value="present",
                    source_url="https://example.com/legacy",
                )
            ]
        )

        candidate = generate_ad_claim_candidates(product)[0]

        self.assertEqual(candidate.source_url, "https://example.com/legacy")
        self.assertIsNone(candidate.evidence)

    def test_specs_remain_before_features_in_product_source_order(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(key="second", label="Second", value=2),
                ProductSpec(key="first", label="First", value=1),
            ],
            features=[
                ProductFeature(id="feature-b", name="Feature B"),
                ProductFeature(id="feature-a", name="Feature A"),
            ],
        )

        candidates = generate_ad_claim_candidates(product)

        self.assertEqual(
            [candidate.id for candidate in candidates],
            ["spec:second", "spec:first", "feature:feature-b", "feature:feature-a"],
        )

    def test_generation_does_not_mutate_product(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(
                    key="playback_time",
                    label="Playback Time",
                    value="up to 20 hours",
                    evidence=ClaimEvidence(
                        raw_text="Playback: Up to 20 hours",
                        source_url="https://example.com/speaker",
                        extraction_method="manual",
                    ),
                )
            ],
            features=[ProductFeature(id="floating-design", name="Floating design")],
        )
        before = product.model_dump(mode="json")

        generate_ad_claim_candidates(product)

        self.assertEqual(product.model_dump(mode="json"), before)

    def test_empty_product_produces_no_candidates(self) -> None:
        self.assertEqual(generate_ad_claim_candidates(sample_product()), [])

    def test_legacy_product_json_without_evidence_is_supported(self) -> None:
        product = Product.model_validate_json(
            json.dumps(
                {
                    "id": "legacy-speaker",
                    "name": "Legacy Speaker",
                    "brand": "Example Audio",
                    "category": "Portable Speakers",
                    "specs": [
                        {
                            "key": "playback_time",
                            "label": "Playback Time",
                            "value": "up to 10 hours",
                            "verified": False,
                        }
                    ],
                    "features": [
                        {
                            "id": "carrying-loop",
                            "name": "Carrying loop",
                            "verified": False,
                        }
                    ],
                }
            )
        )

        candidates = generate_ad_claim_candidates(product)

        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(candidate.evidence is None for candidate in candidates))

    def test_duplicate_candidate_ids_fail_clearly(self) -> None:
        duplicate_specs = sample_product(
            specs=[
                ProductSpec(key="weight", label="Weight", value=1),
                ProductSpec(key="weight", label="Weight", value=2),
            ]
        )
        duplicate_features = sample_product(
            features=[
                ProductFeature(id="portable", name="Portable"),
                ProductFeature(id="portable", name="Portable with strap"),
            ]
        )

        for product, candidate_id in (
            (duplicate_specs, "spec:weight"),
            (duplicate_features, "feature:portable"),
        ):
            with self.subTest(candidate_id=candidate_id):
                with self.assertRaisesRegex(
                    DuplicateAdClaimCandidateError,
                    candidate_id,
                ):
                    generate_ad_claim_candidates(product)

    def test_similar_ids_and_cross_type_ids_do_not_collide(self) -> None:
        product = sample_product(
            specs=[
                ProductSpec(key="water", label="Water", value="IP67"),
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="IP68",
                ),
                ProductSpec(key="shared", label="Shared", value="spec"),
            ],
            features=[ProductFeature(id="shared", name="Shared feature")],
        )

        candidates = generate_ad_claim_candidates(product)

        self.assertEqual(
            [candidate.id for candidate in candidates],
            [
                "spec:water",
                "spec:water_resistance",
                "spec:shared",
                "feature:shared",
            ],
        )

    def test_candidate_json_round_trip_and_unknown_field_validation(self) -> None:
        candidate = generate_ad_claim_candidates(
            sample_product(
                features=[ProductFeature(id="floating-design", name="Floating design")]
            )
        )[0]

        restored = AdClaimCandidate.model_validate_json(candidate.model_dump_json())

        self.assertEqual(restored, candidate)
        invalid = candidate.model_dump()
        invalid["selectable"] = True
        with self.assertRaises(ValidationError):
            AdClaimCandidate.model_validate(invalid)


class AdClaimSelectionTests(unittest.TestCase):
    def candidate_set(self) -> list[AdClaimCandidate]:
        return generate_ad_claim_candidates(
            sample_product(
                specs=[
                    ProductSpec(
                        key="water_resistance",
                        label="Water Resistance",
                        value="IP67",
                    ),
                    ProductSpec(
                        key="playback_time",
                        label="Playback Time",
                        value="up to 20 hours",
                    ),
                ],
                features=[
                    ProductFeature(id="floating-design", name="Floating design"),
                    ProductFeature(
                        id="true-wireless-stereo",
                        name="True Wireless Stereo",
                    ),
                ],
            )
        )

    def test_selection_resolves_in_candidate_source_order(self) -> None:
        candidates = self.candidate_set()

        selected = resolve_ad_claim_selection(
            candidates,
            ["feature:floating-design", "spec:playback_time"],
        )

        self.assertEqual(
            [candidate.id for candidate in selected],
            ["spec:playback_time", "feature:floating-design"],
        )

    def test_empty_selection_returns_empty_list(self) -> None:
        self.assertEqual(resolve_ad_claim_selection(self.candidate_set(), []), [])

    def test_unknown_selected_id_fails_clearly(self) -> None:
        with self.assertRaisesRegex(
            AdClaimSelectionError,
            "unknown ad claim candidate ID: spec:not-present",
        ):
            resolve_ad_claim_selection(
                self.candidate_set(),
                ["spec:not-present"],
            )

    def test_duplicate_selected_id_fails_clearly(self) -> None:
        with self.assertRaisesRegex(
            AdClaimSelectionError,
            "duplicate selected ad claim candidate ID: spec:playback_time",
        ):
            resolve_ad_claim_selection(
                self.candidate_set(),
                ["spec:playback_time", "spec:playback_time"],
            )

    def test_duplicate_available_candidate_id_fails_clearly(self) -> None:
        candidate = self.candidate_set()[0]

        with self.assertRaisesRegex(
            DuplicateAdClaimCandidateError,
            candidate.id,
        ):
            resolve_ad_claim_selection([candidate, candidate], [candidate.id])

    def test_selection_returns_deep_copies_without_mutating_candidates(self) -> None:
        evidence = ClaimEvidence(
            raw_text="Playback: Up to 20 hours",
            source_url="https://example.com/speaker",
            extraction_method="manual",
        )
        candidates = generate_ad_claim_candidates(
            sample_product(
                specs=[
                    ProductSpec(
                        key="playback_time",
                        label="Playback Time",
                        value="up to 20 hours",
                        evidence=evidence,
                    )
                ]
            )
        )
        before = candidates[0].model_dump(mode="json")

        selected = resolve_ad_claim_selection(candidates, [candidates[0].id])

        self.assertEqual(selected[0], candidates[0])
        self.assertIsNot(selected[0], candidates[0])
        self.assertIsNot(selected[0].evidence, candidates[0].evidence)
        self.assertEqual(candidates[0].model_dump(mode="json"), before)

        selected[0].evidence.raw_text = "changed selection evidence"
        self.assertEqual(
            candidates[0].evidence.raw_text,
            "Playback: Up to 20 hours",
        )

    def test_blank_or_non_string_selected_ids_fail_cleanly(self) -> None:
        for selected_id in ("", "   ", 123):
            with self.subTest(selected_id=selected_id):
                with self.assertRaisesRegex(
                    AdClaimSelectionError,
                    "selected ad claim candidate IDs must be non-empty strings",
                ):
                    resolve_ad_claim_selection(
                        self.candidate_set(),
                        [selected_id],
                    )


if __name__ == "__main__":
    unittest.main()
