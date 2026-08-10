import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from pydantic import ValidationError

from official_product_discovery import (
    OfficialProductPageCandidate,
    OfficialProductPageDiscovery,
    OfficialProductPageRequest,
    OfficialProductPageSearchError,
    ProductPageSearchResult,
    discovery_request_from_product,
    discover_official_product_page,
)
from product import (
    Product,
    ProductImage,
    ProductSource,
    ProductSpec,
    ProductVariant,
)
from product_research import ProductFetchError


def researched_product(
    *,
    name: str = "Select 4 Go",
    brand: str = "soundcore",
    source_url: str = "https://www.soundcore.com/products/a31x1021",
    model_number: str = None,
    category: str = "Portable Bluetooth Speaker",
) -> Product:
    return Product(
        id="researched-product",
        name=name,
        brand=brand,
        category=category,
        variant=(
            ProductVariant(model_number=model_number)
            if model_number
            else None
        ),
        sources=[
            ProductSource(
                url=source_url,
                source_type="product_page",
                domain=source_url.split("/")[2],
            )
        ],
    )


class FakeSearchProvider:
    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls = []

    def search(self, query: str, *, limit: int):
        self.calls.append((query, limit))
        response = self.responses.get(query, [])
        if isinstance(response, Exception):
            raise response
        return response


class OfficialProductPageDiscoveryTests(unittest.TestCase):
    @patch("official_product_discovery.research_product")
    def test_exact_official_candidate_is_found(self, research: Mock) -> None:
        request = OfficialProductPageRequest(
            brand="Soundcore",
            product_name="Select 4 Go",
        )
        result_url = "https://soundcore.com/products/a31x1021"
        provider = FakeSearchProvider(
            {
                "Soundcore Select 4 Go": [
                    ProductPageSearchResult(url=result_url)
                ]
            }
        )
        research.return_value = researched_product()

        result = discover_official_product_page(request, provider)

        self.assertEqual(result.status, "found")
        self.assertEqual(result.discovered_url, result_url)
        self.assertEqual(
            result.accepted_url,
            "https://www.soundcore.com/products/a31x1021",
        )
        self.assertEqual(result.product, research.return_value)
        self.assertIsNot(result.product, research.return_value)
        self.assertEqual(result.candidates[0].validation_status, "accepted")
        research.assert_called_once_with(result_url, timeout=15, session=None)

    @patch("official_product_discovery.research_product")
    def test_lower_ranked_exact_product_beats_rank_one_wrong_product(
        self,
        research: Mock,
    ) -> None:
        query = "soundcore Select 4 Go"
        wrong_url = "https://soundcore.com/products/select-5-max"
        exact_url = "https://soundcore.com/products/select-4-go"
        provider = FakeSearchProvider(
            {
                query: [
                    ProductPageSearchResult(url=wrong_url),
                    ProductPageSearchResult(url=exact_url),
                ]
            }
        )
        research.side_effect = lambda url, **_: (
            researched_product(name="Select 5 Max", source_url=url)
            if url == wrong_url
            else researched_product(source_url=url)
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.discovered_url, exact_url)
        self.assertEqual(
            [candidate.validation_status for candidate in result.candidates],
            ["identity_mismatch", "accepted"],
        )

    @patch("official_product_discovery.research_product")
    def test_brand_text_inside_unrelated_domain_is_rejected_without_fetch(
        self,
        research: Mock,
    ) -> None:
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(
                        url="https://soundcore-deals.example/select-4-go"
                    )
                ]
            }
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(
            result.candidates[0].validation_status,
            "unofficial_domain",
        )
        research.assert_not_called()

    @patch("official_product_discovery.research_product")
    def test_known_official_domain_constrains_search_and_still_rejects_wrong_product(
        self,
        research: Mock,
    ) -> None:
        query = "site:soundcore.com soundcore Select 4 Go"
        official_url = "https://www.soundcore.com/products/wrong"
        provider = FakeSearchProvider(
            {
                query: [
                    ProductPageSearchResult(
                        url="https://retailer.test/products/select-4-go"
                    ),
                    ProductPageSearchResult(url=official_url),
                ]
            }
        )
        research.return_value = researched_product(
            name="Select 5 Max",
            source_url=official_url,
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
                known_official_domain="www.soundcore.com",
            ),
            provider,
        )

        self.assertEqual(provider.calls, [(query, 10)])
        self.assertEqual(result.status, "not_found")
        self.assertEqual(
            [candidate.validation_status for candidate in result.candidates],
            ["unofficial_domain", "identity_mismatch"],
        )
        research.assert_called_once()

    @patch("official_product_discovery.research_product")
    def test_equal_valid_candidates_are_ambiguous(self, research: Mock) -> None:
        urls = [
            "https://soundcore.com/products/select-4-go",
            "https://soundcore.com/products/a31x1021",
        ]
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(url=url) for url in urls
                ]
            }
        )
        research.side_effect = lambda url, **_: researched_product(source_url=url)

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.accepted_url)
        self.assertEqual(
            [candidate.validation_status for candidate in result.candidates],
            ["accepted", "accepted"],
        )

    def test_search_provider_failure_is_explicit(self) -> None:
        provider = FakeSearchProvider(
            {"soundcore Select 4 Go": RuntimeError("provider unavailable")}
        )

        with self.assertRaisesRegex(
            OfficialProductPageSearchError,
            "provider unavailable",
        ):
            discover_official_product_page(
                OfficialProductPageRequest(
                    brand="soundcore",
                    product_name="Select 4 Go",
                ),
                provider,
            )

    def test_lazy_search_provider_failure_is_explicit(self) -> None:
        def failing_results():
            yield ProductPageSearchResult(
                url="https://soundcore.com/products/first"
            )
            raise RuntimeError("lazy provider failure")

        provider = FakeSearchProvider(
            {"soundcore Select 4 Go": failing_results()}
        )

        with self.assertRaisesRegex(
            OfficialProductPageSearchError,
            "lazy provider failure",
        ):
            discover_official_product_page(
                OfficialProductPageRequest(
                    brand="soundcore",
                    product_name="Select 4 Go",
                ),
                provider,
            )

    @patch("official_product_discovery.research_product")
    def test_candidate_failure_is_recorded_and_later_candidate_can_succeed(
        self,
        research: Mock,
    ) -> None:
        failed_url = "https://soundcore.com/products/unavailable"
        exact_url = "https://soundcore.com/products/select-4-go"
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(url=failed_url),
                    ProductPageSearchResult(url=exact_url),
                ]
            }
        )
        research.side_effect = [
            ProductFetchError("timed out"),
            researched_product(source_url=exact_url),
        ]

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.candidates[0].validation_status, "research_failed")
        self.assertEqual(result.candidates[0].reason, "timed out")
        self.assertEqual(research.call_count, 2)

    @patch("official_product_discovery.research_product")
    def test_duplicate_result_urls_are_fetched_once(self, research: Mock) -> None:
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(
                        url="https://www.soundcore.com/products/a31x1021?utm_source=search"
                    ),
                    ProductPageSearchResult(
                        url="http://soundcore.com/products/a31x1021"
                    ),
                ]
            }
        )
        research.return_value = researched_product()

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(len(result.candidates), 1)
        research.assert_called_once()

    @patch("official_product_discovery.research_product")
    def test_redirect_preserves_discovered_and_final_urls(self, research: Mock) -> None:
        discovered_url = "https://soundcore.com/go/select-4-go"
        final_url = "https://www.soundcore.com/products/a31x1021"
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(url=discovered_url)
                ]
            }
        )
        research.return_value = researched_product(source_url=final_url)

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.discovered_url, discovered_url)
        self.assertEqual(result.accepted_url, final_url)
        self.assertEqual(result.candidates[0].final_url, final_url)

    @patch("official_product_discovery.research_product")
    def test_off_domain_redirect_cannot_be_accepted(self, research: Mock) -> None:
        discovered_url = "https://soundcore.com/go/select-4-go"
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(url=discovered_url)
                ]
            }
        )
        research.return_value = researched_product(
            source_url="https://impostor.example/products/select-4-go"
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(
            result.candidates[0].validation_status,
            "unofficial_domain",
        )
        self.assertEqual(result.candidates[0].reason, "final_domain_not_official")

    @patch("official_product_discovery.research_product")
    def test_brand_casing_and_researched_seo_name_normalization_match(
        self,
        research: Mock,
    ) -> None:
        url = "https://soundcore.com/products/select-4-go"
        provider = FakeSearchProvider(
            {
                "SOUNDCORE Select 4 Go": [ProductPageSearchResult(url=url)],
            }
        )
        research.return_value = researched_product(
            brand="soundcore",
            name="Select 4 Go",
            source_url=url,
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="SOUNDCORE",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.product.name, "Select 4 Go")

    @patch("official_product_discovery.research_product")
    def test_unique_shared_model_number_is_stronger_than_name_only_match(
        self,
        research: Mock,
    ) -> None:
        name_url = "https://soundcore.com/products/select-4-go-name"
        model_url = "https://soundcore.com/products/a31x1021"
        query = "soundcore Select 4 Go A31X1021"
        provider = FakeSearchProvider(
            {
                query: [
                    ProductPageSearchResult(url=name_url),
                    ProductPageSearchResult(url=model_url),
                ]
            }
        )
        research.side_effect = [
            researched_product(source_url=name_url),
            researched_product(
                source_url=model_url,
                model_number="a31x1021",
            ),
        ]

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
                model_number="A31X1021",
            ),
            provider,
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.discovered_url, model_url)
        self.assertEqual(
            result.candidates[1].identity_match_reason,
            "shared_model_number",
        )

    @patch("official_product_discovery.research_product")
    def test_conflicting_model_number_is_rejected_deterministically(
        self,
        research: Mock,
    ) -> None:
        url = "https://soundcore.com/products/select-4-go"
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go A31X1021": [
                    ProductPageSearchResult(url=url)
                ]
            }
        )
        research.return_value = researched_product(
            source_url=url,
            model_number="A31X9999",
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
                model_number="A31X1021",
            ),
            provider,
        )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.candidates[0].reason, "model_number_conflict")

    @patch("official_product_discovery.research_product")
    def test_similar_name_is_not_fuzzily_accepted(self, research: Mock) -> None:
        url = "https://soundcore.com/products/select-4"
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [ProductPageSearchResult(url=url)],
            }
        )
        research.return_value = researched_product(
            name="Select 4",
            source_url=url,
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.candidates[0].validation_status, "identity_mismatch")

    @patch("official_product_discovery.research_product")
    def test_multiple_discovered_urls_for_same_final_page_are_not_ambiguous(
        self,
        research: Mock,
    ) -> None:
        urls = [
            "https://soundcore.com/go/select-4-go",
            "https://soundcore.com/products/select-4-go",
        ]
        final_url = "https://www.soundcore.com/products/a31x1021"
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(url=url) for url in urls
                ],
            }
        )
        research.return_value = researched_product(source_url=final_url)

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.accepted_url, final_url)
        self.assertEqual(
            [candidate.validation_status for candidate in result.candidates],
            ["accepted", "duplicate_final_url"],
        )

    @patch("official_product_discovery.research_product")
    def test_obvious_non_product_urls_are_rejected_without_fetch(
        self,
        research: Mock,
    ) -> None:
        urls = [
            "https://soundcore.com/collections/speakers",
            "https://soundcore.com/blogs/reviews/select-4-go",
            "https://soundcore.com/support/select-4-go",
            "https://soundcore.com/manuals/select-4-go.pdf",
            "https://youtube.com/watch?v=select4go",
            "https://google.com/search?q=select+4+go",
        ]
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(url=url) for url in urls
                ],
            }
        )

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "not_found")
        self.assertTrue(
            all(
                candidate.validation_status == "rejected_url"
                for candidate in result.candidates
            )
        )
        research.assert_not_called()

    @patch("official_product_discovery.research_product")
    def test_filtered_results_do_not_consume_five_page_fetch_budget(
        self,
        research: Mock,
    ) -> None:
        exact_url = "https://soundcore.com/products/exact"
        results = [
            ProductPageSearchResult(url="https://example.com/not-official"),
            ProductPageSearchResult(url="https://soundcore.com/blog/news"),
            ProductPageSearchResult(url="https://youtube.com/watch?v=x"),
        ] + [
            ProductPageSearchResult(
                url=f"https://soundcore.com/products/candidate-{index}"
            )
            for index in range(4)
        ] + [ProductPageSearchResult(url=exact_url)]
        provider = FakeSearchProvider({"soundcore Select 4 Go": results})
        research.side_effect = [
            researched_product(name="Wrong Product", source_url=result.url)
            for result in results[3:7]
        ] + [researched_product(source_url=exact_url)]

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(research.call_count, 5)

    @patch("official_product_discovery.research_product")
    def test_page_fetches_are_capped_at_five(self, research: Mock) -> None:
        results = [
            ProductPageSearchResult(
                url=f"https://soundcore.com/products/candidate-{index}"
            )
            for index in range(7)
        ]
        provider = FakeSearchProvider({"soundcore Select 4 Go": results})
        research.return_value = researched_product(name="Wrong Product")

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(research.call_count, 5)
        self.assertEqual(
            [candidate.validation_status for candidate in result.candidates[-2:]],
            ["not_fetched", "not_fetched"],
        )

    def test_provider_iterator_is_consumed_at_most_ten_results(self) -> None:
        consumed = []

        def results():
            for index in range(20):
                consumed.append(index)
                yield ProductPageSearchResult(
                    url=f"https://example.com/products/{index}"
                )

        provider = FakeSearchProvider({"soundcore Select 4 Go": results()})

        discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(len(consumed), 10)

    @patch("official_product_discovery._queries")
    def test_search_query_count_is_capped_at_three(self, queries: Mock) -> None:
        queries.return_value = ["one", "two", "three", "four"]
        provider = FakeSearchProvider({})

        discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(provider.calls, [("one", 10), ("two", 10), ("three", 10)])

    @patch("official_product_discovery.research_product")
    def test_search_metadata_never_populates_product_facts(
        self,
        research: Mock,
    ) -> None:
        url = "https://soundcore.com/products/select-4-go"
        provider = FakeSearchProvider(
            {
                "soundcore Select 4 Go": [
                    ProductPageSearchResult(
                        url=url,
                        title="Invented Search Product Name",
                        snippet="Invented battery, waterproof, and image facts",
                    )
                ]
            }
        )
        research.return_value = researched_product(source_url=url)

        result = discover_official_product_page(
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            provider,
        )

        self.assertEqual(result.product, research.return_value)
        self.assertEqual(result.product.specs, [])
        self.assertEqual(result.product.features, [])
        self.assertEqual(result.product.images, [])

    def test_request_can_be_derived_from_product_without_mutation(self) -> None:
        product = researched_product(model_number="A31X1021")
        before = deepcopy(product)

        request = discovery_request_from_product(
            product,
            known_official_domain="soundcore.com",
        )

        self.assertEqual(
            request,
            OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
                model_number="A31X1021",
                known_official_domain="soundcore.com",
            ),
        )
        self.assertEqual(product, before)

    def test_request_rejects_non_domain_values(self) -> None:
        for value in (
            "https://soundcore.com/products",
            "soundcore.com/path",
            "soundcore.com:443",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    OfficialProductPageRequest(
                        brand="soundcore",
                        product_name="Select 4 Go",
                        known_official_domain=value,
                    )

    def test_discovery_models_round_trip_and_reject_unknown_fields(self) -> None:
        candidate = OfficialProductPageCandidate(
            discovered_url="https://example.com/products/select-4-go",
            query="soundcore Select 4 Go",
            search_rank=1,
            domain="example.com",
            validation_status="unofficial_domain",
            reason="domain_not_official",
        )
        result = OfficialProductPageDiscovery(
            request=OfficialProductPageRequest(
                brand="soundcore",
                product_name="Select 4 Go",
            ),
            status="not_found",
            candidates=[candidate],
            queries=["soundcore Select 4 Go"],
            reason="no_official_product_page_accepted",
        )

        self.assertEqual(
            OfficialProductPageDiscovery.model_validate_json(
                result.model_dump_json()
            ),
            result,
        )
        self.assertEqual(
            OfficialProductPageCandidate.model_validate_json(
                candidate.model_dump_json()
            ),
            candidate,
        )
        with self.assertRaises(ValidationError):
            ProductPageSearchResult(
                url="https://soundcore.com/products/select-4-go",
                unexpected="value",
            )
        with self.assertRaises(ValidationError):
            OfficialProductPageDiscovery.model_validate(
                {**result.model_dump(), "unexpected": "value"}
            )


if __name__ == "__main__":
    unittest.main()
