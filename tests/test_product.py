import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from product import (
    ClaimEvidence,
    Product,
    ProductFeature,
    ProductImage,
    ProductSource,
    ProductSpec,
    ProductVariant,
    load_product,
    save_product,
)


class ProductModelTests(unittest.TestCase):
    def test_claim_evidence_records_utc_extraction_context(self) -> None:
        evidence = ClaimEvidence(
            raw_text="Battery Life: Up to 20 hours",
            source_url="https://example.com/product",
            extraction_method="html_spec_table",
            section_heading="Specifications",
            locator="table row 2",
        )

        self.assertEqual(evidence.raw_text, "Battery Life: Up to 20 hours")
        self.assertEqual(evidence.source_url, "https://example.com/product")
        self.assertEqual(evidence.extraction_method, "html_spec_table")
        self.assertEqual(evidence.section_heading, "Specifications")
        self.assertEqual(evidence.locator, "table row 2")
        self.assertIsInstance(evidence.extracted_at, datetime)
        self.assertEqual(evidence.extracted_at.tzinfo, timezone.utc)

    def test_claim_evidence_requires_timezone_aware_datetime_and_normalizes_to_utc(
        self,
    ) -> None:
        source_time = datetime(
            2026,
            8,
            9,
            15,
            30,
            tzinfo=timezone(timedelta(hours=3)),
        )
        evidence = ClaimEvidence(
            raw_text="Battery Life: Up to 20 hours",
            source_url="https://example.com/product",
            extraction_method="html_spec_table",
            extracted_at=source_time,
        )

        self.assertEqual(
            evidence.extracted_at,
            datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(evidence.extracted_at.tzinfo, timezone.utc)

        with self.assertRaises(ValidationError):
            ClaimEvidence(
                raw_text="Battery Life: Up to 20 hours",
                source_url="https://example.com/product",
                extraction_method="html_spec_table",
                extracted_at=datetime(2026, 8, 9, 12, 30),
            )

    def test_claim_evidence_rejects_blank_required_fields_and_unknown_fields(
        self,
    ) -> None:
        valid_data = {
            "raw_text": "Battery Life: Up to 20 hours",
            "source_url": "https://example.com/product",
            "extraction_method": "html_spec_table",
        }

        for field_name in ("raw_text", "source_url", "extraction_method"):
            with self.subTest(field=field_name), self.assertRaises(ValidationError):
                ClaimEvidence(**{**valid_data, field_name: "   "})

        with self.assertRaises(ValidationError) as context:
            ClaimEvidence(**valid_data, unsupported="not allowed")

        self.assertIn(
            ("unsupported",),
            {tuple(error["loc"]) for error in context.exception.errors()},
        )

    def test_product_creation(self) -> None:
        product = Product(
            id="soundcore-select-4-go",
            name="Select 4 Go",
            brand="Soundcore",
            category="Portable Bluetooth Speaker",
        )

        self.assertEqual(product.name, "Select 4 Go")
        self.assertEqual(product.images, [])
        self.assertEqual(product.specs, [])
        self.assertEqual(product.features, [])
        self.assertEqual(product.sources, [])

    def test_nested_models_are_preserved(self) -> None:
        product = Product(
            id="soundcore-select-4-go-green-yellow",
            name="Select 4 Go",
            brand="Soundcore",
            category="Portable Bluetooth Speaker",
            variant=ProductVariant(
                color="Green / Yellow",
                model_number="manual-model-reference",
            ),
            images=[
                ProductImage(
                    id="primary",
                    local_path="soundcore/primary.jpg",
                    angle="front",
                    width=1024,
                    height=1536,
                    confidence=0.9,
                )
            ],
            specs=[
                ProductSpec(
                    key="sample_boolean",
                    label="Sample Boolean",
                    value=True,
                )
            ],
            features=[
                ProductFeature(
                    id="manual-feature",
                    name="Manual feature",
                )
            ],
            sources=[
                ProductSource(
                    url="https://example.com/product",
                    source_type="manual",
                    domain="example.com",
                )
            ],
        )

        self.assertIsInstance(product.variant, ProductVariant)
        self.assertIsInstance(product.images[0], ProductImage)
        self.assertIsInstance(product.specs[0], ProductSpec)
        self.assertIsInstance(product.features[0], ProductFeature)
        self.assertIsInstance(product.sources[0], ProductSource)
        self.assertFalse(product.specs[0].verified)
        self.assertFalse(product.features[0].verified)

    def test_spec_values_preserve_practical_primitive_types(self) -> None:
        values = ["portable", 20, 1.5, True]

        for index, value in enumerate(values):
            with self.subTest(value=value):
                spec = ProductSpec(
                    key=f"sample_{index}",
                    label=f"Sample {index}",
                    value=value,
                )
                self.assertEqual(spec.value, value)
                self.assertIs(type(spec.value), type(value))

    def test_invalid_product_data_reports_all_field_errors(self) -> None:
        with self.assertRaises(ValidationError) as context:
            Product.model_validate(
                {
                    "id": "soundcore-select-4-go",
                    "brand": "Soundcore",
                    "category": "Portable Bluetooth Speaker",
                    "images": [
                        {
                            "id": "primary",
                            "confidence": 1.5,
                            "width": 0,
                        }
                    ],
                    "unexpected": "not allowed",
                }
            )

        error_locations = {tuple(error["loc"]) for error in context.exception.errors()}
        self.assertIn(("name",), error_locations)
        self.assertIn(("images", 0, "confidence"), error_locations)
        self.assertIn(("images", 0, "width"), error_locations)
        self.assertIn(("unexpected",), error_locations)

    def test_boolean_metadata_is_not_coerced_to_numeric_fields(self) -> None:
        with self.assertRaises(ValidationError) as context:
            Product.model_validate(
                {
                    "id": "soundcore-select-4-go",
                    "name": "Select 4 Go",
                    "brand": "Soundcore",
                    "category": "Portable Bluetooth Speaker",
                    "images": [
                        {
                            "id": "primary",
                            "width": True,
                            "height": False,
                            "confidence": True,
                        }
                    ],
                    "specs": [
                        {
                            "key": "sample",
                            "label": "Sample",
                            "value": True,
                            "confidence": False,
                        }
                    ],
                    "features": [
                        {
                            "id": "sample",
                            "name": "Sample",
                            "confidence": True,
                        }
                    ],
                }
            )

        error_locations = {tuple(error["loc"]) for error in context.exception.errors()}
        self.assertEqual(
            error_locations,
            {
                ("images", 0, "width"),
                ("images", 0, "height"),
                ("images", 0, "confidence"),
                ("specs", 0, "confidence"),
                ("features", 0, "confidence"),
            },
        )


class ProductPersistenceTests(unittest.TestCase):
    def test_product_spec_evidence_round_trips_through_json(self) -> None:
        extracted_at = datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc)
        product = Product(
            id="example-speaker",
            name="Example Speaker",
            brand="Example",
            category="Portable Speaker",
            specs=[
                ProductSpec(
                    key="battery_life",
                    label="Battery Life",
                    value="up to 20 hours",
                    source_url="https://example.com/product",
                    confidence=0.85,
                    evidence=ClaimEvidence(
                        raw_text="Battery Life: Up to 20 hours",
                        source_url="https://example.com/product",
                        extraction_method="html_spec_table",
                        section_heading="Specifications",
                        extracted_at=extracted_at,
                    ),
                )
            ],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "product.json"
            save_product(product, output_path)
            json_data = json.loads(output_path.read_text(encoding="utf-8"))
            loaded_product = load_product(output_path)

        evidence = loaded_product.specs[0].evidence
        self.assertIsInstance(evidence, ClaimEvidence)
        self.assertEqual(evidence.extracted_at, extracted_at)
        self.assertEqual(
            json_data["specs"][0]["evidence"]["extracted_at"],
            "2026-08-09T12:30:00Z",
        )
        self.assertEqual(loaded_product, product)

    def test_product_feature_evidence_round_trips_through_json(self) -> None:
        product = Product(
            id="example-speaker",
            name="Example Speaker",
            brand="Example",
            category="Portable Speaker",
            features=[
                ProductFeature(
                    id="carry-loop",
                    name="Carry loop",
                    source_url="https://example.com/product",
                    confidence=0.75,
                    evidence=ClaimEvidence(
                        raw_text="Carry loop: Included for convenient carrying.",
                        source_url="https://example.com/product",
                        extraction_method="explicit_feature_section",
                        section_heading="Features",
                    ),
                )
            ],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "product.json"
            save_product(product, output_path)
            loaded_product = load_product(output_path)

        evidence = loaded_product.features[0].evidence
        self.assertIsInstance(evidence, ClaimEvidence)
        self.assertEqual(evidence.raw_text, "Carry loop: Included for convenient carrying.")
        self.assertEqual(loaded_product, product)

    def test_save_product_writes_human_readable_utf8_json(self) -> None:
        product = Product(
            id="cafe-speaker",
            name="Café Speaker",
            brand="Example",
            category="Portable Speaker",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "product.json"

            saved_path = save_product(product, output_path)
            json_text = output_path.read_text(encoding="utf-8")

        self.assertEqual(saved_path, output_path)
        self.assertIn('\n  "id": "cafe-speaker"', json_text)
        self.assertIn("Café Speaker", json_text)
        self.assertTrue(json_text.endswith("\n"))
        self.assertEqual(json.loads(json_text)["name"], "Café Speaker")

    def test_load_product_restores_nested_model_types(self) -> None:
        product_data = {
            "id": "soundcore-select-4-go-green-yellow",
            "name": "Select 4 Go",
            "brand": "Soundcore",
            "category": "Portable Bluetooth Speaker",
            "variant": {"color": "Green / Yellow"},
            "images": [{"id": "primary", "local_path": "soundcore/primary.jpg"}],
            "specs": [{"key": "sample", "label": "Sample", "value": 2}],
            "features": [{"id": "sample", "name": "Sample"}],
            "sources": [
                {
                    "url": "https://example.com/product",
                    "source_type": "manual",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory) / "product.json"
            input_path.write_text(
                json.dumps(product_data, ensure_ascii=False),
                encoding="utf-8",
            )

            loaded_product = load_product(input_path)

        self.assertIsInstance(loaded_product, Product)
        self.assertIsInstance(loaded_product.variant, ProductVariant)
        self.assertIsInstance(loaded_product.images[0], ProductImage)
        self.assertIsInstance(loaded_product.specs[0], ProductSpec)
        self.assertIsInstance(loaded_product.features[0], ProductFeature)
        self.assertIsInstance(loaded_product.sources[0], ProductSource)
        self.assertEqual(loaded_product.specs[0].value, 2)
        self.assertIsNone(loaded_product.specs[0].evidence)
        self.assertIsNone(loaded_product.features[0].evidence)
        self.assertFalse(loaded_product.specs[0].verified)

    def test_load_product_rejects_malformed_json_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory) / "product.json"
            input_path.write_text('{"id": "broken",', encoding="utf-8")

            with self.assertRaises(ValidationError) as context:
                load_product(input_path)

        self.assertEqual(context.exception.errors()[0]["type"], "json_invalid")


class ProductDemoTests(unittest.TestCase):
    def test_demo_writes_a_loadable_manual_soundcore_product(self) -> None:
        demo_path = Path(__file__).resolve().parents[1] / "demo_product.py"

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                [sys.executable, str(demo_path)],
                cwd=temporary_directory,
                capture_output=True,
                text=True,
                check=False,
            )
            output_path = Path(temporary_directory) / "product.json"

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output_path.is_file())
            loaded_product = load_product(output_path)

        self.assertEqual(loaded_product.brand, "Soundcore")
        self.assertEqual(loaded_product.name, "Select 4 Go")
        self.assertEqual(loaded_product.category, "Portable Bluetooth Speaker")
        self.assertEqual(loaded_product.variant.color, "Green / Yellow")
        self.assertEqual(loaded_product.specs, [])
        self.assertEqual(loaded_product.features, [])
        self.assertIn("Saved and loaded Soundcore Select 4 Go", result.stdout)


if __name__ == "__main__":
    unittest.main()
