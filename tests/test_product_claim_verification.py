import unittest
from copy import deepcopy
from datetime import datetime, timezone

from pydantic import ValidationError

from ad_claims import (
    ProductAdClaimSelection,
    generate_ad_claim_candidates,
    resolve_product_ad_claim_selection,
)
from product import (
    ClaimEvidence,
    Product,
    ProductFeature,
    ProductImage,
    ProductSource,
    ProductSpec,
)
from product_claim_verification import (
    ClaimVerificationObservation,
    ClaimVerificationResult,
    ClaimVerificationSourceSummary,
    DuplicatePrimaryClaimError,
    ProductClaimVerificationReport,
    generate_product_claim_verification_report,
)
from product_source_research import (
    MultiSourceProductResearch,
    ProductReconciliation,
    ProductResearchFailure,
    SupplementaryProductSourceResult,
)


FIXED_EXTRACTED_AT = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)


def evidence(raw_text: str, source_url: str) -> ClaimEvidence:
    return ClaimEvidence(
        raw_text=raw_text,
        source_url=source_url,
        extraction_method="test_fixture",
        extracted_at=FIXED_EXTRACTED_AT,
    )


def researched_product(
    *,
    product_id: str = "primary-product",
    source_url: str = "https://primary.example/products/select-4-go",
    specs: list = None,
    features: list = None,
) -> Product:
    return Product(
        id=product_id,
        name="Select 4 Go",
        brand="Soundcore",
        category="Portable Bluetooth Speaker",
        specs=specs or [],
        features=features or [],
        sources=[
            ProductSource(
                url=source_url,
                source_type="product_page",
                domain=source_url.split("/")[2],
            )
        ],
    )


def successful_source(
    product: Product,
    *,
    requested_url: str,
    status: str = "matched",
    relationship: str = "exact_product",
) -> SupplementaryProductSourceResult:
    return SupplementaryProductSourceResult(
        requested_url=requested_url,
        product=product,
        reconciliation=ProductReconciliation(
            status=status,
            relationship=relationship if status == "matched" else None,
            reason="test_reconciliation",
        ),
    )


def report_for_specs(
    primary_spec: ProductSpec,
    supplementary_specs: list,
    *,
    relationships: list = None,
) -> ProductClaimVerificationReport:
    primary_url = "https://primary.example/product"
    primary = researched_product(source_url=primary_url, specs=[primary_spec])
    relationships = relationships or ["exact_product"] * len(supplementary_specs)
    sources = []
    for index, (supplementary_spec, relationship) in enumerate(
        zip(supplementary_specs, relationships)
    ):
        source_url = f"https://source-{index}.test/product"
        supplementary = researched_product(
            product_id=f"supplementary-{index}",
            source_url=source_url,
            specs=[supplementary_spec],
        )
        sources.append(
            successful_source(
                supplementary,
                requested_url=source_url,
                relationship=relationship,
            )
        )
    return generate_product_claim_verification_report(
        MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=sources,
        )
    )


class ProductClaimVerificationReportTests(unittest.TestCase):
    def test_primary_only_claims_are_supported_in_product_order(self) -> None:
        source_url = "https://primary.example/products/select-4-go"
        primary = researched_product(
            specs=[
                ProductSpec(
                    key="playback_time",
                    label="Playback Time",
                    value="up to 20 hours",
                    source_url=source_url,
                    confidence=0.65,
                    evidence=evidence("Up to 20 hours", source_url),
                )
            ],
            features=[
                ProductFeature(
                    id="floating-design",
                    name="Floating design",
                    source_url=source_url,
                    confidence=0.65,
                    evidence=evidence("Floating design", source_url),
                )
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=source_url,
            primary_product=primary,
        )

        report = generate_product_claim_verification_report(bundle)

        self.assertEqual(report.product_id, primary.id)
        self.assertEqual(report.primary_role, "primary")
        self.assertEqual(
            [result.claim_id for result in report.claim_results],
            ["spec:playback_time", "feature:floating-design"],
        )
        self.assertEqual(
            [result.status for result in report.claim_results],
            ["supported", "supported"],
        )
        self.assertEqual(report.source_summaries, [])

    def test_exact_spec_from_distinct_domain_is_corroborated(self) -> None:
        primary_url = "https://primary.example/products/select-4-go"
        supplementary_url = "https://retailer.test/select-4-go"
        primary = researched_product(
            source_url=primary_url,
            specs=[
                ProductSpec(
                    key="output_power",
                    label="Output Power",
                    value=5,
                    unit="W",
                    source_url=primary_url,
                    confidence=0.95,
                    evidence=evidence("5 W output", primary_url),
                )
            ],
        )
        supplementary = researched_product(
            product_id="supplementary-product",
            source_url=supplementary_url,
            specs=[
                ProductSpec(
                    key="output_power",
                    label="Power",
                    value="5 W",
                    source_url=supplementary_url,
                    confidence=0.75,
                    evidence=evidence("Output power: 5 W", supplementary_url),
                )
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )

        report = generate_product_claim_verification_report(bundle)

        result = report.claim_results[0]
        self.assertEqual(result.status, "corroborated")
        self.assertEqual(len(result.observations), 1)
        observation = result.observations[0]
        self.assertEqual(observation.comparison, "agrees")
        self.assertEqual(observation.claim_id, "spec:output_power")
        self.assertEqual(observation.observed_value, "5 W")
        self.assertEqual(observation.evidence, supplementary.specs[0].evidence)
        self.assertIsNot(observation.evidence, supplementary.specs[0].evidence)
        self.assertEqual(report.source_summaries[0].participation, "admitted")
        self.assertEqual(report.source_summaries[0].source_role, "supplementary")
        self.assertEqual(report.source_summaries[0].final_url, supplementary_url)

    def test_exact_feature_from_distinct_domain_is_corroborated(self) -> None:
        primary_url = "https://primary.example/product"
        supplementary_url = "https://retailer.test/product"
        primary = researched_product(
            source_url=primary_url,
            features=[
                ProductFeature(
                    id="floating-design",
                    name="Floating design",
                    source_url=primary_url,
                )
            ],
        )
        supplementary = researched_product(
            product_id="supplementary-product",
            source_url=supplementary_url,
            features=[
                ProductFeature(
                    id="floating-design",
                    name="floating DESIGN",
                    source_url=supplementary_url,
                )
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )

        result = generate_product_claim_verification_report(bundle).claim_results[0]

        self.assertEqual(result.status, "corroborated")
        self.assertEqual(result.observations[0].comparison, "agrees")

    def test_same_normalized_publisher_does_not_corroborate(self) -> None:
        primary_url = "https://www.example.com/product"
        supplementary_url = "https://shop.example.com/product"
        primary = researched_product(
            source_url=primary_url,
            specs=[
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="IP67",
                )
            ],
        )
        supplementary = researched_product(
            product_id="supplementary-product",
            source_url=supplementary_url,
            specs=[
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="ip67",
                )
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )

        report = generate_product_claim_verification_report(bundle)

        self.assertEqual(report.claim_results[0].status, "supported")
        self.assertEqual(report.claim_results[0].observations, [])
        summary = report.source_summaries[0]
        self.assertEqual(summary.participation, "same_publisher")
        self.assertEqual(summary.normalized_domain, "example.com")

    def test_failed_unmatched_and_ambiguous_sources_do_not_participate(self) -> None:
        primary_url = "https://primary.example/product"
        primary = researched_product(
            source_url=primary_url,
            specs=[
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="IP67",
                )
            ],
        )
        ignored_products = [
            researched_product(
                product_id=f"ignored-{index}",
                source_url=f"https://ignored-{index}.test/product",
                specs=[
                    ProductSpec(
                        key="water_resistance",
                        label="Water Resistance",
                        value="IP67",
                    )
                ],
            )
            for index in range(2)
        ]
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    ignored_products[0],
                    requested_url="https://ignored-0.test/product",
                    status="not_matched",
                ),
                successful_source(
                    ignored_products[1],
                    requested_url="https://ignored-1.test/product",
                    status="ambiguous",
                ),
                SupplementaryProductSourceResult(
                    requested_url="https://failed.test/product",
                    failure=ProductResearchFailure(
                        error_type="ProductFetchError",
                        message="unavailable",
                    ),
                ),
            ],
        )

        report = generate_product_claim_verification_report(bundle)

        self.assertEqual(report.claim_results[0].status, "supported")
        self.assertEqual(report.claim_results[0].observations, [])
        self.assertEqual(
            [summary.participation for summary in report.source_summaries],
            ["not_matched", "ambiguous_reconciliation", "failed"],
        )
        self.assertEqual(
            report.source_summaries[2].failure,
            bundle.supplementary_sources[2].failure,
        )
        self.assertIsNot(
            report.source_summaries[2].failure,
            bundle.supplementary_sources[2].failure,
        )

    def test_trivial_numeric_and_unit_formatting_agrees(self) -> None:
        result = report_for_specs(
            ProductSpec(key="output_power", label="Output", value=20, unit="W"),
            [
                ProductSpec(
                    key="output_power",
                    label="Output",
                    value="20.0 W",
                )
            ],
        ).claim_results[0]

        self.assertEqual(result.status, "corroborated")
        self.assertEqual(result.observations[0].comparison, "agrees")

    def test_unit_conversion_and_numeric_tolerance_are_not_assumed(self) -> None:
        conversion = report_for_specs(
            ProductSpec(key="output_power", label="Output", value=5, unit="W"),
            [
                ProductSpec(
                    key="output_power",
                    label="Output",
                    value=5000,
                    unit="mW",
                )
            ],
        ).claim_results[0]
        tolerance = report_for_specs(
            ProductSpec(
                key="playback_time",
                label="Playback",
                value=20,
                unit="hours",
            ),
            [
                ProductSpec(
                    key="playback_time",
                    label="Playback",
                    value=19.9,
                    unit="hours",
                )
            ],
        ).claim_results[0]

        self.assertEqual(conversion.status, "ambiguous")
        self.assertEqual(conversion.observations[0].comparison, "ambiguous")
        self.assertEqual(tolerance.status, "conflicting")

    def test_qualifier_difference_is_ambiguous_without_being_stripped(self) -> None:
        result = report_for_specs(
            ProductSpec(
                key="playback_time",
                label="Playback",
                value="20 hours",
            ),
            [
                ProductSpec(
                    key="playback_time",
                    label="Playback",
                    value="up to 20 hours",
                )
            ],
        ).claim_results[0]

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.observations[0].comparison, "ambiguous")

    def test_compound_claims_agree_only_as_complete_values(self) -> None:
        exact = report_for_specs(
            ProductSpec(
                key="immersion_protection",
                label="Immersion Protection",
                value="up to 3.3 feet for 30 minutes",
            ),
            [
                ProductSpec(
                    key="immersion_protection",
                    label="Immersion Protection",
                    value="Up to 3.3 feet for 30 minutes",
                )
            ],
        ).claim_results[0]
        different_duration = report_for_specs(
            ProductSpec(
                key="immersion_protection",
                label="Immersion Protection",
                value="up to 3.3 feet for 30 minutes",
            ),
            [
                ProductSpec(
                    key="immersion_protection",
                    label="Immersion Protection",
                    value="up to 3.3 feet for 20 minutes",
                )
            ],
        ).claim_results[0]

        self.assertEqual(exact.status, "corroborated")
        self.assertEqual(different_duration.status, "conflicting")

    def test_feature_context_mismatch_remains_ambiguous(self) -> None:
        primary_url = "https://primary.example/product"
        supplementary_url = "https://retailer.test/product"
        primary = researched_product(
            source_url=primary_url,
            features=[
                ProductFeature(
                    id="true-wireless-stereo",
                    name="True Wireless Stereo",
                    description="Requires two speakers",
                )
            ],
        )
        supplementary = researched_product(
            product_id="supplementary",
            source_url=supplementary_url,
            features=[
                ProductFeature(
                    id="true-wireless-stereo",
                    name="True Wireless Stereo",
                )
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )

        result = generate_product_claim_verification_report(bundle).claim_results[0]

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.observations[0].context, None)

    def test_product_family_corroborates_only_explicit_group_level_claims(self) -> None:
        group_level = report_for_specs(
            ProductSpec(
                key="playback_time",
                label="Playback",
                value="up to 20 hours",
            ),
            [
                ProductSpec(
                    key="playback_time",
                    label="Playback",
                    value="up to 20 hours",
                )
            ],
            relationships=["product_family"],
        ).claim_results[0]
        variant_specific = report_for_specs(
            ProductSpec(key="output_power", label="Output", value=5, unit="W"),
            [
                ProductSpec(
                    key="output_power",
                    label="Output",
                    value=5,
                    unit="W",
                )
            ],
            relationships=["product_family"],
        ).claim_results[0]

        self.assertEqual(group_level.status, "corroborated")
        self.assertEqual(variant_specific.status, "ambiguous")
        self.assertEqual(
            variant_specific.observations[0].comparison,
            "ambiguous",
        )

    def test_any_independent_conflict_overrides_agreement(self) -> None:
        result = report_for_specs(
            ProductSpec(
                key="playback_time",
                label="Playback",
                value="up to 20 hours",
            ),
            [
                ProductSpec(
                    key="playback_time",
                    label="Playback",
                    value="up to 20 hours",
                ),
                ProductSpec(
                    key="playback_time",
                    label="Playback",
                    value="up to 16 hours",
                ),
            ],
        ).claim_results[0]

        self.assertEqual(result.status, "conflicting")
        self.assertEqual(
            [observation.comparison for observation in result.observations],
            ["agrees", "conflicts"],
        )

    def test_missing_and_supplementary_only_claims_are_neutral(self) -> None:
        result = report_for_specs(
            ProductSpec(
                key="playback_time",
                label="Playback",
                value="up to 20 hours",
            ),
            [
                ProductSpec(
                    key="bluetooth_version",
                    label="Bluetooth Version",
                    value="5.4",
                )
            ],
        )

        self.assertEqual(
            [claim.claim_id for claim in result.claim_results],
            ["spec:playback_time"],
        )
        self.assertEqual(result.claim_results[0].status, "supported")
        self.assertEqual(result.claim_results[0].observations, [])

    def test_report_round_trip_is_strict_and_inputs_remain_unchanged(self) -> None:
        primary_url = "https://primary.example/product"
        supplementary_url = "https://retailer.test/product"
        primary = researched_product(
            source_url=primary_url,
            specs=[
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="IP67",
                    source_url=primary_url,
                    evidence=evidence("IP67", primary_url),
                )
            ],
        )
        primary.images.append(
            ProductImage(
                id="primary-image",
                url="https://cdn.example/image.jpg",
                source_url=primary_url,
                source_type="json_ld_product_image",
            )
        )
        supplementary = researched_product(
            product_id="supplementary",
            source_url=supplementary_url,
            specs=[
                ProductSpec(
                    key="water_resistance",
                    label="Water Resistance",
                    value="IP67",
                    source_url=supplementary_url,
                    evidence=evidence("Rated IP67", supplementary_url),
                )
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )
        before = deepcopy(bundle)
        candidate_ids = [
            candidate.id for candidate in generate_ad_claim_candidates(primary)
        ]
        selection = ProductAdClaimSelection(
            product_id=primary.id,
            selected_claim_ids=candidate_ids,
        )

        report = generate_product_claim_verification_report(bundle)
        restored = ProductClaimVerificationReport.model_validate_json(
            report.model_dump_json()
        )
        invalid = report.model_dump(mode="json")
        invalid["verified"] = True

        self.assertEqual(restored, report)
        with self.assertRaises(ValidationError):
            ProductClaimVerificationReport.model_validate(invalid)
        self.assertEqual(bundle, before)
        self.assertEqual(
            report.claim_results[0].primary_evidence.extracted_at,
            FIXED_EXTRACTED_AT,
        )
        self.assertEqual(
            report.claim_results[0].observations[0].evidence.extracted_at,
            FIXED_EXTRACTED_AT,
        )
        self.assertEqual(
            [candidate.id for candidate in generate_ad_claim_candidates(primary)],
            candidate_ids,
        )
        self.assertEqual(
            [candidate.id for candidate in resolve_product_ad_claim_selection(primary, selection)],
            candidate_ids,
        )
        self.assertEqual(primary.images, before.primary_product.images)

    def test_country_code_subdomains_are_same_conservative_publisher(self) -> None:
        primary_url = "https://www.example.co.uk/product"
        supplementary_url = "https://shop.example.co.uk/product"
        primary = researched_product(
            source_url=primary_url,
            specs=[ProductSpec(key="rating", label="Rating", value="IP67")],
        )
        supplementary = researched_product(
            product_id="supplementary",
            source_url=supplementary_url,
            specs=[ProductSpec(key="rating", label="Rating", value="IP67")],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )

        report = generate_product_claim_verification_report(bundle)

        self.assertEqual(report.claim_results[0].status, "supported")
        self.assertEqual(
            report.source_summaries[0].normalized_domain,
            "example.co.uk",
        )
        self.assertEqual(
            report.source_summaries[0].participation,
            "same_publisher",
        )

    def test_product_family_floating_feature_can_corroborate(self) -> None:
        primary_url = "https://primary.example/product"
        supplementary_url = "https://retailer.test/product"
        primary = researched_product(
            source_url=primary_url,
            features=[
                ProductFeature(id="floating-design", name="Floating design")
            ],
        )
        supplementary = researched_product(
            product_id="supplementary",
            source_url=supplementary_url,
            features=[
                ProductFeature(id="floating-design", name="Floating design")
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                    relationship="product_family",
                )
            ],
        )

        result = generate_product_claim_verification_report(bundle).claim_results[0]

        self.assertEqual(result.status, "corroborated")
        self.assertEqual(result.observations[0].relationship, "product_family")

    def test_matching_tws_context_corroborates_without_discarding_condition(self) -> None:
        primary_url = "https://primary.example/product"
        supplementary_url = "https://retailer.test/product"
        feature = ProductFeature(
            id="true-wireless-stereo",
            name="True Wireless Stereo",
            description="Requires two speakers",
        )
        primary = researched_product(
            source_url=primary_url,
            features=[feature],
        )
        supplementary = researched_product(
            product_id="supplementary",
            source_url=supplementary_url,
            features=[feature.model_copy(deep=True)],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )

        result = generate_product_claim_verification_report(bundle).claim_results[0]

        self.assertEqual(result.status, "corroborated")
        self.assertEqual(result.context, "Requires two speakers")
        self.assertEqual(
            result.observations[0].context,
            "Requires two speakers",
        )

    def test_duplicate_supplementary_concepts_cannot_hide_a_conflict(self) -> None:
        primary_url = "https://primary.example/product"
        supplementary_url = "https://retailer.test/product"
        primary = researched_product(
            source_url=primary_url,
            specs=[
                ProductSpec(
                    key="playback_time",
                    label="Playback",
                    value="up to 20 hours",
                )
            ],
        )
        supplementary = researched_product(
            product_id="supplementary",
            source_url=supplementary_url,
            specs=[
                ProductSpec(
                    key="playback_time",
                    label="Playback",
                    value="up to 20 hours",
                ),
                ProductSpec(
                    key="playback_time",
                    label="Battery Life",
                    value="up to 16 hours",
                ),
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
            supplementary_sources=[
                successful_source(
                    supplementary,
                    requested_url=supplementary_url,
                )
            ],
        )

        result = generate_product_claim_verification_report(bundle).claim_results[0]

        self.assertEqual(result.status, "conflicting")
        self.assertEqual(
            [observation.comparison for observation in result.observations],
            ["agrees", "conflicts"],
        )

    def test_duplicate_primary_claim_identity_fails_clearly(self) -> None:
        primary_url = "https://primary.example/product"
        primary = researched_product(
            source_url=primary_url,
            specs=[
                ProductSpec(key="rating", label="Rating", value="IP67"),
                ProductSpec(key="rating", label="Other Rating", value="IP68"),
            ],
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
        )

        with self.assertRaisesRegex(
            DuplicatePrimaryClaimError,
            "duplicate primary claim ID: spec:rating",
        ):
            generate_product_claim_verification_report(bundle)

    def test_report_models_reject_inconsistent_derived_state(self) -> None:
        observation = ClaimVerificationObservation(
            requested_url="https://retailer.test/product",
            source_url="https://retailer.test/product",
            normalized_domain="retailer.test",
            relationship="exact_product",
            claim_id="spec:rating",
            observed_value="IP67",
            comparison="agrees",
        )

        with self.assertRaisesRegex(
            ValidationError,
            "status does not match observation comparisons",
        ):
            ClaimVerificationResult(
                claim_id="spec:rating",
                claim_type="spec",
                primary_value="IP67",
                status="corroborated",
            )
        with self.assertRaisesRegex(
            ValidationError,
            "observation claim ID must match result claim ID",
        ):
            ClaimVerificationResult(
                claim_id="spec:other",
                claim_type="spec",
                primary_value="IP67",
                status="corroborated",
                observations=[observation],
            )
        with self.assertRaisesRegex(
            ValidationError,
            "failed source summary requires failure metadata only",
        ):
            ClaimVerificationSourceSummary(
                requested_url="https://failed.test/product",
                participation="failed",
            )

    def test_report_model_rejects_duplicate_claim_ids(self) -> None:
        result = ClaimVerificationResult(
            claim_id="spec:rating",
            claim_type="spec",
            primary_value="IP67",
            status="supported",
        )

        with self.assertRaisesRegex(
            ValidationError,
            "claim_results must contain unique claim IDs",
        ):
            ProductClaimVerificationReport(
                product_id="product",
                primary_source_url="https://primary.example/product",
                claim_results=[result, result.model_copy(deep=True)],
            )


if __name__ == "__main__":
    unittest.main()
