import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import (
    DerivedAssetSourceLinkError,
    ProductDerivedAssetManifest,
    build_product_derived_asset_manifest,
    load_product_derived_asset_manifest,
    save_product_derived_asset_manifest,
    validate_product_derived_asset_sources,
)
from product_image_downloads import DownloadedProductImage
from product_reference_sheets import (
    ProductReferenceSheet,
    ProductReferenceSheetTile,
)


def downloaded_image(
    source_image_id: str = "source-one",
    *,
    product_id: str = "example-product",
    sha256: str = "a" * 64,
    local_path: str = "/assets/example-product/source.png",
) -> DownloadedProductImage:
    return DownloadedProductImage(
        product_id=product_id,
        source_image_id=source_image_id,
        source_url=f"https://cdn.example/{source_image_id}.png",
        final_url=f"https://media.example/{source_image_id}.png",
        local_path=local_path,
        sha256=sha256,
        byte_size=100,
        width=600,
        height=600,
        format="PNG",
        mime_type="image/png",
        downloaded_at=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )


def reference_sheet(
    source: DownloadedProductImage,
    *,
    product_id: str = None,
    sheet_sha256: str = "b" * 64,
    sheet_path: str = "/assets/example-product/reference-sheet.png",
) -> ProductReferenceSheet:
    return ProductReferenceSheet(
        product_id=product_id or source.product_id,
        local_path=sheet_path,
        width=2048,
        height=2048,
        sha256=sheet_sha256,
        created_at=datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc),
        tiles=[
            ProductReferenceSheetTile(
                index=0,
                source_image_id=source.source_image_id,
                source_sha256=source.sha256,
                source_local_path=source.local_path,
                x=64,
                y=64,
                width=1920,
                height=1920,
                rendered_x=724,
                rendered_y=724,
                rendered_width=600,
                rendered_height=600,
            )
        ],
    )


class ProductDerivedAssetManifestTests(unittest.TestCase):
    def test_empty_manifest_round_trips_with_strict_schema_version(self) -> None:
        manifest = ProductDerivedAssetManifest(product_id="example-product")

        restored = ProductDerivedAssetManifest.model_validate_json(
            manifest.model_dump_json()
        )

        self.assertEqual(restored, manifest)
        self.assertEqual(restored.schema_version, 1)
        for invalid_update in (
            {"schema_version": 2},
            {"campaign_id": "not-a-derived-asset-field"},
        ):
            invalid = manifest.model_dump(mode="json")
            invalid.update(invalid_update)
            with self.subTest(invalid_update=invalid_update):
                with self.assertRaises(ValidationError):
                    ProductDerivedAssetManifest.model_validate(invalid)

    def test_every_reference_sheet_must_match_manifest_product(self) -> None:
        source = downloaded_image()

        with self.assertRaisesRegex(ValidationError, "different-product"):
            ProductDerivedAssetManifest(
                product_id="example-product",
                reference_sheets=[
                    reference_sheet(source, product_id="different-product")
                ],
            )

    def test_duplicate_sheet_hashes_or_paths_are_rejected(self) -> None:
        source = downloaded_image()
        first = reference_sheet(source)
        duplicate_hash = reference_sheet(
            source,
            sheet_path="/assets/example-product/other.png",
        )
        duplicate_path = reference_sheet(
            source,
            sheet_sha256="c" * 64,
        )

        for sheets, message in (
            ([first, duplicate_hash], "SHA-256"),
            ([first, duplicate_path], "local path"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    ProductDerivedAssetManifest(
                        product_id="example-product",
                        reference_sheets=sheets,
                    )

    def test_shared_derived_bytes_can_retain_distinct_tile_provenance(self) -> None:
        first_source = downloaded_image()
        second_source = downloaded_image(
            "source-two",
            sha256="c" * 64,
            local_path="/assets/example-product/source-two.png",
        )
        originals = ProductAssetManifest(
            product_id="example-product",
            images=[first_source, second_source],
        )
        shared_path = "/assets/example-product/shared-sheet.png"
        first_sheet = reference_sheet(first_source, sheet_path=shared_path)
        second_sheet = reference_sheet(second_source, sheet_path=shared_path)

        derived = build_product_derived_asset_manifest(
            originals,
            [first_sheet, second_sheet],
        )

        self.assertEqual(len(derived.reference_sheets), 2)
        self.assertEqual(
            {sheet.sha256 for sheet in derived.reference_sheets},
            {"b" * 64},
        )
        self.assertEqual(
            [sheet.tiles[0].source_image_id for sheet in derived.reference_sheets],
            ["source-one", "source-two"],
        )

    def test_duplicate_sheet_provenance_is_rejected(self) -> None:
        source = downloaded_image()
        sheet = reference_sheet(source)

        with self.assertRaisesRegex(ValidationError, "provenance"):
            ProductDerivedAssetManifest(
                product_id="example-product",
                reference_sheets=[sheet, sheet.model_copy(deep=True)],
            )

    def test_builder_links_every_tile_to_original_manifest_identity(self) -> None:
        source = downloaded_image()
        originals = ProductAssetManifest(
            product_id="example-product",
            images=[source],
        )
        sheet = reference_sheet(source)

        derived = build_product_derived_asset_manifest(originals, [sheet])

        self.assertEqual(derived.product_id, originals.product_id)
        self.assertEqual(derived.reference_sheets, [sheet])
        self.assertIsNot(derived.reference_sheets[0], sheet)

    def test_unknown_source_image_id_fails_link_validation(self) -> None:
        source = downloaded_image()
        originals = ProductAssetManifest(
            product_id="example-product",
            images=[source],
        )
        tile = reference_sheet(source).tiles[0].model_copy(
            update={"source_image_id": "unknown-source"}
        )
        sheet = reference_sheet(source).model_copy(update={"tiles": [tile]})

        with self.assertRaisesRegex(DerivedAssetSourceLinkError, "unknown-source"):
            build_product_derived_asset_manifest(originals, [sheet])

    def test_mismatched_source_hash_or_path_fails_link_validation(self) -> None:
        source = downloaded_image()
        originals = ProductAssetManifest(
            product_id="example-product",
            images=[source],
        )

        for update, message in (
            ({"source_sha256": "d" * 64}, "SHA-256"),
            ({"source_local_path": "/assets/wrong.png"}, "local path"),
        ):
            with self.subTest(update=update):
                tile = reference_sheet(source).tiles[0].model_copy(update=update)
                sheet = reference_sheet(source).model_copy(
                    update={"tiles": [tile]}
                )
                with self.assertRaisesRegex(
                    DerivedAssetSourceLinkError,
                    message,
                ):
                    build_product_derived_asset_manifest(originals, [sheet])

    def test_existing_manifest_can_be_revalidated_against_originals(self) -> None:
        source = downloaded_image()
        originals = ProductAssetManifest(
            product_id="example-product",
            images=[source],
        )
        derived = ProductDerivedAssetManifest(
            product_id="example-product",
            reference_sheets=[reference_sheet(source)],
        )

        validated = validate_product_derived_asset_sources(derived, originals)

        self.assertEqual(validated, derived)
        self.assertIsNot(validated, derived)

    def test_save_and_load_use_human_readable_utf8_json(self) -> None:
        source = downloaded_image(product_id="café-speaker")
        manifest = ProductDerivedAssetManifest(
            product_id="café-speaker",
            reference_sheets=[reference_sheet(source)],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "derived-assets.json"
            saved_path = save_product_derived_asset_manifest(manifest, path)
            text = path.read_text(encoding="utf-8")
            restored = load_product_derived_asset_manifest(path)

        self.assertEqual(saved_path, path)
        self.assertEqual(restored, manifest)
        self.assertIn("café-speaker", text)
        self.assertIn('\n  "schema_version": 1', text)
        self.assertTrue(text.endswith("\n"))

    def test_load_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "derived-assets.json"
            path.write_text('{"schema_version": 1,', encoding="utf-8")

            with self.assertRaises(ValidationError) as context:
                load_product_derived_asset_manifest(path)

        self.assertEqual(context.exception.errors()[0]["type"], "json_invalid")

    def test_build_and_validation_do_not_mutate_inputs(self) -> None:
        source = downloaded_image()
        originals = ProductAssetManifest(
            product_id="example-product",
            images=[source],
        )
        sheet = reference_sheet(source)
        originals_before = json.loads(originals.model_dump_json())
        sheet_before = json.loads(sheet.model_dump_json())

        derived = build_product_derived_asset_manifest(originals, [sheet])
        validate_product_derived_asset_sources(derived, originals)

        self.assertEqual(json.loads(originals.model_dump_json()), originals_before)
        self.assertEqual(json.loads(sheet.model_dump_json()), sheet_before)


if __name__ == "__main__":
    unittest.main()
