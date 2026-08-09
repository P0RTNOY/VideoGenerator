import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timezone
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
    def test_jsonld_specs_and_features_include_source_evidence(self) -> None:
        source_url = "https://shop.example/products/trail-speaker"

        product = product_research.research_product_from_html(
            fixture_text("jsonld_product.html"),
            source_url,
        )
        battery = next(spec for spec in product.specs if spec.key == "battery_life")
        rugged_loop = next(
            feature
            for feature in product.features
            if feature.name == "Rugged carry loop"
        )

        self.assertIsNotNone(battery.evidence)
        self.assertEqual(battery.evidence.raw_text, "Battery Life: 20 hours")
        self.assertEqual(
            battery.evidence.extraction_method,
            "jsonld_additional_property",
        )
        self.assertEqual(battery.evidence.source_url, source_url)
        self.assertEqual(battery.evidence.extracted_at.tzinfo, timezone.utc)
        self.assertIsNotNone(rugged_loop.evidence)
        self.assertEqual(rugged_loop.evidence.raw_text, "Rugged carry loop")
        self.assertEqual(
            rugged_loop.evidence.extraction_method,
            "jsonld_feature_list",
        )
        self.assertEqual(rugged_loop.evidence.source_url, source_url)

    def test_existing_html_extractors_include_section_evidence(self) -> None:
        source_url = "https://shop.example/products/trail-speaker"
        table_product = product_research.research_product_from_html(
            fixture_text("jsonld_product.html"),
            source_url,
        )
        table_spec = next(
            spec for spec in table_product.specs if spec.key == "weight"
        )
        html_feature = next(
            feature
            for feature in table_product.features
            if feature.name == "One-button control"
        )

        self.assertIsNotNone(table_spec.evidence)
        self.assertEqual(table_spec.evidence.raw_text, "Weight 450 g")
        self.assertEqual(table_spec.evidence.extraction_method, "html_spec_table")
        self.assertEqual(table_spec.evidence.section_heading, "Specifications")
        self.assertIsNotNone(html_feature.evidence)
        self.assertEqual(
            html_feature.evidence.raw_text,
            "One-button control Keeps operation simple.",
        )
        self.assertEqual(
            html_feature.evidence.extraction_method,
            "explicit_feature_section",
        )
        self.assertEqual(html_feature.evidence.section_heading, "Features")

        definition_url = "https://example.com/products/select-go"
        definition_product = product_research.research_product_from_html(
            fixture_text("jsonld_graph.html"),
            definition_url,
        )
        definition_spec = next(
            spec
            for spec in definition_product.specs
            if spec.key == "water_resistance"
        )

        self.assertIsNotNone(definition_spec.evidence)
        self.assertEqual(
            definition_spec.evidence.raw_text,
            "Water Resistance Yes",
        )
        self.assertEqual(
            definition_spec.evidence.extraction_method,
            "html_definition_list",
        )
        self.assertEqual(
            definition_spec.evidence.section_heading,
            "Technical details",
        )

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
        self.assertEqual(
            specs["battery_life"].evidence.extraction_method,
            "jsonld_additional_property",
        )
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
        self.assertEqual(
            features["Rugged carry loop"].evidence.extraction_method,
            "jsonld_feature_list",
        )
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


class PrimaryProductClaimResearchTests(unittest.TestCase):
    def test_unique_product_h1_admits_primary_list_and_extracts_ip_evidence(
        self,
    ) -> None:
        source_url = "https://claims.example/products/trail-speaker-pro"

        product = product_research.research_product_from_html(
            fixture_text("primary_claims.html"),
            source_url,
        )
        specs = {spec.key: spec for spec in product.specs}

        self.assertIn("water_resistance", specs)
        ip_spec = specs["water_resistance"]
        self.assertEqual(ip_spec.label, "Water Resistance")
        self.assertEqual(ip_spec.value, "IP67")
        self.assertIsNone(ip_spec.unit)
        self.assertEqual(ip_spec.confidence, 0.65)
        self.assertFalse(ip_spec.verified)
        self.assertEqual(ip_spec.source_url, source_url)
        self.assertIsNotNone(ip_spec.evidence)
        self.assertEqual(
            ip_spec.evidence.raw_text,
            "IP67 Protection: IP67 rated for water protection and can float. "
            "It can be immersed in up to 3.3 feet for up to 30 minutes.",
        )
        self.assertEqual(
            ip_spec.evidence.extraction_method,
            "primary_product_claim_list",
        )
        self.assertEqual(ip_spec.evidence.section_heading, "Acme Trail Speaker Pro")
        self.assertEqual(ip_spec.evidence.source_url, source_url)
        self.assertNotIn("bluetooth_version", specs)

    def test_primary_claim_classifier_emits_only_supported_qualified_facts(
        self,
    ) -> None:
        source_url = "https://claims.example/products/trail-speaker-pro"

        product = product_research.research_product_from_html(
            fixture_text("primary_claims.html"),
            source_url,
        )
        specs = {spec.key: spec for spec in product.specs}
        features = {feature.id: feature for feature in product.features}

        self.assertEqual(
            set(specs),
            {
                "water_resistance",
                "playback_time",
                "output_power",
                "immersion_protection",
            },
        )
        self.assertEqual(specs["playback_time"].value, "up to 20 hours")
        self.assertIsNone(specs["playback_time"].unit)
        self.assertEqual(specs["output_power"].value, 5)
        self.assertEqual(specs["output_power"].unit, "W")
        self.assertEqual(
            specs["immersion_protection"].value,
            "up to 3.3 feet for up to 30 minutes",
        )
        self.assertIsNone(specs["immersion_protection"].unit)

        self.assertEqual(
            set(features),
            {
                "floating-design",
                "included-carrying-strap",
                "true-wireless-stereo",
            },
        )
        self.assertEqual(features["floating-design"].name, "Floating design")
        self.assertEqual(
            features["included-carrying-strap"].name,
            "Included carrying strap",
        )
        tws = features["true-wireless-stereo"]
        self.assertEqual(tws.name, "True Wireless Stereo")
        self.assertIn("requires two speakers", tws.description)
        self.assertIn("2 seconds", tws.description)

        ip_raw_text = specs["water_resistance"].evidence.raw_text
        self.assertEqual(
            specs["immersion_protection"].evidence.raw_text,
            ip_raw_text,
        )
        self.assertEqual(features["floating-design"].evidence.raw_text, ip_raw_text)
        for claim in [*specs.values(), *features.values()]:
            self.assertEqual(claim.confidence, 0.65)
            self.assertFalse(claim.verified)
            self.assertEqual(claim.source_url, source_url)
            self.assertEqual(claim.evidence.source_url, source_url)
            self.assertEqual(
                claim.evidence.extraction_method,
                "primary_product_claim_list",
            )

        self.assertNotIn("bluetooth_version", specs)
        self.assertNotIn("lightweight", features)
        self.assertNotIn("powerful-sound", features)
        self.assertNotIn("punchy-sound", features)

    def test_primary_list_selection_is_css_agnostic(self) -> None:
        product = product_research.research_product_from_html(
            fixture_text("primary_claims_alt_classes.html"),
            "https://alternate.example/products/studio-mini",
        )

        specs = {spec.key: spec for spec in product.specs}
        features = {feature.id: feature for feature in product.features}
        self.assertIn("water_resistance", specs)
        self.assertEqual(specs["water_resistance"].value, "IP68")
        self.assertEqual(specs["playback_time"].value, "approximately 8 hours")
        self.assertIn("floating-design", features)

    def test_marketing_links_overlays_and_excluded_sections_are_rejected(
        self,
    ) -> None:
        html = fixture_text("primary_claims_rejections.html")
        soup = product_research.BeautifulSoup(html, "html.parser")
        product_h1 = product_research._find_primary_product_h1(
            soup,
            "Quiet Speaker",
            "Generic Audio",
        )

        self.assertIsNotNone(product_h1)
        for list_id in (
            "navigation-list",
            "before-title-list",
            "marketing-only-list",
            "mostly-links-list",
            "purchase-steps-list",
            "promotion-list",
            "unheaded-reviews-list",
            "overlay-list",
            "shipping-list",
            "returns-list",
            "reviews-list",
            "questions-list",
            "recommendations-list",
            "comparison-list",
            "footer-list",
        ):
            with self.subTest(list_id=list_id):
                candidate = soup.find(id=list_id)
                self.assertFalse(
                    product_research._is_primary_claim_list(candidate, product_h1)
                )

        product = product_research.research_product_from_html(
            html,
            "https://rejections.example/products/quiet-speaker",
        )
        self.assertEqual(product.specs, [])
        self.assertEqual(product.features, [])

    def test_ambiguous_product_h1_skips_primary_claim_extraction(self) -> None:
        product = product_research.research_product_from_html(
            fixture_text("primary_claims_ambiguous_h1.html"),
            "https://ambiguous.example/products/twin-speaker",
        )

        self.assertEqual(product.specs, [])
        self.assertEqual(product.features, [])

    def test_missing_product_h1_skips_primary_claim_extraction(self) -> None:
        html = fixture_text("primary_claims_alt_classes.html").replace(
            "<h1>Studio Mini by EchoWorks</h1>",
            "<p>Studio Mini by EchoWorks</p>",
        )

        product = product_research.research_product_from_html(
            html,
            "https://alternate.example/products/studio-mini",
        )

        self.assertEqual(product.specs, [])
        self.assertEqual(product.features, [])

    def test_instructional_seconds_do_not_become_playback_duration(self) -> None:
        soup = product_research.BeautifulSoup(
            "<main><h1>Pairing Speaker</h1></main>",
            "html.parser",
        )
        product_h1 = soup.find("h1")

        spec = product_research._extract_playback_claim(
            "Stereo Pairing: Hold the button for 2 seconds to connect.",
            "https://claims.example/products/pairing-speaker",
            product_h1,
        )

        self.assertIsNone(spec)

    def test_compound_immersion_is_a_primary_fact_signal(self) -> None:
        self.assertTrue(
            product_research._has_primary_fact_signal(
                "Immersion Protection: Submersible to 1 meter for 30 minutes."
            )
        )

    def test_ip_classifier_supports_ipx_ratings_but_rejects_invalid_ratings(
        self,
    ) -> None:
        soup = product_research.BeautifulSoup(
            "<main><h1>Protected Speaker</h1></main>",
            "html.parser",
        )
        product_h1 = soup.find("h1")
        source_url = "https://claims.example/products/protected-speaker"

        supported = product_research._extract_ip_claim(
            "Water Protection: The product has an IPX7 water rating.",
            source_url,
            product_h1,
        )
        unsupported = product_research._extract_ip_claim(
            "Water Protection: The product claims an IP99 water rating.",
            source_url,
            product_h1,
        )

        self.assertIsNotNone(supported)
        self.assertEqual(supported.value, "IPX7")
        self.assertIsNone(unsupported)

    def test_tws_compatibility_qualifier_is_preserved_in_description(self) -> None:
        soup = product_research.BeautifulSoup(
            "<main><h1>Pairing Speaker</h1></main>",
            "html.parser",
        )
        product_h1 = soup.find("h1")

        features = product_research._extract_primary_features(
            "Stereo Pairing: Compatible with True Wireless Stereo speakers.",
            "https://claims.example/products/pairing-speaker",
            product_h1,
        )

        self.assertEqual(len(features), 1)
        self.assertIsNotNone(features[0].description)
        self.assertIn("Compatible with", features[0].description)

    def test_higher_confidence_sources_keep_their_evidence_during_deduplication(
        self,
    ) -> None:
        product = product_research.research_product_from_html(
            fixture_text("primary_claims_dedup.html"),
            "https://priority.example/products/source-priority-speaker",
        )
        specs = {spec.key: spec for spec in product.specs}
        features = {feature.id: feature for feature in product.features}

        self.assertEqual(specs["water_resistance"].value, "IP68")
        self.assertEqual(specs["water_resistance"].confidence, 0.95)
        self.assertEqual(
            specs["water_resistance"].evidence.extraction_method,
            "jsonld_additional_property",
        )
        self.assertEqual(features["floating-design"].confidence, 0.90)
        self.assertEqual(
            features["floating-design"].evidence.extraction_method,
            "jsonld_feature_list",
        )


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
            self.assertEqual(spec.evidence.source_url, source_url)
            self.assertFalse(spec.verified)
        for feature in features.values():
            self.assertEqual(feature.source_url, source_url)
            self.assertEqual(feature.evidence.source_url, source_url)
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

    def test_final_redirected_url_is_preserved_in_claim_evidence(self) -> None:
        research = product_research
        session = Mock()
        response = Mock()
        response.url = "https://shop.example/products/trail-speaker?redirected=1"
        response.headers = {"Content-Type": "text/html"}
        response.text = fixture_text("jsonld_product.html")
        session.get.return_value = response

        product = research.research_product(
            "https://shop.example/go/trail-speaker",
            session=session,
        )

        self.assertTrue(product.specs)
        self.assertTrue(product.features)
        for claim in [*product.specs, *product.features]:
            self.assertEqual(claim.source_url, response.url)
            self.assertIsNotNone(claim.evidence)
            self.assertEqual(claim.evidence.source_url, response.url)

    def test_final_redirected_url_is_preserved_in_primary_claim_evidence(
        self,
    ) -> None:
        session = Mock()
        response = Mock()
        response.url = "https://claims.example/products/trail-speaker-pro?final=1"
        response.headers = {"Content-Type": "text/html"}
        response.text = fixture_text("primary_claims.html")
        session.get.return_value = response

        product = product_research.research_product(
            "https://claims.example/go/trail-speaker-pro",
            session=session,
        )

        primary_claims = [
            claim
            for claim in [*product.specs, *product.features]
            if claim.evidence
            and claim.evidence.extraction_method == "primary_product_claim_list"
        ]
        self.assertTrue(primary_claims)
        for claim in primary_claims:
            self.assertEqual(claim.source_url, response.url)
            self.assertEqual(claim.evidence.source_url, response.url)


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
