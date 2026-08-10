import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from product_asset_manifest import (
    ProductAssetManifest,
    load_product_asset_manifest,
    save_product_asset_manifest,
)
from product_image_downloads import DownloadedProductImage


def downloaded_image(
    source_image_id: str,
    *,
    product_id: str = "example-product",
    sha256: str = "a" * 64,
    local_path: str = "/assets/example-product/a.png",
) -> DownloadedProductImage:
    return DownloadedProductImage(
        product_id=product_id,
        source_image_id=source_image_id,
        source_url=f"https://cdn.example/{source_image_id}.png",
        final_url=f"https://media.example/{source_image_id}.png",
        local_path=local_path,
        sha256=sha256,
        byte_size=100,
        width=10,
        height=8,
        format="PNG",
        mime_type="image/png",
        downloaded_at=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )


class ProductAssetManifestTests(unittest.TestCase):
    def test_manifest_round_trips_with_strict_schema_version(self) -> None:
        manifest = ProductAssetManifest(
            product_id="example-product",
            images=[downloaded_image("source-one")],
        )

        restored = ProductAssetManifest.model_validate_json(
            manifest.model_dump_json()
        )

        self.assertEqual(restored, manifest)
        self.assertEqual(restored.schema_version, 1)
        invalid = manifest.model_dump(mode="json")
        invalid["campaign_id"] = "not-an-asset-field"
        with self.assertRaises(ValidationError):
            ProductAssetManifest.model_validate(invalid)
        invalid = manifest.model_dump(mode="json")
        invalid["schema_version"] = 2
        with self.assertRaises(ValidationError):
            ProductAssetManifest.model_validate(invalid)

    def test_manifest_requires_every_asset_to_match_product(self) -> None:
        with self.assertRaisesRegex(ValidationError, "different-product"):
            ProductAssetManifest(
                product_id="example-product",
                images=[
                    downloaded_image(
                        "source-one",
                        product_id="different-product",
                    )
                ],
            )

    def test_duplicate_source_image_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "source-one"):
            ProductAssetManifest(
                product_id="example-product",
                images=[
                    downloaded_image("source-one"),
                    downloaded_image("source-one"),
                ],
            )

    def test_exact_duplicate_content_can_keep_multiple_source_references(self) -> None:
        manifest = ProductAssetManifest(
            product_id="example-product",
            images=[
                downloaded_image("source-one"),
                downloaded_image("source-two"),
            ],
        )

        self.assertEqual(len(manifest.images), 2)
        self.assertEqual(manifest.images[0].sha256, manifest.images[1].sha256)
        self.assertEqual(
            manifest.images[0].local_path,
            manifest.images[1].local_path,
        )

    def test_same_hash_cannot_claim_different_canonical_paths(self) -> None:
        with self.assertRaisesRegex(ValidationError, "same SHA-256"):
            ProductAssetManifest(
                product_id="example-product",
                images=[
                    downloaded_image("source-one"),
                    downloaded_image(
                        "source-two",
                        local_path="/assets/example-product/other.png",
                    ),
                ],
            )

    def test_same_local_path_cannot_claim_different_hashes(self) -> None:
        with self.assertRaisesRegex(ValidationError, "same local path"):
            ProductAssetManifest(
                product_id="example-product",
                images=[
                    downloaded_image("source-one"),
                    downloaded_image("source-two", sha256="b" * 64),
                ],
            )

    def test_save_and_load_use_human_readable_utf8_json(self) -> None:
        manifest = ProductAssetManifest(
            product_id="café-speaker",
            images=[
                downloaded_image("source-one", product_id="café-speaker")
            ],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "assets.json"
            saved_path = save_product_asset_manifest(manifest, path)
            text = path.read_text(encoding="utf-8")
            restored = load_product_asset_manifest(path)

        self.assertEqual(saved_path, path)
        self.assertEqual(restored, manifest)
        self.assertIn("café-speaker", text)
        self.assertIn('\n  "schema_version": 1', text)
        self.assertTrue(text.endswith("\n"))

    def test_load_reports_malformed_json_as_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "assets.json"
            path.write_text('{"schema_version": 1,', encoding="utf-8")

            with self.assertRaises(ValidationError) as context:
                load_product_asset_manifest(path)

        self.assertEqual(context.exception.errors()[0]["type"], "json_invalid")

    def test_manifest_creation_does_not_mutate_download_records(self) -> None:
        asset = downloaded_image("source-one")
        before = json.loads(asset.model_dump_json())

        ProductAssetManifest(product_id="example-product", images=[asset])

        self.assertEqual(json.loads(asset.model_dump_json()), before)


if __name__ == "__main__":
    unittest.main()
