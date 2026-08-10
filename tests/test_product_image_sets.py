import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from product import Product, ProductImage
from product_asset_manifest import ProductAssetManifest
from product_image_downloads import DownloadedProductImage, ImageIntegrityError
from product_image_sets import (
    DuplicateProductImageIdError,
    ProductImageSetProductMismatchError,
    UnknownManifestImageError,
    build_verified_product_image_set,
)


def product_image(
    image_id: str,
    *,
    confidence: float = 0.80,
    width: int = 800,
    height: int = 600,
) -> ProductImage:
    return ProductImage(
        id=image_id,
        url=f"https://cdn.example/{image_id}.png",
        source_url="https://shop.example/products/speaker",
        source_type="json_ld_product_image",
        confidence=confidence,
        width=width,
        height=height,
    )


def product(*images: ProductImage, product_id: str = "example-product") -> Product:
    return Product(
        id=product_id,
        name="Example Speaker",
        brand="Example",
        category="Portable Speakers",
        images=list(images),
    )


def png_bytes(color: tuple) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (7, 5), color).save(output, format="PNG")
    return output.getvalue()


def downloaded_image(
    image_id: str,
    path: Path,
    data: bytes,
    *,
    product_id: str = "example-product",
) -> DownloadedProductImage:
    return DownloadedProductImage(
        product_id=product_id,
        source_image_id=image_id,
        source_url=f"https://cdn.example/{image_id}.png",
        final_url=f"https://media.example/{image_id}.png",
        local_path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        width=7,
        height=5,
        format="PNG",
        mime_type="image/png",
    )


class VerifiedProductImageSetTests(unittest.TestCase):
    def test_empty_product_and_manifest_produce_an_empty_set(self) -> None:
        manifest = ProductAssetManifest(product_id="example-product")

        selected = build_verified_product_image_set(product(), manifest)

        self.assertEqual(selected, [])

    def test_product_and_manifest_identity_must_match(self) -> None:
        manifest = ProductAssetManifest(product_id="different-product")

        with self.assertRaises(ProductImageSetProductMismatchError):
            build_verified_product_image_set(product(), manifest)

    def test_manifest_sources_must_exist_in_current_product_research(self) -> None:
        data = png_bytes((1, 2, 3))
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "unknown.png"
            path.write_bytes(data)
            manifest = ProductAssetManifest(
                product_id="example-product",
                images=[downloaded_image("unknown", path, data)],
            )

            with self.assertRaisesRegex(UnknownManifestImageError, "unknown"):
                build_verified_product_image_set(
                    product(product_image("known")),
                    manifest,
                )

    def test_partially_downloaded_product_selects_only_available_assets(self) -> None:
        data = png_bytes((1, 2, 3))
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "available.png"
            path.write_bytes(data)
            manifest = ProductAssetManifest(
                product_id="example-product",
                images=[downloaded_image("available", path, data)],
            )

            selected = build_verified_product_image_set(
                product(
                    product_image("missing", confidence=0.95),
                    product_image("available", confidence=0.80),
                ),
                manifest,
            )

        self.assertEqual(
            [image.source_image_id for image in selected],
            ["available"],
        )

    def test_available_assets_follow_existing_product_image_ranking(self) -> None:
        low_data = png_bytes((1, 2, 3))
        high_data = png_bytes((4, 5, 6))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            low_path = root / "low.png"
            high_path = root / "high.png"
            low_path.write_bytes(low_data)
            high_path.write_bytes(high_data)
            manifest = ProductAssetManifest(
                product_id="example-product",
                images=[
                    downloaded_image("low", low_path, low_data),
                    downloaded_image("high", high_path, high_data),
                ],
            )

            selected = build_verified_product_image_set(
                product(
                    product_image("low", confidence=0.70),
                    product_image("high", confidence=0.95),
                ),
                manifest,
            )

        self.assertEqual(
            [image.source_image_id for image in selected],
            ["high", "low"],
        )

    def test_exact_duplicate_bytes_keep_highest_ranked_source_reference(self) -> None:
        data = png_bytes((1, 2, 3))
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "shared.png"
            path.write_bytes(data)
            manifest = ProductAssetManifest(
                product_id="example-product",
                images=[
                    downloaded_image("lower", path, data),
                    downloaded_image("higher", path, data),
                ],
            )

            selected = build_verified_product_image_set(
                product(
                    product_image("lower", confidence=0.70),
                    product_image("higher", confidence=0.95),
                ),
                manifest,
            )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].source_image_id, "higher")

    def test_every_manifest_asset_is_reverified_before_deduplication(self) -> None:
        data = png_bytes((1, 2, 3))
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "shared.png"
            path.write_bytes(data)
            valid = downloaded_image("higher", path, data)
            invalid = downloaded_image("lower", path, data).model_copy(
                update={"width": 99}
            )
            manifest = ProductAssetManifest(
                product_id="example-product",
                images=[valid, invalid],
            )

            with self.assertRaisesRegex(ImageIntegrityError, "dimensions"):
                build_verified_product_image_set(
                    product(
                        product_image("higher", confidence=0.95),
                        product_image("lower", confidence=0.70),
                    ),
                    manifest,
                )

    def test_duplicate_product_image_ids_fail_instead_of_joining_ambiguously(self) -> None:
        duplicate = product_image("same")
        manifest = ProductAssetManifest(product_id="example-product")

        with self.assertRaisesRegex(DuplicateProductImageIdError, "same"):
            build_verified_product_image_set(
                product(duplicate, duplicate.model_copy(deep=True)),
                manifest,
            )

    def test_selection_returns_isolated_records_without_mutating_inputs(self) -> None:
        data = png_bytes((1, 2, 3))
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "available.png"
            path.write_bytes(data)
            source_product = product(product_image("available"))
            manifest = ProductAssetManifest(
                product_id="example-product",
                images=[downloaded_image("available", path, data)],
            )
            product_before = json.loads(source_product.model_dump_json())
            manifest_before = json.loads(manifest.model_dump_json())

            selected = build_verified_product_image_set(
                source_product,
                manifest,
            )
            selected[0].source_image_id = "changed"

        self.assertEqual(json.loads(source_product.model_dump_json()), product_before)
        self.assertEqual(json.loads(manifest.model_dump_json()), manifest_before)


if __name__ == "__main__":
    unittest.main()
