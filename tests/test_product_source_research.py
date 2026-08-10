import json
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from pydantic import ValidationError

from ad_claims import generate_ad_claim_candidates
from product import (
    ClaimEvidence,
    Product,
    ProductFeature,
    ProductImage,
    ProductSource,
    ProductSpec,
    ProductVariant,
)
from product_research import ProductFetchError
from product_source_research import (
    MultiSourceProductResearch,
    ProductReconciliation,
    ProductResearchFailure,
    SupplementaryProductSourceResult,
    TooManyProductSourcesError,
    reconcile_products,
    research_product_sources,
)


def researched_product(
    *,
    product_id: str = "primary-product",
    name: str = "Select 4 Go",
    brand: str = "Soundcore",
    category: str = "Portable Bluetooth Speaker",
    source_name: str = None,
    model_number: str = None,
    sku: str = None,
    color: str = None,
    source_url: str = "https://primary.example/products/select-4-go",
    with_research_data: bool = False,
) -> Product:
    evidence = ClaimEvidence(
        raw_text="Up to 20 hours of playback",
        source_url=source_url,
        extraction_method="test_fixture",
    )
    variant = (
        ProductVariant(
            model_number=model_number,
            sku=sku,
            color=color,
        )
        if any((model_number, sku, color))
        else None
    )
    return Product(
        id=product_id,
        source_name=source_name,
        name=name,
        brand=brand,
        category=category,
        variant=variant,
        images=(
            [
                ProductImage(
                    id=f"image-{product_id}",
                    url=f"https://cdn.example/{product_id}.jpg",
                    source_url=source_url,
                    source_type="json_ld_product_image",
                    confidence=0.95,
                )
            ]
            if with_research_data
            else []
        ),
        specs=(
            [
                ProductSpec(
                    key="playback_time",
                    label="Playback Time",
                    value="up to 20 hours",
                    source_url=source_url,
                    confidence=0.65,
                    evidence=evidence,
                )
            ]
            if with_research_data
            else []
        ),
        features=(
            [
                ProductFeature(
                    id="carrying-strap",
                    name="Carrying strap",
                    source_url=source_url,
                    confidence=0.65,
                    evidence=evidence.model_copy(deep=True),
                )
            ]
            if with_research_data
            else []
        ),
        sources=[
            ProductSource(
                url=source_url,
                source_type="product_page",
                domain=source_url.split("/")[2],
            )
        ],
    )


class ProductReconciliationTests(unittest.TestCase):
    def test_exact_brand_and_canonical_name_match(self) -> None:
        primary = researched_product()
        supplementary = researched_product(
            product_id="supplementary-product",
            brand="soundcore",
            source_name="Soundcore Select 4 Go | Portable Speaker",
            source_url="https://retailer.example/select-4-go",
        )

        result = reconcile_products(primary, supplementary)

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.relationship, "exact_product")
        self.assertEqual(result.reason, "exact_brand_and_name")

    def test_generic_category_only_name_does_not_reconcile(self) -> None:
        primary = researched_product(name="Bluetooth Speaker")
        supplementary = researched_product(
            product_id="generic-supplementary",
            name="Bluetooth Speaker",
            source_url="https://retailer.example/generic-speaker",
        )

        result = reconcile_products(primary, supplementary)

        self.assertEqual(result.status, "not_matched")
        self.assertEqual(result.reason, "insufficient_distinctive_identity")

    def test_different_brands_and_models_are_rejected(self) -> None:
        brand_mismatch = reconcile_products(
            researched_product(),
            researched_product(
                product_id="other-brand",
                brand="Othercore",
                source_url="https://other.example/select-4-go",
            ),
        )
        model_mismatch = reconcile_products(
            researched_product(model_number="A31X1"),
            researched_product(
                product_id="other-model",
                name="Select 4 Go",
                model_number="A31X2",
                source_url="https://retailer.example/select-4-go",
            ),
        )

        self.assertEqual(brand_mismatch.reason, "brand_mismatch")
        self.assertEqual(model_mismatch.reason, "model_number_conflict")
        self.assertEqual(brand_mismatch.status, "not_matched")
        self.assertEqual(model_mismatch.status, "not_matched")

    def test_exact_shared_model_number_is_a_strong_match(self) -> None:
        result = reconcile_products(
            researched_product(name="Select 4 Go", model_number="A31X1"),
            researched_product(
                product_id="retailer-model",
                name="Soundcore Portable Speaker A31X1",
                model_number="a31x1",
                source_url="https://retailer.example/a31x1",
            ),
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.reason, "shared_model_number")

    def test_sku_neither_proves_nor_disproves_cross_source_identity(self) -> None:
        different_skus = reconcile_products(
            researched_product(sku="PRIMARY-123"),
            researched_product(
                product_id="different-sku",
                sku="RETAILER-999",
                source_url="https://retailer.example/select-4-go",
            ),
        )
        same_sku_different_name = reconcile_products(
            researched_product(sku="SHARED-123"),
            researched_product(
                product_id="same-sku-other-model",
                name="Select 5 Max",
                sku="SHARED-123",
                source_url="https://retailer.example/select-5-max",
            ),
        )

        self.assertEqual(different_skus.status, "matched")
        self.assertEqual(same_sku_different_name.status, "not_matched")
        self.assertEqual(same_sku_different_name.reason, "name_mismatch")

    def test_meaningful_model_tokens_ignore_only_generic_seo_terms(self) -> None:
        result = reconcile_products(
            researched_product(name="WH-1000XM6"),
            researched_product(
                product_id="seo-model",
                name="WH-1000XM6 Wireless Headphones",
                source_url="https://retailer.example/wh-1000xm6",
            ),
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.reason, "exact_brand_and_name")

    def test_partial_model_family_overlap_is_ambiguous_not_matched(self) -> None:
        result = reconcile_products(
            researched_product(name="Trail 8"),
            researched_product(
                product_id="trail-mini",
                name="Trail 8 Mini",
                source_url="https://retailer.example/trail-8-mini",
            ),
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.relationship)
        self.assertEqual(result.reason, "partial_model_family_overlap")

    def test_group_primary_and_specific_variant_match_only_at_family_scope(self) -> None:
        primary = researched_product(
            product_id="soundcore-example-group-products-select-4-go",
        )
        supplementary = researched_product(
            product_id="retailer-variant-select-4-go-green",
            color="Green",
            sku="RETAILER-GREEN",
            source_url="https://retailer.example/select-4-go-green",
        )

        result = reconcile_products(primary, supplementary)

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.relationship, "product_family")

    def test_different_variant_fields_cannot_form_an_exact_variant_match(self) -> None:
        result = reconcile_products(
            researched_product(model_number="Green"),
            researched_product(
                product_id="color-variant",
                color="Green",
                source_url="https://retailer.example/select-4-go-green",
            ),
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.relationship, "product_family")

    def test_reconciliation_does_not_mutate_either_product(self) -> None:
        primary = researched_product(with_research_data=True)
        supplementary = researched_product(
            product_id="supplementary",
            source_url="https://retailer.example/select-4-go",
            with_research_data=True,
        )
        before_primary = json.loads(primary.model_dump_json())
        before_supplementary = json.loads(supplementary.model_dump_json())

        reconcile_products(primary, supplementary)

        self.assertEqual(json.loads(primary.model_dump_json()), before_primary)
        self.assertEqual(
            json.loads(supplementary.model_dump_json()),
            before_supplementary,
        )


class MultiSourceProductResearchModelTests(unittest.TestCase):
    def test_model_round_trip_rejects_unknown_fields(self) -> None:
        primary = researched_product()
        supplementary = researched_product(
            product_id="supplementary",
            source_url="https://retailer.example/select-4-go",
        )
        bundle = MultiSourceProductResearch(
            primary_requested_url="https://primary.example/products/select-4-go",
            primary_product=primary,
            supplementary_sources=[
                SupplementaryProductSourceResult(
                    requested_url="https://retailer.example/select-4-go",
                    product=supplementary,
                    reconciliation=reconcile_products(primary, supplementary),
                )
            ],
        )

        restored = MultiSourceProductResearch.model_validate_json(
            bundle.model_dump_json()
        )
        invalid = bundle.model_dump(mode="json")
        invalid["campaign_id"] = "not-allowed"

        self.assertEqual(restored, bundle)
        self.assertEqual(restored.primary_role, "primary")
        self.assertEqual(restored.supplementary_sources[0].source_role, "supplementary")
        with self.assertRaises(ValidationError):
            MultiSourceProductResearch.model_validate(invalid)

    def test_supplementary_result_requires_exactly_product_or_failure(self) -> None:
        with self.assertRaises(ValidationError):
            SupplementaryProductSourceResult(
                requested_url="https://retailer.example/product",
            )
        with self.assertRaises(ValidationError):
            SupplementaryProductSourceResult(
                requested_url=" ",
                product=researched_product(),
                reconciliation=ProductReconciliation(
                    status="matched",
                    relationship="exact_product",
                    reason="exact_brand_and_name",
                ),
            )
        with self.assertRaises(ValidationError):
            SupplementaryProductSourceResult(
                requested_url="https://retailer.example/product",
                product=researched_product(),
                reconciliation=ProductReconciliation(
                    status="matched",
                    relationship="exact_product",
                    reason="exact_brand_and_name",
                ),
                failure=ProductResearchFailure(
                    error_type="ProductFetchError",
                    message="failed",
                ),
            )


class ExplicitProductSourceResearchTests(unittest.TestCase):
    @patch("product_source_research.research_product")
    def test_primary_only_returns_isolated_primary_product(self, research: Mock) -> None:
        primary = researched_product(with_research_data=True)
        before = json.loads(primary.model_dump_json())
        research.return_value = primary
        session = Mock()

        result = research_product_sources(
            "https://primary.example/products/select-4-go",
            session=session,
        )

        self.assertEqual(result.primary_product, primary)
        self.assertIsNot(result.primary_product, primary)
        self.assertEqual(result.supplementary_sources, [])
        self.assertEqual(json.loads(primary.model_dump_json()), before)
        research.assert_called_once_with(
            "https://primary.example/products/select-4-go",
            timeout=15,
            session=session,
        )

    @patch("product_source_research.research_product")
    def test_matching_supplementary_sources_preserve_input_order(self, research: Mock) -> None:
        primary = researched_product()
        supplementaries = [
            researched_product(
                product_id=f"supplementary-{index}",
                source_url=f"https://retailer-{index}.example/select-4-go",
            )
            for index in range(3)
        ]
        urls = [
            "https://one.example/select-4-go",
            "https://two.example/select-4-go",
            "https://three.example/select-4-go",
        ]
        research.side_effect = [primary, *supplementaries]

        result = research_product_sources(
            "https://primary.example/products/select-4-go",
            urls,
        )

        self.assertEqual(
            [source.requested_url for source in result.supplementary_sources],
            urls,
        )
        self.assertTrue(
            all(
                source.reconciliation.status == "matched"
                for source in result.supplementary_sources
            )
        )
        self.assertEqual(
            [entry.args[0] for entry in research.call_args_list],
            ["https://primary.example/products/select-4-go", *urls],
        )

    @patch("product_source_research.research_product")
    def test_more_than_three_supplementary_urls_fail_before_fetch(self, research: Mock) -> None:
        with self.assertRaises(TooManyProductSourcesError):
            research_product_sources(
                "https://primary.example/product",
                [f"https://retailer-{index}.example/product" for index in range(4)],
            )

        research.assert_not_called()

    @patch("product_source_research.research_product")
    def test_primary_failure_fails_the_complete_run(self, research: Mock) -> None:
        research.side_effect = ProductFetchError("primary failed")

        with self.assertRaisesRegex(ProductFetchError, "primary failed"):
            research_product_sources(
                "https://primary.example/product",
                ["https://retailer.example/product"],
            )

        research.assert_called_once()

    @patch("product_source_research.research_product")
    def test_supplementary_failure_is_recorded_and_later_sources_continue(
        self,
        research: Mock,
    ) -> None:
        primary = researched_product()
        later = researched_product(
            product_id="later-source",
            source_url="https://later.example/select-4-go",
        )
        research.side_effect = [
            primary,
            ProductFetchError("retailer unavailable"),
            later,
        ]

        result = research_product_sources(
            "https://primary.example/product",
            [
                "https://failed.example/product",
                "https://later.example/product",
            ],
        )

        failure, success = result.supplementary_sources
        self.assertIsNone(failure.product)
        self.assertEqual(failure.failure.error_type, "ProductFetchError")
        self.assertEqual(failure.failure.message, "retailer unavailable")
        self.assertEqual(success.reconciliation.status, "matched")
        self.assertEqual(research.call_count, 3)

    @patch("product_source_research.research_product")
    def test_empty_supplementary_error_message_remains_structured(
        self,
        research: Mock,
    ) -> None:
        research.side_effect = [researched_product(), ProductFetchError()]

        result = research_product_sources(
            "https://primary.example/product",
            ["https://failed.example/product"],
        )

        failure = result.supplementary_sources[0].failure
        self.assertEqual(failure.error_type, "ProductFetchError")
        self.assertEqual(failure.message, "ProductFetchError")

    @patch("product_source_research.research_product")
    def test_invalid_blank_supplementary_url_remains_a_structured_failure(
        self,
        research: Mock,
    ) -> None:
        research.side_effect = [
            researched_product(),
            ProductFetchError("Product URL is required."),
            researched_product(
                product_id="later-source",
                source_url="https://later.example/select-4-go",
            ),
        ]

        result = research_product_sources(
            "https://primary.example/product",
            ["", "https://later.example/product"],
        )

        failure, success = result.supplementary_sources
        self.assertEqual(failure.requested_url, "")
        self.assertEqual(failure.failure.error_type, "ProductFetchError")
        self.assertEqual(failure.failure.message, "Product URL is required.")
        self.assertEqual(success.reconciliation.status, "matched")
        self.assertEqual(research.call_count, 3)

    @patch("product_source_research.research_product")
    def test_unmatched_product_is_retained_without_merging_research_data(
        self,
        research: Mock,
    ) -> None:
        primary = researched_product(with_research_data=True)
        supplementary = researched_product(
            product_id="different-product",
            name="Select 5 Max",
            source_url="https://retailer.example/select-5-max",
            with_research_data=True,
        )
        supplementary.specs[0].value = "up to 30 hours"
        primary_before = deepcopy(primary)
        supplementary_before = deepcopy(supplementary)
        claims_before = generate_ad_claim_candidates(primary)
        research.side_effect = [primary, supplementary]

        result = research_product_sources(
            "https://primary.example/select-4-go",
            ["https://retailer.example/select-5-max"],
        )

        source = result.supplementary_sources[0]
        self.assertEqual(source.reconciliation.status, "not_matched")
        self.assertEqual(source.product, supplementary_before)
        self.assertEqual(result.primary_product, primary_before)
        self.assertEqual(len(result.primary_product.specs), 1)
        self.assertEqual(len(result.primary_product.features), 1)
        self.assertEqual(len(result.primary_product.images), 1)
        self.assertFalse(result.primary_product.specs[0].verified)
        self.assertEqual(
            generate_ad_claim_candidates(result.primary_product),
            claims_before,
        )
        self.assertEqual(primary, primary_before)
        self.assertEqual(supplementary, supplementary_before)


if __name__ == "__main__":
    unittest.main()
