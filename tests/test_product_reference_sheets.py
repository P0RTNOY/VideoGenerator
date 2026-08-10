import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from ad_claims import generate_ad_claim_candidates
from product import Product, ProductFeature, ProductImage, ProductSpec
from product_asset_manifest import ProductAssetManifest
from product_image_downloads import DownloadedProductImage, ImageIntegrityError
from product_image_eligibility import ProductImageEligibilityRequirements
from product_image_sets import build_verified_product_image_set
from product_images import rank_product_images
from product_reference_sheets import (
    MAX_REFERENCE_SHEET_BYTES,
    NoEligibleReferenceImagesError,
    ProductReferenceSheet,
    ProductReferenceSheetPolicy,
    ReferenceSheetFilesystemError,
    ReferenceSheetIntegrityError,
    build_product_reference_sheet,
    verify_product_reference_sheet,
)


PRODUCT_ID = "example-product"


def encoded_image(
    size: tuple = (600, 600),
    color: tuple = (20, 100, 180),
    *,
    mode: str = "RGB",
    image_format: str = "PNG",
) -> bytes:
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format=image_format)
    return output.getvalue()


def product_image(
    image_id: str,
    size: tuple,
    *,
    confidence: float = 0.80,
) -> ProductImage:
    return ProductImage(
        id=image_id,
        url=f"https://cdn.example/{image_id}.png",
        source_url="https://shop.example/products/speaker",
        source_type="json_ld_product_image",
        width=size[0],
        height=size[1],
        confidence=confidence,
    )


def downloaded_image(
    image_id: str,
    path: Path,
    data: bytes,
    size: tuple,
    *,
    image_format: str = "PNG",
) -> DownloadedProductImage:
    mime_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }[image_format]
    return DownloadedProductImage(
        product_id=PRODUCT_ID,
        source_image_id=image_id,
        source_url=f"https://cdn.example/{image_id}.png",
        final_url=f"https://media.example/{image_id}.png",
        local_path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        width=size[0],
        height=size[1],
        format=image_format,
        mime_type=mime_type,
    )


def product_for(images: list) -> Product:
    return Product(
        id=PRODUCT_ID,
        name="Example Speaker",
        brand="Example",
        category="Portable Speakers",
        images=images,
        specs=[
            ProductSpec(key="playback", label="Playback", value=10, unit="h")
        ],
        features=[
            ProductFeature(id="portable", name="Portable")
        ],
    )


def image_bundle(
    root: Path,
    count: int,
    *,
    sizes: list = None,
    confidences: list = None,
) -> tuple:
    originals = root / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    product_images = []
    assets = []
    for index in range(count):
        image_id = f"image-{index}"
        size = sizes[index] if sizes is not None else (600, 600)
        confidence = (
            confidences[index] if confidences is not None else 0.80
        )
        color = (
            (index * 37 + 11) % 256,
            (index * 67 + 23) % 256,
            (index * 97 + 41) % 256,
        )
        data = encoded_image(size, color)
        path = originals / f"{image_id}.png"
        path.write_bytes(data)
        product_images.append(
            product_image(
                image_id,
                size,
                confidence=confidence,
            )
        )
        assets.append(downloaded_image(image_id, path, data, size))
    return (
        product_for(product_images),
        ProductAssetManifest(product_id=PRODUCT_ID, images=assets),
        assets,
    )


def tile_bounds(sheet: ProductReferenceSheet) -> list:
    return [
        (tile.x, tile.y, tile.width, tile.height)
        for tile in sheet.tiles
    ]


class ProductReferenceSheetLayoutTests(unittest.TestCase):
    def test_one_to_six_images_use_the_approved_grid_coordinates(self) -> None:
        expected = {
            1: [(64, 64, 1920, 1920)],
            2: [(64, 64, 944, 1920), (1040, 64, 944, 1920)],
            3: [
                (64, 64, 944, 944),
                (1040, 64, 944, 944),
                (64, 1040, 944, 944),
            ],
            4: [
                (64, 64, 944, 944),
                (1040, 64, 944, 944),
                (64, 1040, 944, 944),
                (1040, 1040, 944, 944),
            ],
            5: [
                (64, 64, 619, 944),
                (715, 64, 619, 944),
                (1366, 64, 618, 944),
                (64, 1040, 619, 944),
                (715, 1040, 619, 944),
            ],
            6: [
                (64, 64, 619, 944),
                (715, 64, 619, 944),
                (1366, 64, 618, 944),
                (64, 1040, 619, 944),
                (715, 1040, 619, 944),
                (1366, 1040, 618, 944),
            ],
        }

        for count in range(1, 7):
            with self.subTest(count=count):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    product, manifest, _ = image_bundle(root, count)

                    sheet = build_product_reference_sheet(
                        product,
                        manifest,
                        storage_root=root / "products",
                    )

                self.assertEqual(tile_bounds(sheet), expected[count])
                self.assertEqual(
                    [tile.index for tile in sheet.tiles],
                    list(range(count)),
                )

    def test_more_than_six_uses_first_six_eligible_ranked_images(self) -> None:
        sizes = [(600, 600)] * 7 + [(400, 400)]
        confidences = [0.60, 0.95, 0.70, 0.90, 0.80, 0.85, 0.65, 1.0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(
                root,
                8,
                sizes=sizes,
                confidences=confidences,
            )

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )

        self.assertEqual(
            [tile.source_image_id for tile in sheet.tiles],
            ["image-1", "image-3", "image-5", "image-4", "image-2", "image-6"],
        )

    def test_equal_ranking_preserves_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 4)

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )

        self.assertEqual(
            [tile.source_image_id for tile in sheet.tiles],
            ["image-0", "image-1", "image-2", "image-3"],
        )

    def test_policy_rejects_oversized_or_unusable_canvas_geometry(self) -> None:
        for invalid in (
            {"canvas_width": 2049},
            {"canvas_height": 0},
            {"outer_margin": -1},
            {"tile_gap": True},
            {"canvas_width": 100, "outer_margin": 40, "tile_gap": 20},
            {"layout_style": "masonry"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    ProductReferenceSheetPolicy.model_validate(invalid)


class ProductReferenceSheetRenderingTests(unittest.TestCase):
    def test_contain_fit_downscales_without_crop_or_stretch(self) -> None:
        size = (2400, 1200)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1, sizes=[size])

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )
            tile = sheet.tiles[0]

        self.assertEqual((tile.rendered_width, tile.rendered_height), (1920, 960))
        self.assertEqual(tile.rendered_width * size[1], tile.rendered_height * size[0])
        self.assertEqual((tile.rendered_x, tile.rendered_y), (64, 544))

    def test_source_smaller_than_tile_is_centered_without_upscaling(self) -> None:
        size = (600, 900)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1, sizes=[size])

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )
            tile = sheet.tiles[0]

        self.assertEqual((tile.rendered_width, tile.rendered_height), size)
        self.assertEqual((tile.rendered_x, tile.rendered_y), (724, 574))

    def test_transparency_is_flattened_to_white_without_changing_original(self) -> None:
        size = (600, 600)
        rgba = Image.new("RGBA", size, (0, 0, 0, 0))
        for x in range(200, 400):
            for y in range(200, 400):
                rgba.putpixel((x, y), (255, 0, 0, 255))
        output = io.BytesIO()
        rgba.save(output, format="PNG")
        data = output.getvalue()

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original = root / "original.png"
            original.write_bytes(data)
            discovered = product_image("transparent", size)
            asset = downloaded_image("transparent", original, data, size)
            source_product = product_for([discovered])
            manifest = ProductAssetManifest(
                product_id=PRODUCT_ID,
                images=[asset],
            )
            before = original.read_bytes()

            sheet = build_product_reference_sheet(
                source_product,
                manifest,
                storage_root=root / "products",
            )
            with Image.open(sheet.local_path) as rendered:
                self.assertEqual(rendered.mode, "RGB")
                self.assertEqual(rendered.getpixel((725, 725)), (255, 255, 255))
                self.assertEqual(rendered.getpixel((1024, 1024)), (255, 0, 0))

            self.assertEqual(original.read_bytes(), before)

    def test_sheet_is_unannotated_rgb_png_on_solid_white_background(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1)

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )
            with Image.open(sheet.local_path) as rendered:
                colors = {color for _, color in rendered.getcolors(2048 * 2048)}
                image_info = dict(rendered.info)
                rendered_format = rendered.format
                rendered_size = rendered.size
                rendered_mode = rendered.mode

        self.assertEqual(rendered_format, "PNG")
        self.assertEqual(rendered_size, (2048, 2048))
        self.assertEqual(rendered_mode, "RGB")
        self.assertEqual(
            colors,
            {(255, 255, 255), (11, 23, 41)},
        )
        self.assertNotIn("comment", image_info)
        self.assertNotIn("description", image_info)

    def test_jpeg_png_and_webp_sources_all_render_to_canonical_png(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            originals = root / "originals"
            originals.mkdir()
            product_images = []
            assets = []
            for index, image_format in enumerate(("JPEG", "PNG", "WEBP")):
                image_id = image_format.casefold()
                size = (600, 600)
                data = encoded_image(
                    size,
                    (20 + index, 100, 180),
                    image_format=image_format,
                )
                path = originals / f"{image_id}.{image_id}"
                path.write_bytes(data)
                product_images.append(product_image(image_id, size))
                assets.append(
                    downloaded_image(
                        image_id,
                        path,
                        data,
                        size,
                        image_format=image_format,
                    )
                )
            product = product_for(product_images)
            manifest = ProductAssetManifest(
                product_id=PRODUCT_ID,
                images=assets,
            )

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )
            with Image.open(sheet.local_path) as rendered:
                output_format = rendered.format

        self.assertEqual(output_format, "PNG")
        self.assertEqual(
            [tile.source_image_id for tile in sheet.tiles],
            ["jpeg", "png", "webp"],
        )


class ProductReferenceSheetPolicyTests(unittest.TestCase):
    def test_default_eligibility_requires_512_by_512(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1, sizes=[(511, 700)])

            with self.assertRaises(NoEligibleReferenceImagesError):
                build_product_reference_sheet(
                    product,
                    manifest,
                    storage_root=root / "products",
                )

    def test_caller_can_override_reference_sheet_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1, sizes=[(400, 400)])
            policy = ProductReferenceSheetPolicy(
                eligibility=ProductImageEligibilityRequirements(
                    min_width=300,
                    min_height=300,
                    allowed_formats=["PNG"],
                )
            )

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
                policy=policy,
            )

        self.assertEqual(len(sheet.tiles), 1)
        self.assertEqual(
            (sheet.tiles[0].rendered_width, sheet.tiles[0].rendered_height),
            (400, 400),
        )

    def test_empty_or_fully_ineligible_input_never_creates_blank_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            empty = product_for([])
            empty_manifest = ProductAssetManifest(product_id=PRODUCT_ID)
            small_product, small_manifest, _ = image_bundle(
                root,
                1,
                sizes=[(400, 400)],
            )

            for product, manifest in (
                (empty, empty_manifest),
                (small_product, small_manifest),
            ):
                with self.subTest(images=len(product.images)):
                    with self.assertRaises(NoEligibleReferenceImagesError):
                        build_product_reference_sheet(
                            product,
                            manifest,
                            storage_root=root / "products",
                        )


class ProductReferenceSheetProvenanceTests(unittest.TestCase):
    def test_tile_and_sheet_metadata_preserve_reproducible_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, assets = image_bundle(root, 2)

            sheet = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )
            restored = ProductReferenceSheet.model_validate_json(
                sheet.model_dump_json()
            )
            stored_sha256 = hashlib.sha256(
                Path(sheet.local_path).read_bytes()
            ).hexdigest()

        self.assertEqual(restored, sheet)
        self.assertEqual(sheet.product_id, product.id)
        self.assertEqual(sheet.format, "PNG")
        self.assertEqual((sheet.width, sheet.height), (2048, 2048))
        self.assertEqual(
            [tile.source_sha256 for tile in sheet.tiles],
            [asset.sha256 for asset in assets],
        )
        self.assertEqual(
            [tile.source_local_path for tile in sheet.tiles],
            [asset.local_path for asset in assets],
        )
        self.assertEqual(
            Path(sheet.local_path).parts[-4:-1],
            ("assets", "derived", "reference-sheets"),
        )
        self.assertEqual(Path(sheet.local_path).stem, sheet.sha256)
        self.assertEqual(stored_sha256, sheet.sha256)

        invalid = json.loads(sheet.model_dump_json())
        invalid["licensed"] = True
        with self.assertRaises(ValidationError):
            ProductReferenceSheet.model_validate(invalid)
        invalid = json.loads(sheet.model_dump_json())
        invalid["tiles"][0]["caption"] = "Front view"
        with self.assertRaises(ValidationError):
            ProductReferenceSheet.model_validate(invalid)
        invalid = json.loads(sheet.model_dump_json())
        invalid["tiles"][0]["rendered_x"] = sheet.width
        with self.assertRaises(ValidationError):
            ProductReferenceSheet.model_validate(invalid)

    def test_timestamp_does_not_change_rendered_png_or_sha256(self) -> None:
        first_time = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        second_time = first_time + timedelta(hours=1)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1)

            with patch(
                "product_reference_sheets._utc_now",
                side_effect=[first_time, second_time],
            ):
                first = build_product_reference_sheet(
                    product,
                    manifest,
                    storage_root=root / "products",
                )
                first_bytes = Path(first.local_path).read_bytes()
                second = build_product_reference_sheet(
                    product,
                    manifest,
                    storage_root=root / "products",
                )
                second_bytes = Path(second.local_path).read_bytes()

        self.assertNotEqual(first.created_at, second.created_at)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.local_path, second.local_path)
        self.assertEqual(first_bytes, second_bytes)

    def test_builder_does_not_mutate_product_manifest_or_originals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, assets = image_bundle(root, 2)
            product_before = json.loads(product.model_dump_json())
            manifest_before = json.loads(manifest.model_dump_json())
            originals_before = {
                asset.local_path: Path(asset.local_path).read_bytes()
                for asset in assets
            }
            candidate_ids_before = [
                candidate.id for candidate in generate_ad_claim_candidates(product)
            ]
            ranked_ids_before = [image.id for image in rank_product_images(product.images)]
            verified_ids_before = [
                image.source_image_id
                for image in build_verified_product_image_set(product, manifest)
            ]

            build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )

            self.assertEqual(json.loads(product.model_dump_json()), product_before)
            self.assertEqual(json.loads(manifest.model_dump_json()), manifest_before)
            self.assertEqual(
                {
                    path: Path(path).read_bytes()
                    for path in originals_before
                },
                originals_before,
            )
            self.assertEqual(
                [candidate.id for candidate in generate_ad_claim_candidates(product)],
                candidate_ids_before,
            )
            self.assertEqual(
                [image.id for image in rank_product_images(product.images)],
                ranked_ids_before,
            )
            self.assertEqual(
                [
                    image.source_image_id
                    for image in build_verified_product_image_set(product, manifest)
                ],
                verified_ids_before,
            )


class ProductReferenceSheetFailureTests(unittest.TestCase):
    def test_missing_integrity_invalid_and_corrupt_sources_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)

            for failure_kind in ("missing", "hash-mismatch", "corrupt"):
                with self.subTest(failure_kind=failure_kind):
                    case_root = root / failure_kind
                    product, manifest, assets = image_bundle(case_root, 1)
                    source = Path(assets[0].local_path)
                    if failure_kind == "missing":
                        source.unlink()
                    elif failure_kind == "hash-mismatch":
                        source.write_bytes(b"x" * assets[0].byte_size)
                    else:
                        corrupt = b"not-a-decodable-image"
                        source.write_bytes(corrupt)
                        invalid_asset = assets[0].model_copy(
                            update={
                                "sha256": hashlib.sha256(corrupt).hexdigest(),
                                "byte_size": len(corrupt),
                            }
                        )
                        manifest = ProductAssetManifest(
                            product_id=PRODUCT_ID,
                            images=[invalid_asset],
                        )

                    with self.assertRaises(ImageIntegrityError):
                        build_product_reference_sheet(
                            product,
                            manifest,
                            storage_root=root / "products",
                        )

    def test_corrupt_existing_output_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1)
            first = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )
            output_path = Path(first.local_path)
            output_path.write_bytes(b"corrupt existing output")

            with self.assertRaisesRegex(
                ReferenceSheetFilesystemError,
                "SHA-256",
            ):
                build_product_reference_sheet(
                    product,
                    manifest,
                    storage_root=root / "products",
                )

    def test_output_directory_failure_is_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1)
            storage_file = root / "not-a-directory"
            storage_file.write_text("occupied", encoding="utf-8")

            with self.assertRaises(ReferenceSheetFilesystemError):
                build_product_reference_sheet(
                    product,
                    manifest,
                    storage_root=storage_file,
                )

    def test_existing_output_symbolic_link_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            product, manifest, _ = image_bundle(root, 1)
            first = build_product_reference_sheet(
                product,
                manifest,
                storage_root=root / "products",
            )
            output_path = Path(first.local_path)
            external = root / "external.png"
            external.write_bytes(output_path.read_bytes())
            output_path.unlink()
            output_path.symlink_to(external)

            with self.assertRaisesRegex(
                ReferenceSheetFilesystemError,
                "symbolic link",
            ):
                build_product_reference_sheet(
                    product,
                    manifest,
                    storage_root=root / "products",
                )


class ProductReferenceSheetIntegrityTests(unittest.TestCase):
    def build_sheet(self, root: Path) -> ProductReferenceSheet:
        product, manifest, _ = image_bundle(root, 1)
        return build_product_reference_sheet(
            product,
            manifest,
            storage_root=root / "products",
        )

    def test_valid_sheet_reverifies_and_returns_an_isolated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sheet = self.build_sheet(Path(temporary_directory))
            expected_byte_size = len(Path(sheet.local_path).read_bytes())

            verified = verify_product_reference_sheet(sheet)

        self.assertEqual(verified, sheet)
        self.assertIsNot(verified, sheet)
        self.assertEqual(sheet.byte_size, expected_byte_size)

    def test_missing_or_non_file_path_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sheet = self.build_sheet(root)
            missing = sheet.model_copy(
                update={"local_path": str(root / "missing.png")}
            )
            directory = root / "directory.png"
            directory.mkdir()
            non_file = sheet.model_copy(update={"local_path": str(directory)})

            for candidate in (missing, non_file):
                with self.subTest(path=candidate.local_path):
                    with self.assertRaisesRegex(
                        ReferenceSheetIntegrityError,
                        "regular file",
                    ):
                        verify_product_reference_sheet(candidate)

    def test_symbolic_link_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sheet = self.build_sheet(root)
            link = root / "linked-sheet.png"
            link.symlink_to(sheet.local_path)
            linked = sheet.model_copy(update={"local_path": str(link)})

            with self.assertRaisesRegex(
                ReferenceSheetIntegrityError,
                "symbolic link",
            ):
                verify_product_reference_sheet(linked)

    def test_recorded_byte_size_mismatch_is_detected_before_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sheet = self.build_sheet(Path(temporary_directory))
            path = Path(sheet.local_path)
            path.write_bytes(path.read_bytes() + b"changed")

            with self.assertRaisesRegex(
                ReferenceSheetIntegrityError,
                "byte size",
            ):
                verify_product_reference_sheet(sheet)

    def test_sha256_mismatch_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sheet = self.build_sheet(Path(temporary_directory))
            path = Path(sheet.local_path)
            data = path.read_bytes()
            path.write_bytes(bytes([data[0] ^ 1]) + data[1:])

            with self.assertRaisesRegex(
                ReferenceSheetIntegrityError,
                "SHA-256",
            ):
                verify_product_reference_sheet(sheet)

    def test_corrupt_bytes_fail_even_when_recorded_hash_and_size_match(self) -> None:
        corrupt = b"not a reference-sheet image"
        with tempfile.TemporaryDirectory() as temporary_directory:
            sheet = self.build_sheet(Path(temporary_directory))
            path = Path(sheet.local_path)
            path.write_bytes(corrupt)
            changed = sheet.model_copy(
                update={
                    "sha256": hashlib.sha256(corrupt).hexdigest(),
                    "byte_size": len(corrupt),
                }
            )

            with self.assertRaisesRegex(
                ReferenceSheetIntegrityError,
                "decode",
            ):
                verify_product_reference_sheet(changed)

    def test_decoded_dimensions_format_and_mode_must_remain_canonical(self) -> None:
        cases = (
            ("dimensions", "PNG", "RGB", (1024, 1024)),
            ("format", "JPEG", "RGB", (2048, 2048)),
            ("RGB", "PNG", "RGBA", (2048, 2048)),
        )
        for message, image_format, mode, size in cases:
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    sheet = self.build_sheet(Path(temporary_directory))
                    output = io.BytesIO()
                    color = (
                        (20, 100, 180, 128)
                        if mode == "RGBA"
                        else (20, 100, 180)
                    )
                    Image.new(mode, size, color).save(output, format=image_format)
                    data = output.getvalue()
                    Path(sheet.local_path).write_bytes(data)
                    changed = sheet.model_copy(
                        update={
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "byte_size": len(data),
                        }
                    )

                    with self.assertRaisesRegex(
                        ReferenceSheetIntegrityError,
                        message,
                    ):
                        verify_product_reference_sheet(changed)

    def test_local_sheet_cannot_exceed_absolute_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sheet = self.build_sheet(root)
            oversized = root / "oversized.png"
            with oversized.open("wb") as output:
                output.seek(MAX_REFERENCE_SHEET_BYTES)
                output.write(b"x")
            changed = sheet.model_copy(update={"local_path": str(oversized)})

            with self.assertRaisesRegex(
                ReferenceSheetIntegrityError,
                "20 MB",
            ):
                verify_product_reference_sheet(changed)

    def test_legacy_metadata_without_byte_size_still_reverifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sheet = self.build_sheet(Path(temporary_directory))
            payload = sheet.model_dump(mode="json")
            payload.pop("byte_size")
            legacy = ProductReferenceSheet.model_validate(payload)

            verified = verify_product_reference_sheet(legacy)

        self.assertIsNone(legacy.byte_size)
        self.assertEqual(verified, legacy)

    def test_reverification_does_not_mutate_sheet_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sheet = self.build_sheet(Path(temporary_directory))
            before = json.loads(sheet.model_dump_json())

            verify_product_reference_sheet(sheet)

        self.assertEqual(json.loads(sheet.model_dump_json()), before)

if __name__ == "__main__":
    unittest.main()
