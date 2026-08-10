import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from official_product_discovery import (
    OfficialProductPageDiscovery,
    OfficialProductPageRequest,
)
from product import Product, ProductSource
from product_discovery_orchestration import (
    DiscoverySelectionError,
    research_selected_discovery_sources,
)
from product_source_research import (
    MultiSourceProductResearch,
    ProductReconciliation,
)
from supplementary_product_discovery import (
    SupplementaryProductSourceDiscovery,
    SupplementarySourceCandidate,
    SupplementarySourceProposal,
)


def product(*, product_id: str, source_url: str) -> Product:
    return Product(
        id=product_id,
        name="Trail 100",
        brand="Acme Audio",
        category="Portable Speakers",
        sources=[
            ProductSource(
                url=source_url,
                source_type="product_page",
                domain=source_url.split("/")[2],
            )
        ],
    )


def official_discovery(primary: Product) -> OfficialProductPageDiscovery:
    primary_url = primary.sources[0].url
    return OfficialProductPageDiscovery(
        request=OfficialProductPageRequest(
            brand=primary.brand,
            product_name=primary.name,
        ),
        status="found",
        accepted_url=primary_url,
        discovered_url=primary_url,
        product=primary,
        reason="one_strongest_official_product_page_accepted",
    )


def supplementary_discovery(
    primary: Product,
    *supplementary_products: Product,
) -> SupplementaryProductSourceDiscovery:
    reconciliation = ProductReconciliation(
        status="matched",
        relationship="exact_product",
        reason="exact_brand_and_name",
    )
    candidates = []
    proposals = []
    for rank, supplementary in enumerate(supplementary_products, start=1):
        accepted_url = supplementary.sources[0].url
        publisher = accepted_url.split("/")[2]
        candidates.append(
            SupplementarySourceCandidate(
                discovered_url=accepted_url,
                query="Acme Audio Trail 100",
                search_rank=rank,
                status="accepted",
                reason="exact_product_direct_retailer",
                final_url=accepted_url,
                publisher=publisher,
                source_classification="direct_retailer_product",
                classification_reasons=[
                    "structured_product",
                    "structured_offer",
                    "commerce_control",
                    "price_signal",
                ],
                product=supplementary,
                reconciliation=reconciliation,
            )
        )
        proposals.append(
            SupplementarySourceProposal(
                discovered_url=accepted_url,
                accepted_url=accepted_url,
                publisher=publisher,
                product=supplementary,
            )
        )
    return SupplementaryProductSourceDiscovery(
        primary_product_id=primary.id,
        primary_url=primary.sources[0].url,
        status="found" if proposals else "not_found",
        queries=["Acme Audio Trail 100"],
        proposals=proposals,
        candidates=candidates,
        reason=(
            "direct_retailer_product_sources_proposed"
            if proposals
            else "no_direct_retailer_product_sources_accepted"
        ),
    )


class ProductDiscoveryOrchestrationTests(unittest.TestCase):
    @patch("product_discovery_orchestration.research_product_sources")
    def test_explicit_selected_proposal_is_sent_to_feature_221(
        self,
        research_sources: Mock,
    ) -> None:
        primary_url = "https://acmeaudio.example/products/trail-100"
        supplementary_url = "https://retailer.example/products/trail-100"
        primary = product(product_id="official-primary", source_url=primary_url)
        supplementary = product(
            product_id="retailer-product",
            source_url=supplementary_url,
        )
        expected = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
        )
        research_sources.return_value = expected

        result = research_selected_discovery_sources(
            official_discovery(primary),
            supplementary_discovery(primary, supplementary),
            [supplementary_url],
            timeout=9,
            session="session",
        )

        research_sources.assert_called_once_with(
            primary_url,
            [supplementary_url],
            timeout=9,
            session="session",
        )
        self.assertEqual(result, expected)
        self.assertIsNot(result, expected)

    @patch("product_discovery_orchestration.research_product_sources")
    def test_selected_urls_resolve_in_proposal_order(
        self,
        research_sources: Mock,
    ) -> None:
        primary_url = "https://acmeaudio.example/products/trail-100"
        primary = product(product_id="official-primary", source_url=primary_url)
        first_url = "https://one.example/products/trail-100"
        second_url = "https://two.example/products/trail-100"
        first = product(product_id="one", source_url=first_url)
        second = product(product_id="two", source_url=second_url)
        expected = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
        )
        research_sources.return_value = expected

        research_selected_discovery_sources(
            official_discovery(primary),
            supplementary_discovery(primary, first, second),
            [second_url, first_url],
        )

        research_sources.assert_called_once_with(
            primary_url,
            [first_url, second_url],
            timeout=15,
            session=None,
        )

    @patch("product_discovery_orchestration.research_product_sources")
    def test_unknown_duplicate_and_non_list_selections_fail_before_research(
        self,
        research_sources: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        accepted_url = "https://retailer.example/products/trail-100"
        supplementary = supplementary_discovery(
            primary,
            product(product_id="retailer", source_url=accepted_url),
        )

        for selected in (
            ["https://unknown.example/products/trail-100"],
            [accepted_url, accepted_url],
            accepted_url,
            [""],
            None,
        ):
            with self.subTest(selected=selected):
                with self.assertRaises(DiscoverySelectionError):
                    research_selected_discovery_sources(
                        official_discovery(primary),
                        supplementary,
                        selected,
                    )

        research_sources.assert_not_called()

    @patch("product_discovery_orchestration.research_product_sources")
    def test_mismatched_or_not_found_discoveries_fail_before_research(
        self,
        research_sources: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        supplementary = supplementary_discovery(
            primary,
            product(
                product_id="retailer",
                source_url="https://retailer.example/products/trail-100",
            ),
        )
        not_found = OfficialProductPageDiscovery(
            request=OfficialProductPageRequest(
                brand="Acme Audio",
                product_name="Trail 100",
            ),
            status="not_found",
            reason="no_official_product_page_accepted",
        )
        wrong_primary = supplementary.model_copy(
            update={"primary_product_id": "another-product"},
            deep=True,
        )

        for official, discovered in (
            (not_found, supplementary),
            (official_discovery(primary), wrong_primary),
        ):
            with self.subTest(official_status=official.status):
                with self.assertRaises(DiscoverySelectionError):
                    research_selected_discovery_sources(
                        official,
                        discovered,
                        [],
                    )

        research_sources.assert_not_called()

    @patch("product_discovery_orchestration.research_product_sources")
    def test_primary_url_mismatch_fails_before_research(
        self,
        research_sources: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        discovered = supplementary_discovery(primary).model_copy(
            update={
                "primary_url": "https://another.example/products/trail-100"
            },
            deep=True,
        )

        with self.assertRaises(DiscoverySelectionError):
            research_selected_discovery_sources(
                official_discovery(primary),
                discovered,
                [],
            )

        research_sources.assert_not_called()

    @patch("product_discovery_orchestration.research_product_sources")
    def test_explicit_empty_selection_runs_primary_only_without_verification(
        self,
        research_sources: Mock,
    ) -> None:
        primary_url = "https://acmeaudio.example/products/trail-100"
        primary = product(product_id="official-primary", source_url=primary_url)
        official = official_discovery(primary)
        supplementary = supplementary_discovery(primary)
        official_before = deepcopy(official)
        supplementary_before = deepcopy(supplementary)
        expected = MultiSourceProductResearch(
            primary_requested_url=primary_url,
            primary_product=primary,
        )
        research_sources.return_value = expected

        with patch(
            "product_claim_verification.generate_product_claim_verification_report"
        ) as verification:
            result = research_selected_discovery_sources(
                official,
                supplementary,
                [],
            )

        research_sources.assert_called_once_with(
            primary_url,
            [],
            timeout=15,
            session=None,
        )
        verification.assert_not_called()
        self.assertEqual(official, official_before)
        self.assertEqual(supplementary, supplementary_before)
        result.primary_product.name = "Changed output"
        self.assertEqual(expected.primary_product.name, "Trail 100")


if __name__ == "__main__":
    unittest.main()
