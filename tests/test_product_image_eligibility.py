import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from product_image_downloads import DownloadedProductImage, ImageIntegrityError
from product_image_eligibility import (
    ProductImageEligibilityRequirements,
    ProductImageEligibilityResult,
    evaluate_product_image_eligibility,
)


def png_bytes(size: tuple = (12, 8)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, (20, 100, 180)).save(output, format="PNG")
    return output.getvalue()


def downloaded_image(path: Path, data: bytes) -> DownloadedProductImage:
    return DownloadedProductImage(
        product_id="example-product",
        source_image_id="source-image",
        source_url="https://cdn.example/source.png",
        final_url="https://media.example/final.png",
        local_path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        width=12,
        height=8,
        format="PNG",
        mime_type="image/png",
    )


class ProductImageEligibilityTests(unittest.TestCase):
    def test_requirements_are_strict_and_have_no_implicit_thresholds(self) -> None:
        requirements = ProductImageEligibilityRequirements()

        self.assertIsNone(requirements.min_width)
        self.assertIsNone(requirements.min_height)
        self.assertIsNone(requirements.min_pixel_count)
        self.assertIsNone(requirements.allowed_formats)

        for invalid in (
            {"min_width": 0},
            {"min_height": True},
            {"min_pixel_count": -1},
            {"allowed_formats": []},
            {"allowed_formats": ["PNG", "PNG"]},
            {"max_beauty_score": 10},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    ProductImageEligibilityRequirements.model_validate(invalid)

    def test_no_requirements_accepts_an_integrity_verified_image(self) -> None:
        data = png_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "image.png"
            path.write_bytes(data)

            result = evaluate_product_image_eligibility(
                downloaded_image(path, data),
                ProductImageEligibilityRequirements(),
            )

        self.assertTrue(result.eligible)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.pixel_count, 96)
        self.assertEqual(result.aspect_ratio, 1.5)

    def test_explicit_requirements_report_all_violations_in_stable_order(self) -> None:
        data = png_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "image.png"
            path.write_bytes(data)

            result = evaluate_product_image_eligibility(
                downloaded_image(path, data),
                ProductImageEligibilityRequirements(
                    min_width=13,
                    min_height=9,
                    min_pixel_count=97,
                    allowed_formats=["JPEG", "WEBP"],
                ),
            )

        self.assertFalse(result.eligible)
        self.assertEqual(
            result.violations,
            [
                "width_below_minimum",
                "height_below_minimum",
                "pixel_count_below_minimum",
                "format_not_allowed",
            ],
        )

    def test_values_equal_to_explicit_minimums_are_eligible(self) -> None:
        data = png_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "image.png"
            path.write_bytes(data)

            result = evaluate_product_image_eligibility(
                downloaded_image(path, data),
                ProductImageEligibilityRequirements(
                    min_width=12,
                    min_height=8,
                    min_pixel_count=96,
                    allowed_formats=["PNG"],
                ),
            )

        self.assertTrue(result.eligible)

    def test_result_round_trips_as_strict_typed_json(self) -> None:
        result = ProductImageEligibilityResult(
            product_id="example-product",
            source_image_id="source-image",
            sha256="a" * 64,
            width=12,
            height=8,
            pixel_count=96,
            aspect_ratio=1.5,
            format="PNG",
            eligible=False,
            violations=["format_not_allowed"],
        )

        restored = ProductImageEligibilityResult.model_validate_json(
            result.model_dump_json()
        )

        self.assertEqual(restored, result)
        invalid = json.loads(result.model_dump_json())
        invalid["aesthetic_score"] = 0.9
        with self.assertRaises(ValidationError):
            ProductImageEligibilityResult.model_validate(invalid)
        invalid = json.loads(result.model_dump_json())
        invalid["eligible"] = True
        with self.assertRaises(ValidationError):
            ProductImageEligibilityResult.model_validate(invalid)
        invalid = json.loads(result.model_dump_json())
        invalid["violations"] = ["format_not_allowed", "format_not_allowed"]
        with self.assertRaises(ValidationError):
            ProductImageEligibilityResult.model_validate(invalid)

    def test_file_integrity_failure_is_not_recast_as_ineligibility(self) -> None:
        data = png_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "image.png"
            path.write_bytes(data)
            asset = downloaded_image(path, data)
            path.write_bytes(b"corrupt")

            with self.assertRaises(ImageIntegrityError):
                evaluate_product_image_eligibility(
                    asset,
                    ProductImageEligibilityRequirements(),
                )

    def test_evaluation_does_not_mutate_asset_or_requirements(self) -> None:
        data = png_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "image.png"
            path.write_bytes(data)
            asset = downloaded_image(path, data)
            requirements = ProductImageEligibilityRequirements(min_width=20)
            asset_before = json.loads(asset.model_dump_json())
            requirements_before = json.loads(requirements.model_dump_json())

            evaluate_product_image_eligibility(asset, requirements)

        self.assertEqual(json.loads(asset.model_dump_json()), asset_before)
        self.assertEqual(
            json.loads(requirements.model_dump_json()),
            requirements_before,
        )


if __name__ == "__main__":
    unittest.main()
