import unittest

from product_identity import (
    canonicalize_product_name,
    generate_source_scoped_product_id,
    normalize_identity_url,
    stable_variant_identity,
)


class CanonicalProductNameTests(unittest.TestCase):
    def test_trims_supported_seo_suffix_when_model_token_is_present(self) -> None:
        self.assertEqual(
            canonicalize_product_name(
                "soundcore Select 4 Go | Waterproof Bluetooth Speaker",
                "soundcore",
            ),
            "Select 4 Go",
        )

    def test_removes_repeated_exact_leading_brand_prefixes(self) -> None:
        self.assertEqual(
            canonicalize_product_name(
                "Acme Audio Acme Audio Trail 7",
                "Acme Audio",
            ),
            "Trail 7",
        )

    def test_preserves_brand_substring_inside_product_name(self) -> None:
        self.assertEqual(
            canonicalize_product_name("GoPro Hero 12", "Go"),
            "GoPro Hero 12",
        )

    def test_preserves_model_tokens_in_retained_name(self) -> None:
        self.assertEqual(
            canonicalize_product_name("Acme X-200 | Outdoor Speaker", "Acme"),
            "X-200",
        )

    def test_keeps_ambiguous_separator_name_unchanged(self) -> None:
        source_name = "Acme Meadow | Portable Speaker"

        self.assertEqual(
            canonicalize_product_name(source_name, "Acme"),
            source_name,
        )

    def test_does_not_split_ascii_hyphen(self) -> None:
        self.assertEqual(
            canonicalize_product_name("Acme Trail-7 Outdoor Speaker", "Acme"),
            "Trail-7 Outdoor Speaker",
        )

    def test_does_not_split_colon(self) -> None:
        self.assertEqual(
            canonicalize_product_name("Acme Trail 7: Outdoor Speaker", "Acme"),
            "Trail 7: Outdoor Speaker",
        )

    def test_supports_spaced_en_and_em_dashes(self) -> None:
        for separator in (" – ", " — "):
            with self.subTest(separator=separator):
                self.assertEqual(
                    canonicalize_product_name(
                        f"Acme Trail 7{separator}Outdoor Speaker",
                        "Acme",
                    ),
                    "Trail 7",
                )

    def test_strong_source_can_corroborate_shorter_name_without_digit(self) -> None:
        self.assertEqual(
            canonicalize_product_name(
                "Acme Meadow | Portable Speaker",
                "Acme",
                corroborating_names=("Meadow",),
            ),
            "Meadow",
        )

    def test_does_not_mutate_corroborating_input(self) -> None:
        names = ["Trail 7", "Acme Trail 7"]
        before = list(names)

        canonicalize_product_name(
            "Acme Trail 7 | Outdoor Speaker",
            "Acme",
            corroborating_names=names,
        )

        self.assertEqual(names, before)


class SourceScopedProductIdentityTests(unittest.TestCase):
    def test_same_canonical_url_produces_same_id_regardless_of_display_name(self) -> None:
        first = generate_source_scoped_product_id(
            "https://shop.example/products/trail",
            "product",
            canonical_url="https://shop.example/products/trail",
        )
        second = generate_source_scoped_product_id(
            "https://www.shop.example/products/trail",
            "product",
            canonical_url="http://shop.example/products/trail",
        )

        self.assertEqual(first, second)
        self.assertEqual(first, "shop-example-product-products-trail")

    def test_tracking_parameters_are_ignored(self) -> None:
        clean = generate_source_scoped_product_id(
            "https://shop.example/products/trail",
            "product",
        )
        tracked = generate_source_scoped_product_id(
            "https://www.shop.example/products/trail?utm_source=email&gclid=1&fbclid=2",
            "product",
        )

        self.assertEqual(clean, tracked)

    def test_meaningful_query_parameters_are_retained_and_sorted(self) -> None:
        first = normalize_identity_url(
            "https://shop.example/products/trail?variant=green&size=mini",
        )
        second = normalize_identity_url(
            "http://www.shop.example/products/trail?size=mini&variant=green&utm_medium=cpc",
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            "shop.example/products/trail?size=mini&variant=green",
        )

    def test_same_name_paths_and_domains_cannot_collide(self) -> None:
        path_a = generate_source_scoped_product_id(
            "https://shop.example/products/trail-a",
            "product",
        )
        path_b = generate_source_scoped_product_id(
            "https://shop.example/products/trail-b",
            "product",
        )
        domain_b = generate_source_scoped_product_id(
            "https://retailer.example/products/trail-a",
            "product",
        )

        self.assertEqual(len({path_a, path_b, domain_b}), 3)

    def test_cross_origin_canonical_url_is_not_used(self) -> None:
        normalized = normalize_identity_url(
            "https://shop.example/products/trail",
            canonical_url="https://unrelated.example/products/trail",
        )

        self.assertEqual(normalized, "shop.example/products/trail")

    def test_variant_scope_requires_a_stable_signal(self) -> None:
        group_id = generate_source_scoped_product_id(
            "https://shop.example/products/family",
            "group",
        )
        variant_id = generate_source_scoped_product_id(
            "https://shop.example/products/family",
            "variant",
            variant_signal="sku:FS-GREEN",
        )

        self.assertEqual(group_id, "shop-example-group-products-family")
        self.assertEqual(
            variant_id,
            "shop-example-variant-products-family-sku-fs-green",
        )

    def test_stable_variant_identity_prefers_sku_then_model_identifiers(self) -> None:
        self.assertEqual(
            stable_variant_identity(
                {"sku": "SKU-1", "mpn": "MPN-1", "model": "MODEL-1"},
                "https://shop.example/products/family",
            ),
            "sku:SKU-1",
        )
        self.assertEqual(
            stable_variant_identity(
                {"mpn": "MPN-1", "model": "MODEL-1"},
                "https://shop.example/products/family",
            ),
            "mpn:MPN-1",
        )
        self.assertEqual(
            stable_variant_identity(
                {"model": "MODEL-1"},
                "https://shop.example/products/family",
            ),
            "model:MODEL-1",
        )

    def test_color_and_size_alone_never_define_variant_identity(self) -> None:
        self.assertIsNone(
            stable_variant_identity(
                {"color": "Forest Green", "size": "Mini"},
                "https://shop.example/products/family",
            )
        )

    def test_unique_variant_url_or_structured_id_can_define_variant_identity(self) -> None:
        group_url = "https://shop.example/products/family"

        self.assertEqual(
            stable_variant_identity(
                {"url": f"{group_url}?variant=green"},
                group_url,
                group_url=group_url,
            ),
            "url:shop.example/products/family?variant=green",
        )
        self.assertEqual(
            stable_variant_identity(
                {"@id": "urn:example:variant:green"},
                group_url,
                group_url=group_url,
            ),
            "id:urn:example:variant:green",
        )

    def test_structured_id_fragments_remain_variant_distinguishing(self) -> None:
        group_url = "https://shop.example/products/family"

        green = stable_variant_identity(
            {"@id": f"{group_url}#green"},
            group_url,
            group_url=group_url,
        )
        blue = stable_variant_identity(
            {"@id": f"{group_url}#blue"},
            group_url,
            group_url=group_url,
        )

        self.assertEqual(green, "id:shop.example/products/family#green")
        self.assertEqual(blue, "id:shop.example/products/family#blue")
        self.assertNotEqual(green, blue)

    def test_malformed_variant_url_is_ignored_instead_of_breaking_research(self) -> None:
        self.assertIsNone(
            stable_variant_identity(
                {"url": "javascript:invalid", "color": "Green"},
                "https://shop.example/products/family",
            )
        )

    def test_group_url_is_not_mistaken_for_unique_variant_url(self) -> None:
        group_url = "https://shop.example/products/family"

        self.assertIsNone(
            stable_variant_identity(
                {"url": group_url, "color": "Green"},
                group_url,
                group_url=group_url,
            )
        )


if __name__ == "__main__":
    unittest.main()
