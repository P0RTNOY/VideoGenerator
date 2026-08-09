import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from product import ProductVariant, load_product

import product_research
import research_product as research_cli


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ProductResearchIdentityTests(unittest.TestCase):
    def test_slug_generation_uses_brand_name_and_variant(self) -> None:
        research = product_research
        variant = ProductVariant(color="Forést Green", size="Mini")

        product_id = research.generate_product_id(
            "Acme Audio",
            "Trail Speaker",
            variant,
        )

        self.assertEqual(
            product_id,
            "acme-audio-trail-speaker-forest-green-mini",
        )

    def test_direct_jsonld_builds_product_identity_and_source(self) -> None:
        research = product_research
        source_url = "https://shop.example/products/trail-speaker"

        product = research.research_product_from_html(
            fixture_text("jsonld_product.html"),
            source_url,
        )

        self.assertEqual(product.id, "acme-audio-trail-speaker-forest-green")
        self.assertEqual(product.name, "Trail Speaker")
        self.assertEqual(product.brand, "Acme Audio")
        self.assertEqual(product.category, "Portable Speakers")
        self.assertEqual(product.description, "A compact speaker for outdoor use.")
        self.assertEqual(product.variant.color, "Forest Green")
        self.assertEqual(product.variant.model_number, "Trail 100")
        self.assertEqual(product.variant.sku, "TS-100")
        self.assertEqual(product.images, [])
        self.assertEqual(len(product.sources), 1)
        self.assertEqual(product.sources[0].url, source_url)
        self.assertEqual(product.sources[0].domain, "shop.example")
        self.assertEqual(product.sources[0].title, "Trail Speaker | Acme Audio")
        self.assertEqual(product.sources[0].source_type, "product_page")


class ProductResearchExtractionTests(unittest.TestCase):
    def test_jsonld_and_table_specs_are_deduplicated_by_confidence(self) -> None:
        research = product_research
        source_url = "https://shop.example/products/trail-speaker"

        product = research.research_product_from_html(
            fixture_text("jsonld_product.html"),
            source_url,
        )
        specs = {spec.key: spec for spec in product.specs}

        self.assertEqual(set(specs), {"battery_life", "weight"})
        self.assertEqual(specs["battery_life"].value, 20)
        self.assertEqual(specs["battery_life"].unit, "hours")
        self.assertEqual(specs["battery_life"].confidence, 0.95)
        self.assertEqual(specs["weight"].value, 450)
        self.assertEqual(specs["weight"].unit, "g")
        self.assertEqual(specs["weight"].confidence, 0.85)
        for spec in specs.values():
            self.assertEqual(spec.source_url, source_url)
            self.assertFalse(spec.verified)

    def test_jsonld_and_explicit_section_features_are_conservative_and_deduplicated(self) -> None:
        research = product_research

        product = research.research_product_from_html(
            fixture_text("jsonld_product.html"),
            "https://shop.example/products/trail-speaker",
        )
        features = {feature.name: feature for feature in product.features}

        self.assertEqual(
            set(features),
            {"Rugged carry loop", "Simple controls", "One-button control"},
        )
        self.assertEqual(features["Rugged carry loop"].confidence, 0.90)
        self.assertEqual(features["Simple controls"].confidence, 0.90)
        self.assertEqual(features["One-button control"].confidence, 0.75)
        self.assertNotIn(
            "This ordinary marketing paragraph must not become a feature.",
            features,
        )
        for feature in features.values():
            self.assertFalse(feature.verified)

    def test_definition_list_specs_and_collection_feature_list_are_supported(self) -> None:
        research = product_research

        product = research.research_product_from_html(
            fixture_text("jsonld_graph.html"),
            "https://example.com/products/select-go",
        )
        specs = {spec.key: spec for spec in product.specs}

        self.assertEqual(set(specs), {"battery_life", "water_resistance"})
        self.assertEqual(specs["battery_life"].value, 12)
        self.assertEqual(specs["battery_life"].unit, "hours")
        self.assertIs(specs["water_resistance"].value, True)
        self.assertEqual(specs["water_resistance"].confidence, 0.85)
        self.assertEqual(
            {feature.name for feature in product.features},
            {"Compact design", "Easy controls"},
        )

    def test_single_additional_property_object_is_supported(self) -> None:
        research = product_research

        product = research.research_product_from_html(
            fixture_text("jsonld_array.html"),
            "https://array.example/products/array-speaker",
        )

        self.assertEqual(len(product.specs), 1)
        self.assertEqual(product.specs[0].key, "bluetooth_version")
        self.assertEqual(product.specs[0].value, "5.3")
        self.assertIsNone(product.specs[0].unit)
        self.assertEqual(product.specs[0].confidence, 0.95)

    def test_incomplete_product_identity_raises_clear_extraction_error(self) -> None:
        research = product_research

        with self.assertRaises(research.ProductExtractionError) as context:
            research.research_product_from_html(
                fixture_text("incomplete_product.html"),
                "https://incomplete.example/products/speaker",
            )

        self.assertIn("brand", str(context.exception))

    def test_graph_selection_prefers_current_page_product_before_completeness(self) -> None:
        research = product_research
        final_url = "https://example.com/products/select-go?campaign=local"

        product = research.research_product_from_html(
            fixture_text("jsonld_graph.html"),
            final_url,
        )

        self.assertEqual(product.name, "Select Go")
        self.assertNotEqual(product.name, "Related Deluxe Speaker")
        self.assertEqual(product.brand, "Example Audio")
        self.assertEqual(product.category, "Bluetooth Speakers")
        self.assertEqual(product.sources[0].url, final_url)

    def test_query_string_url_selects_current_product_and_model_falls_back_to_mpn(self) -> None:
        research = product_research

        product = research.research_product_from_html(
            fixture_text("jsonld_query_products.html"),
            "https://query.example/product?id=A",
        )

        self.assertEqual(product.name, "Query Speaker A")
        self.assertEqual(product.category, "Portable Speakers")
        self.assertEqual(product.variant.model_number, "A-100")

    def test_breadcrumb_positions_and_item_url_are_resolved_deterministically(self) -> None:
        research = product_research

        try:
            product = research.research_product_from_html(
                fixture_text("jsonld_breadcrumb_positions.html"),
                "https://position.example/product",
            )
        except Exception as error:
            self.fail(f"Breadcrumb extraction raised {type(error).__name__}: {error}")

        self.assertEqual(product.category, "Portable Speakers")

    def test_top_level_jsonld_array_is_supported(self) -> None:
        research = product_research

        product = research.research_product_from_html(
            fixture_text("jsonld_array.html"),
            "https://array.example/products/array-speaker",
        )

        self.assertEqual(product.name, "Array Speaker")
        self.assertEqual(product.brand, "Array Audio")
        self.assertEqual(product.category, "Desktop Speakers")

    def test_product_meta_tags_are_used_without_product_jsonld(self) -> None:
        research = product_research

        product = research.research_product_from_html(
            fixture_text("meta_product.html"),
            "https://meta.example/products/meta-speaker",
        )

        self.assertEqual(product.name, "Meta Speaker")
        self.assertEqual(product.brand, "Meta Audio")
        self.assertEqual(product.category, "Bookshelf Speakers")
        self.assertEqual(
            product.description,
            "A product represented by metadata only.",
        )

    def test_malformed_jsonld_is_ignored_and_html_breadcrumb_supplies_category(self) -> None:
        research = product_research

        product = research.research_product_from_html(
            fixture_text("malformed_jsonld_breadcrumb.html"),
            "https://crumb.example/products/breadcrumb-speaker",
        )

        self.assertEqual(product.name, "Breadcrumb Speaker")
        self.assertEqual(product.brand, "Crumb Audio")
        self.assertEqual(product.category, "Portable Speakers")


class ProductGroupResearchTests(unittest.TestCase):
    def test_direct_product_group_uses_group_identity_and_its_only_variant(self) -> None:
        source_url = "https://group.example/products/family-speaker"

        product = product_research.research_product_from_html(
            fixture_text("product_group_direct.html"),
            source_url,
        )

        self.assertEqual(product.id, "group-audio-family-speaker-green-mini")
        self.assertEqual(product.name, "Family Speaker")
        self.assertEqual(product.brand, "Group Audio")
        self.assertEqual(product.category, "Portable Speakers")
        self.assertEqual(product.description, "A family of compact speakers.")
        self.assertIsNotNone(product.variant)
        self.assertEqual(product.variant.color, "Green")
        self.assertEqual(product.variant.size, "Mini")
        self.assertEqual(product.variant.sku, "FS-GREEN")
        self.assertEqual(product.variant.model_number, "FS100")
        self.assertEqual(product.sources[0].url, source_url)

    def test_group_and_selected_variant_specs_and_features_are_deduplicated(self) -> None:
        source_url = "https://group.example/products/family-speaker"

        product = product_research.research_product_from_html(
            fixture_text("product_group_direct.html"),
            source_url,
        )
        specs = {spec.key: spec for spec in product.specs}
        features = {feature.id: feature for feature in product.features}

        self.assertEqual(set(specs), {"battery_life", "weight"})
        self.assertEqual(specs["battery_life"].value, 10)
        self.assertEqual(specs["battery_life"].unit, "hours")
        self.assertEqual(specs["weight"].value, 200)
        self.assertEqual(specs["weight"].unit, "g")
        self.assertEqual(
            set(features),
            {"balanced-sound", "carry-loop", "simple-controls"},
        )
        self.assertEqual(features["carry-loop"].name, "Carry loop")
        for spec in specs.values():
            self.assertEqual(spec.source_url, source_url)
            self.assertFalse(spec.verified)
        for feature in features.values():
            self.assertEqual(feature.source_url, source_url)
            self.assertFalse(feature.verified)

    def test_product_group_in_top_level_array_is_supported(self) -> None:
        product = product_research.research_product_from_html(
            fixture_text("product_group_array.html"),
            "https://array-group.example/products/array-group-speaker",
        )

        self.assertEqual(product.name, "Array Group Speaker")
        self.assertEqual(product.brand, "Array Group Audio")
        self.assertEqual(product.category, "Desktop Speakers")
        self.assertIsNone(product.variant)

    def test_graph_product_group_selects_only_url_matching_variant(self) -> None:
        source_url = (
            "https://graph-group.example/products/graph-speaker?variant=green"
        )

        product = product_research.research_product_from_html(
            fixture_text("product_group_graph_url.html"),
            source_url,
        )

        self.assertEqual(product.name, "Graph Speaker")
        self.assertEqual(product.brand, "Graph Audio")
        self.assertEqual(product.category, "Wireless Speakers")
        self.assertIsNotNone(product.variant)
        self.assertEqual(product.variant.color, "Green")
        self.assertEqual(product.variant.sku, "GRAPH-GREEN")
        self.assertEqual(product.sources[0].url, source_url)

    def test_main_entity_selects_referenced_variant_and_breadcrumb_category(self) -> None:
        product = product_research.research_product_from_html(
            fixture_text("product_group_main_entity.html"),
            "https://entity-group.example/products/entity-speaker",
        )

        self.assertEqual(product.name, "Entity Speaker")
        self.assertEqual(product.brand, "Entity Audio")
        self.assertEqual(product.category, "Bluetooth Speakers")
        self.assertIsNotNone(product.variant)
        self.assertEqual(product.variant.color, "Green")
        self.assertEqual(product.variant.sku, "ENTITY-GREEN")

    def test_ambiguous_group_variants_do_not_leak_arbitrary_variant_data(self) -> None:
        product = product_research.research_product_from_html(
            fixture_text("product_group_ambiguous.html"),
            "https://choice-group.example/products/choice-speaker",
        )

        self.assertEqual(product.id, "choice-audio-choice-speaker")
        self.assertEqual(product.name, "Choice Speaker")
        self.assertEqual(product.brand, "Choice Audio")
        self.assertEqual(product.category, "Portable Speakers")
        self.assertIsNone(product.variant)
        self.assertEqual(product.specs, [])
        self.assertEqual(
            [feature.name for feature in product.features],
            ["Shared controls"],
        )

    def test_product_group_missing_required_identity_fails_cleanly(self) -> None:
        with self.assertRaises(product_research.ProductExtractionError) as context:
            product_research.research_product_from_html(
                fixture_text("product_group_missing_identity.html"),
                "https://missing-group.example/products/incomplete",
            )

        self.assertIn("brand", str(context.exception))


class ProductResearchFetchTests(unittest.TestCase):
    def test_fetch_uses_headers_timeout_redirects_and_preserves_final_url(self) -> None:
        research = product_research
        session = Mock()
        response = Mock()
        response.url = "https://shop.example/final-product"
        response.headers = {"Content-Type": "text/html; charset=UTF-8"}
        response.text = "<html><title>Product</title></html>"
        session.get.return_value = response

        page = research.fetch_product_page(
            "https://shop.example/original-product",
            timeout=7,
            session=session,
        )

        self.assertEqual(page.url, response.url)
        self.assertEqual(page.html, response.text)
        session.get.assert_called_once_with(
            "https://shop.example/original-product",
            headers=research.REQUEST_HEADERS,
            timeout=7,
            allow_redirects=True,
        )
        response.raise_for_status.assert_called_once_with()

    def test_invalid_urls_are_rejected_before_request(self) -> None:
        research = product_research
        session = Mock()

        for invalid_url in (
            "ftp://example.com/product",
            "https:///missing-host",
            "https://user:password@example.com/product",
        ):
            with self.subTest(url=invalid_url):
                with self.assertRaises(research.InvalidProductUrlError):
                    research.fetch_product_page(invalid_url, session=session)

        session.get.assert_not_called()

    def test_timeout_and_connection_failures_raise_clear_fetch_errors(self) -> None:
        research = product_research

        for request_error, expected_text in (
            (requests.Timeout("slow"), "timed out"),
            (requests.ConnectionError("offline"), "connect"),
        ):
            with self.subTest(error=type(request_error).__name__):
                session = Mock()
                session.get.side_effect = request_error
                with self.assertRaises(research.ProductFetchError) as context:
                    research.fetch_product_page(
                        "https://example.com/product",
                        session=session,
                    )
                self.assertIn(expected_text, str(context.exception).casefold())

    def test_http_and_non_html_responses_raise_clear_fetch_errors(self) -> None:
        research = product_research

        http_session = Mock()
        http_response = Mock()
        http_response.status_code = 404
        http_response.raise_for_status.side_effect = requests.HTTPError("not found")
        http_session.get.return_value = http_response
        with self.assertRaises(research.ProductFetchError) as http_context:
            research.fetch_product_page(
                "https://example.com/missing",
                session=http_session,
            )
        self.assertIn("404", str(http_context.exception))

        json_session = Mock()
        json_response = Mock()
        json_response.url = "https://example.com/product.json"
        json_response.headers = {"Content-Type": "application/json"}
        json_session.get.return_value = json_response
        with self.assertRaises(research.ProductFetchError) as content_context:
            research.fetch_product_page(
                "https://example.com/product.json",
                session=json_session,
            )
        self.assertIn("HTML", str(content_context.exception))

    def test_http_error_raised_directly_by_client_remains_a_fetch_error(self) -> None:
        research = product_research
        session = Mock()
        response = Mock(status_code=503)
        request_error = requests.HTTPError("unavailable", response=response)
        session.get.side_effect = request_error

        caught_error = None
        try:
            research.fetch_product_page(
                "https://example.com/product",
                session=session,
            )
        except Exception as error:  # The assertion below verifies the public error type.
            caught_error = error

        self.assertIsInstance(caught_error, research.ProductFetchError)
        self.assertIn("503", str(caught_error))

    def test_research_product_parses_the_final_redirected_page(self) -> None:
        research = product_research
        session = Mock()
        response = Mock()
        response.url = "https://meta.example/products/meta-speaker"
        response.headers = {"Content-Type": "text/html"}
        response.text = fixture_text("meta_product.html")
        session.get.return_value = response

        product = research.research_product(
            "https://meta.example/go/meta-speaker",
            session=session,
        )

        self.assertEqual(product.name, "Meta Speaker")
        self.assertEqual(product.sources[0].url, response.url)


class ProductResearchCliTests(unittest.TestCase):
    def test_cli_saves_researched_product_and_prints_summary(self) -> None:
        product = product_research.research_product_from_html(
            fixture_text("jsonld_product.html"),
            "https://shop.example/products/trail-speaker",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "researched_product.json"
            standard_output = io.StringIO()
            with patch.object(
                research_cli,
                "research_product",
                return_value=product,
            ), redirect_stdout(standard_output):
                exit_code = research_cli.main(
                    [
                        "https://shop.example/products/trail-speaker",
                        "--output",
                        str(output_path),
                    ]
                )

            loaded = load_product(output_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(loaded, product)
        summary = standard_output.getvalue()
        self.assertIn("Acme Audio Trail Speaker", summary)
        self.assertIn("2 specs", summary)
        self.assertIn("3 features", summary)
        self.assertIn("1 source", summary)
        self.assertIn(str(output_path), summary)

    def test_cli_reports_research_errors_without_writing_output(self) -> None:
        standard_error = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "missing.json"
            with patch.object(
                research_cli,
                "research_product",
                side_effect=product_research.ProductFetchError("offline"),
            ), redirect_stderr(standard_error):
                exit_code = research_cli.main(
                    [
                        "https://shop.example/products/trail-speaker",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertFalse(output_path.exists())

        self.assertEqual(exit_code, 1)
        self.assertIn("offline", standard_error.getvalue())


if __name__ == "__main__":
    unittest.main()
