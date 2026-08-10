import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from official_product_discovery import (
    OfficialProductPageSearchError,
    ProductPageSearchResult,
)
from product import Product, ProductSource, ProductVariant
from supplementary_product_discovery import (
    SupplementaryProductSourceDiscovery,
    SupplementarySourceCandidate,
    SupplementarySourceProposal,
    classify_supplementary_product_page,
    discover_supplementary_product_sources,
)
from product_research import ProductFetchError


DIRECT_RETAILER_HTML = """
<!doctype html>
<html>
  <head>
    <link rel="canonical" href="https://retailer.example/products/trail-100">
    <meta property="product:price:amount" content="99.00">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "url": "https://retailer.example/products/trail-100",
        "name": "Trail 100",
        "brand": {"@type": "Brand", "name": "Acme Audio"},
        "offers": {
          "@type": "Offer",
          "price": "99.00",
          "priceCurrency": "USD",
          "availability": "https://schema.org/InStock",
          "seller": {"@type": "Organization", "name": "Retailer"}
        }
      }
    </script>
  </head>
  <body>
    <main><h1>Trail 100</h1><button>Add to cart</button></main>
  </body>
</html>
"""


def product(
    *,
    product_id: str,
    source_url: str,
    name: str = "Trail 100",
    brand: str = "Acme Audio",
    model_number: str = None,
) -> Product:
    return Product(
        id=product_id,
        name=name,
        brand=brand,
        category="Portable Speakers",
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


def direct_retailer_html(url: str) -> str:
    publisher_label = url.split("/")[2].split(".")[-2]
    seller = publisher_label.replace("-", " ").title()
    return DIRECT_RETAILER_HTML.replace(
        "https://retailer.example/products/trail-100",
        url,
    ).replace('"name": "Retailer"', f'"name": "{seller}"')


class FakeSearchProvider:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, query: str, *, limit: int):
        self.calls.append((query, limit))
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


class SupplementaryProductSourceDiscoveryTests(unittest.TestCase):
    @patch("supplementary_product_discovery.research_product_page")
    def test_direct_retailer_exact_product_page_is_accepted(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        discovered_url = "https://retailer.example/products/trail-100"
        researched = product(
            product_id="retailer-product",
            source_url=discovered_url,
        )
        research_page.return_value = SimpleNamespace(
            url=discovered_url,
            html=DIRECT_RETAILER_HTML,
            product=researched,
        )
        provider = FakeSearchProvider(
            [ProductPageSearchResult(url=discovered_url)]
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(result.status, "found")
        self.assertEqual(len(result.proposals), 1)
        self.assertEqual(result.proposals[0].accepted_url, discovered_url)
        self.assertEqual(result.proposals[0].publisher, "retailer.example")
        self.assertEqual(
            result.candidates[0].source_classification,
            "direct_retailer_product",
        )
        self.assertEqual(result.candidates[0].status, "accepted")
        self.assertEqual(
            result.candidates[0].reconciliation.relationship,
            "exact_product",
        )
        self.assertEqual(result.proposals[0].product, researched)
        self.assertIsNot(result.proposals[0].product, researched)
        research_page.assert_called_once_with(
            discovered_url,
            timeout=15,
            session=None,
        )

    @patch("supplementary_product_discovery.research_product_page")
    def test_three_independent_publishers_are_accepted_and_fourth_hits_limit(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        urls = [
            f"https://retailer-{index}.example/products/trail-100"
            for index in range(1, 5)
        ]
        provider = FakeSearchProvider(
            [ProductPageSearchResult(url=url) for url in urls]
        )
        research_page.side_effect = lambda url, **_: SimpleNamespace(
            url=url,
            html=direct_retailer_html(url),
            product=product(product_id=f"product-{url}", source_url=url),
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(len(result.proposals), 3)
        self.assertEqual(len({item.publisher for item in result.proposals}), 3)
        self.assertEqual(result.candidates[3].status, "proposal_limit")
        self.assertEqual(research_page.call_count, 4)

    @patch("supplementary_product_discovery.research_product_page")
    def test_one_proposal_per_normalized_publisher(self, research_page: Mock) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        urls = [
            "https://shop.retailer.example/products/trail-100",
            "https://www.retailer.example/item/trail-100",
        ]
        provider = FakeSearchProvider(
            [ProductPageSearchResult(url=url) for url in urls]
        )
        research_page.side_effect = lambda url, **_: SimpleNamespace(
            url=url,
            html=direct_retailer_html(url),
            product=product(product_id=f"product-{url}", source_url=url),
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(len(result.proposals), 1)
        self.assertEqual(result.proposals[0].publisher, "retailer.example")
        self.assertEqual(result.candidates[1].status, "duplicate_publisher")

    @patch("supplementary_product_discovery.research_product_page")
    def test_official_primary_publisher_is_excluded_before_fetch(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://shop.acmeaudio.example/products/trail-100",
        )
        provider = FakeSearchProvider(
            [
                ProductPageSearchResult(
                    url="https://www.acmeaudio.example/store/trail-100"
                )
            ]
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(result.status, "not_found")
        self.assertEqual(
            result.candidates[0].status,
            "excluded_primary_publisher",
        )
        research_page.assert_not_called()

    @patch("supplementary_product_discovery.research_product_page")
    def test_redirect_to_official_primary_publisher_is_excluded(
        self,
        research_page: Mock,
    ) -> None:
        primary_url = "https://acmeaudio.example/products/trail-100"
        primary = product(product_id="official-primary", source_url=primary_url)
        discovered_url = "https://redirector.example/trail-100"
        research_page.return_value = SimpleNamespace(
            url=primary_url,
            html=direct_retailer_html(primary_url),
            product=product(product_id="official-copy", source_url=primary_url),
        )

        result = discover_supplementary_product_sources(
            primary,
            FakeSearchProvider([ProductPageSearchResult(url=discovered_url)]),
        )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(
            result.candidates[0].status,
            "excluded_primary_publisher",
        )
        self.assertEqual(result.candidates[0].final_url, primary_url)

    @patch("supplementary_product_discovery.research_product_page")
    def test_product_family_match_is_recorded_but_not_accepted(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="acme-group-trail-100",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        url = "https://retailer.example/products/trail-100-red"
        research_page.return_value = SimpleNamespace(
            url=url,
            html=direct_retailer_html(url),
            product=product(
                product_id="retailer-variant",
                source_url=url,
                model_number="TRAIL-100-RED",
            ),
        )

        result = discover_supplementary_product_sources(
            primary,
            FakeSearchProvider([ProductPageSearchResult(url=url)]),
        )

        candidate = result.candidates[0]
        self.assertEqual(result.status, "not_found")
        self.assertEqual(candidate.status, "identity_mismatch")
        self.assertEqual(candidate.reconciliation.relationship, "product_family")
        self.assertEqual(candidate.reason, "product_family_not_accepted")

    @patch("supplementary_product_discovery.research_product_page")
    def test_unmatched_and_ambiguous_identity_are_rejected(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        wrong_url = "https://one.example/products/ridge-200"
        family_url = "https://two.example/products/trail-100-mini"
        provider = FakeSearchProvider(
            [
                ProductPageSearchResult(url=wrong_url),
                ProductPageSearchResult(url=family_url),
            ]
        )
        researched = {
            wrong_url: product(
                product_id="wrong",
                source_url=wrong_url,
                name="Ridge 200",
            ),
            family_url: product(
                product_id="ambiguous",
                source_url=family_url,
                name="Trail 100 Mini",
            ),
        }
        research_page.side_effect = lambda url, **_: SimpleNamespace(
            url=url,
            html=direct_retailer_html(url),
            product=researched[url],
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(result.status, "not_found")
        self.assertEqual(
            [item.reconciliation.status for item in result.candidates],
            ["not_matched", "ambiguous"],
        )
        self.assertTrue(
            all(item.status == "identity_mismatch" for item in result.candidates)
        )

    @patch("supplementary_product_discovery.research_product_page")
    def test_rank_one_marketplace_loses_to_lower_rank_retailer(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        marketplace_url = "https://market.example/listing/trail-100"
        retailer_url = "https://retailer.example/products/trail-100"
        marketplace_html = """
          <script type="application/ld+json">
          {"@context":"https://schema.org","@type":"Product","offers":{
            "@type":"AggregateOffer","offerCount":2,"offers":[
              {"@type":"Offer","seller":{"name":"A"}},
              {"@type":"Offer","seller":{"name":"B"}}
            ]}}
          </script><meta property="product:price:amount" content="99">
          <button>Add to cart</button>
        """
        provider = FakeSearchProvider(
            [
                ProductPageSearchResult(url=marketplace_url),
                ProductPageSearchResult(url=retailer_url),
            ]
        )
        research_page.side_effect = lambda url, **_: SimpleNamespace(
            url=url,
            html=(marketplace_html if url == marketplace_url else DIRECT_RETAILER_HTML),
            product=product(product_id=f"product-{url}", source_url=url),
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(result.proposals[0].accepted_url, retailer_url)
        self.assertEqual(result.candidates[0].status, "rejected_source_type")
        self.assertEqual(
            result.candidates[0].source_classification,
            "marketplace_or_third_party",
        )

    @patch("supplementary_product_discovery.research_product_page")
    def test_redirects_and_products_are_isolated_without_automatic_followups(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        primary_before = deepcopy(primary)
        discovered_url = "https://retailer.example/go/trail-100"
        final_url = "https://www.retailer.example/products/trail-100"
        researched = product(product_id="retailer", source_url=final_url)
        researched_before = deepcopy(researched)
        research_page.return_value = SimpleNamespace(
            url=final_url,
            html=direct_retailer_html(final_url),
            product=researched,
        )

        with patch(
            "product_source_research.research_product_sources"
        ) as multi_source, patch(
            "product_claim_verification.generate_product_claim_verification_report"
        ) as verification:
            result = discover_supplementary_product_sources(
                primary,
                FakeSearchProvider(
                    [
                        ProductPageSearchResult(
                            url=discovered_url,
                            title="Invented title",
                            snippet="Invented specs and features",
                        )
                    ]
                ),
            )

        self.assertEqual(result.proposals[0].discovered_url, discovered_url)
        self.assertEqual(result.proposals[0].accepted_url, final_url)
        self.assertEqual(result.proposals[0].product, researched)
        self.assertEqual(result.proposals[0].product.specs, [])
        self.assertEqual(result.proposals[0].product.features, [])
        result.proposals[0].product.name = "Changed output"
        self.assertEqual(primary, primary_before)
        self.assertEqual(researched, researched_before)
        multi_source.assert_not_called()
        verification.assert_not_called()

    @patch("supplementary_product_discovery.research_product_page")
    def test_ambiguous_and_editorial_page_types_are_not_proposed(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        ambiguous_url = "https://unknown.example/products/trail-100"
        editorial_url = "https://publisher.example/trail-100-review"
        provider = FakeSearchProvider(
            [
                ProductPageSearchResult(url=ambiguous_url),
                ProductPageSearchResult(url=editorial_url),
            ]
        )
        ambiguous_html = """
          <script type="application/ld+json">
            {"@context":"https://schema.org","@type":"Product",
             "name":"Trail 100"}
          </script>
        """
        editorial_html = """
          <meta property="og:type" content="article">
          <article><h1>Trail 100 Review</h1></article>
        """
        research_page.side_effect = lambda url, **_: SimpleNamespace(
            url=url,
            html=ambiguous_html if url == ambiguous_url else editorial_html,
            product=product(product_id=f"product-{url}", source_url=url),
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(result.status, "not_found")
        self.assertEqual(
            [candidate.source_classification for candidate in result.candidates],
            ["ambiguous", "editorial_or_review"],
        )
        self.assertTrue(
            all(
                candidate.status == "rejected_source_type"
                for candidate in result.candidates
            )
        )

    @patch("supplementary_product_discovery.research_product_page")
    def test_lower_rank_direct_url_wins_within_same_publisher(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        redirect_url = "https://retailer.example/go/trail-100"
        redirected_final = "https://shop.retailer.example/products/trail-100"
        direct_url = "https://www.retailer.example/products/trail-100-special"
        provider = FakeSearchProvider(
            [
                ProductPageSearchResult(url=redirect_url),
                ProductPageSearchResult(url=direct_url),
            ]
        )
        final_urls = {
            redirect_url: redirected_final,
            direct_url: direct_url,
        }
        research_page.side_effect = lambda url, **_: SimpleNamespace(
            url=final_urls[url],
            html=direct_retailer_html(final_urls[url]),
            product=product(
                product_id=f"product-{url}",
                source_url=final_urls[url],
            ),
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(len(result.proposals), 1)
        self.assertEqual(result.proposals[0].discovered_url, direct_url)
        self.assertEqual(
            [candidate.status for candidate in result.candidates],
            ["duplicate_publisher", "accepted"],
        )

    @patch("supplementary_product_discovery.research_product_page")
    def test_candidate_failure_continues_to_later_result(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        failed_url = "https://one.example/products/trail-100"
        accepted_url = "https://retailer.example/products/trail-100"
        research_page.side_effect = [
            ProductFetchError("timed out"),
            SimpleNamespace(
                url=accepted_url,
                html=DIRECT_RETAILER_HTML,
                product=product(
                    product_id="retailer",
                    source_url=accepted_url,
                ),
            ),
        ]

        result = discover_supplementary_product_sources(
            primary,
            FakeSearchProvider(
                [
                    ProductPageSearchResult(url=failed_url),
                    ProductPageSearchResult(url=accepted_url),
                ]
            ),
        )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.candidates[0].status, "research_failed")
        self.assertEqual(result.candidates[0].reason, "timed out")
        self.assertEqual(research_page.call_count, 2)

    @patch("supplementary_product_discovery.research_product_page")
    def test_duplicate_urls_are_fetched_once(self, research_page: Mock) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        canonical_url = "https://retailer.example/products/trail-100"
        research_page.return_value = SimpleNamespace(
            url=canonical_url,
            html=DIRECT_RETAILER_HTML,
            product=product(product_id="retailer", source_url=canonical_url),
        )
        provider = FakeSearchProvider(
            [
                ProductPageSearchResult(
                    url="https://www.retailer.example/products/trail-100?utm_source=x"
                ),
                ProductPageSearchResult(
                    url="http://retailer.example/products/trail-100"
                ),
            ]
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(result.status, "found")
        self.assertEqual(len(result.candidates), 1)
        research_page.assert_called_once()

    @patch("supplementary_product_discovery.research_product_page")
    def test_six_fetch_limit_is_enforced(self, research_page: Mock) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        urls = [
            f"https://retailer-{index}.example/products/trail-100"
            for index in range(8)
        ]
        provider = FakeSearchProvider(
            [ProductPageSearchResult(url=url) for url in urls]
        )
        research_page.side_effect = lambda url, **_: SimpleNamespace(
            url=url,
            html=direct_retailer_html(url),
            product=product(
                product_id=f"wrong-{url}",
                source_url=url,
                name="Wrong Product",
            ),
        )

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(research_page.call_count, 6)
        self.assertEqual(
            [candidate.status for candidate in result.candidates[-2:]],
            ["not_fetched", "not_fetched"],
        )

    def test_three_queries_and_ten_results_per_query_are_enforced(self) -> None:
        consumed = []

        class GeneratedProvider:
            def __init__(self):
                self.calls = []

            def search(self, query: str, *, limit: int):
                call_index = len(self.calls)
                self.calls.append((query, limit))

                def generated_results():
                    for index in range(20):
                        consumed.append((call_index, index))
                        yield ProductPageSearchResult(
                            url=(
                                "https://social.example/reviews/"
                                f"{call_index}-{index}"
                            )
                        )

                return generated_results()

        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
            model_number="A3100",
        )
        provider = GeneratedProvider()

        result = discover_supplementary_product_sources(primary, provider)

        self.assertEqual(len(provider.calls), 3)
        self.assertTrue(all(limit == 10 for _, limit in provider.calls))
        self.assertEqual(len(consumed), 30)
        self.assertEqual(
            [sum(call_index == index for call_index, _ in consumed) for index in range(3)],
            [10, 10, 10],
        )
        self.assertTrue(all("A3100" in query for query, _ in provider.calls))
        self.assertEqual(result.status, "not_found")

    def test_search_provider_failures_are_explicit(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        with self.assertRaisesRegex(
            OfficialProductPageSearchError,
            "provider unavailable",
        ):
            discover_supplementary_product_sources(
                primary,
                FakeSearchProvider(RuntimeError("provider unavailable")),
            )

        def lazy_failure():
            yield ProductPageSearchResult(url="https://one.example/product")
            raise RuntimeError("lazy failure")

        with self.assertRaisesRegex(OfficialProductPageSearchError, "lazy failure"):
            discover_supplementary_product_sources(
                primary,
                FakeSearchProvider(lazy_failure()),
            )

    @patch("supplementary_product_discovery.research_product_page")
    def test_obvious_unsuitable_urls_do_not_consume_fetches(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        urls = [
            "https://youtube.com/watch?v=trail100",
            "https://publisher.example/reviews/trail-100",
            "https://retailer.example/support/trail-100",
            "https://retailer.example/manual.pdf",
        ]

        result = discover_supplementary_product_sources(
            primary,
            FakeSearchProvider(
                [ProductPageSearchResult(url=url) for url in urls]
            ),
        )

        self.assertEqual(result.status, "not_found")
        self.assertTrue(
            all(candidate.status == "rejected_url" for candidate in result.candidates)
        )
        research_page.assert_not_called()

    @patch("supplementary_product_discovery.research_product_page")
    def test_unsuitable_final_redirect_url_is_rejected(
        self,
        research_page: Mock,
    ) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
        )
        discovered_url = "https://retailer.example/go/trail-100"
        final_url = "https://retailer.example/reviews/trail-100"
        research_page.return_value = SimpleNamespace(
            url=final_url,
            html=direct_retailer_html(final_url),
            product=product(product_id="retailer", source_url=final_url),
        )

        result = discover_supplementary_product_sources(
            primary,
            FakeSearchProvider([ProductPageSearchResult(url=discovered_url)]),
        )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.candidates[0].status, "rejected_url")
        self.assertEqual(result.candidates[0].reason, "clearly_non_product_path")
        self.assertEqual(result.candidates[0].final_url, final_url)

    def test_models_round_trip_and_reject_unknown_fields(self) -> None:
        candidate = SupplementarySourceCandidate(
            discovered_url="https://retailer.example/products/trail-100",
            query="Acme Audio Trail 100",
            search_rank=1,
            status="rejected_url",
            reason="clearly_non_product_path",
        )
        result = SupplementaryProductSourceDiscovery(
            primary_product_id="primary",
            primary_url="https://acmeaudio.example/products/trail-100",
            status="not_found",
            queries=["Acme Audio Trail 100"],
            candidates=[candidate],
            reason="no_direct_retailer_product_sources_accepted",
        )

        self.assertEqual(
            SupplementaryProductSourceDiscovery.model_validate_json(
                result.model_dump_json()
            ),
            result,
        )
        self.assertEqual(
            SupplementarySourceCandidate.model_validate_json(
                candidate.model_dump_json()
            ),
            candidate,
        )
        with self.assertRaises(ValidationError):
            SupplementarySourceCandidate.model_validate(
                {**candidate.model_dump(), "unexpected": True}
            )
        with self.assertRaises(ValidationError):
            SupplementarySourceCandidate(
                discovered_url="https://retailer.example/products/trail-100",
                query="Acme Audio Trail 100",
                search_rank=1,
                status="accepted",
                reason="exact_product_direct_retailer",
            )
        with self.assertRaises(ValidationError):
            SupplementaryProductSourceDiscovery(
                primary_product_id="primary",
                primary_url="https://acmeaudio.example/products/trail-100",
                status="found",
                queries=["Acme Audio Trail 100"],
                proposals=[
                    SupplementarySourceProposal(
                        discovered_url=(
                            "https://retailer.example/products/trail-100"
                        ),
                        accepted_url=(
                            "https://retailer.example/products/trail-100"
                        ),
                        publisher="retailer.example",
                        product=product(
                            product_id="retailer",
                            source_url=(
                                "https://retailer.example/products/trail-100"
                            ),
                        ),
                    )
                ],
                candidates=[],
                reason="direct_retailer_product_sources_proposed",
            )


class SupplementarySourceClassificationTests(unittest.TestCase):
    def classify(self, html: str):
        return classify_supplementary_product_page(
            html,
            "https://retailer.example/products/trail-100",
        )

    def test_multi_seller_aggregate_offer_is_marketplace(self) -> None:
        html = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Trail 100",
          "offers": {
            "@type": "AggregateOffer",
            "offerCount": 2,
            "lowPrice": "89",
            "highPrice": "109",
            "offers": [
              {"@type": "Offer", "seller": {"name": "Seller One"}},
              {"@type": "Offer", "seller": {"name": "Seller Two"}}
            ]
          }
        }
        </script>
        <meta property="product:price:amount" content="89">
        <button>Add to cart</button>
        """

        classification = self.classify(html)

        self.assertEqual(
            classification.source_type,
            "marketplace_or_third_party",
        )
        self.assertIn("multiple_sellers", classification.reasons)

    def test_single_seller_not_matching_publisher_is_ambiguous(self) -> None:
        html = DIRECT_RETAILER_HTML.replace(
            '"name": "Retailer"',
            '"name": "Unrelated Seller"',
        )

        classification = self.classify(html)

        self.assertEqual(classification.source_type, "ambiguous")
        self.assertIn("seller_publisher_mismatch", classification.reasons)

    def test_partial_seller_name_cannot_impersonate_publisher(self) -> None:
        html = DIRECT_RETAILER_HTML.replace(
            '"name": "Retailer"',
            '"name": "Tail"',
        )

        classification = self.classify(html)

        self.assertEqual(classification.source_type, "ambiguous")
        self.assertIn("seller_publisher_mismatch", classification.reasons)

    def test_primary_article_page_is_editorial(self) -> None:
        html = """
        <meta property="og:type" content="article">
        <script type="application/ld+json">
          {"@context": "https://schema.org", "@type": "NewsArticle",
           "headline": "Trail 100 launch coverage"}
        </script>
        <article><h1>Trail 100 launch coverage</h1></article>
        """

        classification = self.classify(html)

        self.assertEqual(classification.source_type, "editorial_or_review")
        self.assertIn("primary_editorial_schema", classification.reasons)

    def test_primary_review_page_is_editorial(self) -> None:
        html = """
        <script type="application/ld+json">
          {"@context": "https://schema.org", "@type": "Review",
           "itemReviewed": {"@type": "Product", "name": "Trail 100"}}
        </script>
        <article><h1>Trail 100 Review</h1></article>
        """

        self.assertEqual(
            self.classify(html).source_type,
            "editorial_or_review",
        )

    def test_comparison_or_buying_guide_article_is_editorial(self) -> None:
        for heading in (
            "Trail 100 versus Ridge 200 comparison",
            "Portable speaker buying guide",
        ):
            with self.subTest(heading=heading):
                html = f"<article><h1>{heading}</h1><p>Editorial copy.</p></article>"
                self.assertEqual(
                    self.classify(html).source_type,
                    "editorial_or_review",
                )

    def test_customer_reviews_do_not_turn_product_page_into_editorial(self) -> None:
        html = DIRECT_RETAILER_HTML.replace(
            "</main>",
            "<section><h2>Customer Reviews</h2><p>Great speaker.</p></section></main>",
        )

        classification = self.classify(html)

        self.assertEqual(
            classification.source_type,
            "direct_retailer_product",
        )

    def test_product_without_enough_commerce_signals_is_ambiguous(self) -> None:
        html = """
        <script type="application/ld+json">
          {"@context": "https://schema.org", "@type": "Product",
           "name": "Trail 100"}
        </script>
        <h1>Trail 100</h1>
        """

        classification = self.classify(html)

        self.assertEqual(classification.source_type, "ambiguous")

    def test_primary_article_with_embedded_product_is_still_editorial(self) -> None:
        html = """
        <script type="application/ld+json">
        {
          "@context":"https://schema.org",
          "@graph":[
            {"@type":"Article","headline":"Trail 100 Review",
             "mainEntityOfPage":"https://retailer.example/products/trail-100"},
            {"@type":"Product","name":"Trail 100","offers":{
              "@type":"Offer","price":"99","priceCurrency":"USD"
            }}
          ]
        }
        </script>
        <meta property="product:price:amount" content="99">
        <button>Add to cart</button>
        """

        classification = self.classify(html)

        self.assertEqual(classification.source_type, "editorial_or_review")
        self.assertIn("primary_editorial_schema", classification.reasons)

    def test_visible_multiple_sellers_are_marketplace(self) -> None:
        html = DIRECT_RETAILER_HTML.replace(
            ',\n          "seller": {"@type": "Organization", "name": "Retailer"}',
            "",
        ).replace(
            "</main>",
            """
            <section aria-label="Other sellers">
              <div data-seller="Seller One">Sold by Seller One</div>
              <div data-seller="Seller Two">Sold by Seller Two</div>
            </section></main>
            """,
        )

        classification = self.classify(html)

        self.assertEqual(
            classification.source_type,
            "marketplace_or_third_party",
        )
        self.assertIn("multiple_sellers", classification.reasons)


if __name__ == "__main__":
    unittest.main()
