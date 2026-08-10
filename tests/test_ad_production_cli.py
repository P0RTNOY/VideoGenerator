import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ad_claims import ProductAdClaimSelection
from ad_production_cli import build_parser, main
from ad_production_run import load_ad_production_run
from campaign import AdCampaign
from editing_audio_assets import EditingAudioManifest
from product import Product
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from storyboard import Storyboard, StoryboardShot
from storyboard_asset_plan import StoryboardAssetPlan


class AdProductionCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.project_id = "local-speaker-ad"
        self.product = Product(
            id="product:local-speaker",
            name="Local Speaker",
            brand="Example",
            category="Portable speaker",
        )
        self.campaign = AdCampaign(
            id="campaign:local-speaker",
            claim_selection=ProductAdClaimSelection(
                product_id=self.product.id,
                selected_claim_ids=[],
            ),
            ad_format="vertical_social",
            aspect_ratio="9:16",
            target_duration_seconds=5,
            creative_style="minimal",
            environment="Studio",
            language_tag="en",
            claim_snapshots=[],
        )
        self.storyboard = Storyboard(
            id="storyboard:local-speaker",
            campaign_id=self.campaign.id,
            shots=[
                StoryboardShot(
                    id="hero",
                    purpose="hook",
                    duration_seconds=5.0,
                    visual_description="A static product composition.",
                )
            ],
        )
        self.originals = ProductAssetManifest(product_id=self.product.id)
        self.derived = ProductDerivedAssetManifest(product_id=self.product.id)
        self.audio = EditingAudioManifest(product_id=self.product.id)
        self.asset_plan = StoryboardAssetPlan(
            id="asset-plan:local-speaker",
            product_id=self.product.id,
            campaign_id=self.campaign.id,
            storyboard_id=self.storyboard.id,
        )
        self.documents = {
            "product": self._write("documents/product.json", self.product),
            "campaign": self._write("documents/campaign.json", self.campaign),
            "storyboard": self._write("documents/storyboard.json", self.storyboard),
            "originals": self._write("documents/originals.json", self.originals),
            "derived": self._write("documents/derived.json", self.derived),
            "audio": self._write("documents/audio.json", self.audio),
            "asset_plan": self._write("documents/asset-plan.json", self.asset_plan),
        }

    def tearDown(self):
        self.directory.cleanup()

    def _write(self, relative_path, model):
        path = self.workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True) + "\n",
            encoding="utf-8",
        )
        return relative_path

    def invoke(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--workspace", str(self.workspace), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def initialize(self, *input_arguments):
        code, output, error = self.invoke("init", self.project_id, *input_arguments)
        self.assertEqual((code, error), (0, ""))
        return json.loads(output)

    def attach(self, command, document_key, *extra):
        code, output, error = self.invoke(
            command,
            self.project_id,
            *extra,
            self.documents[document_key],
        )
        self.assertEqual((code, error), (0, ""))
        return json.loads(output)

    def test_init_creates_one_revision_zero_run_and_refuses_collision(self):
        result = self.initialize("--product-name", "Local Speaker")
        self.assertEqual(result["project_id"], self.project_id)
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["product_input"]["kind"], "product_name")
        run_path = self.workspace / "projects" / self.project_id / "run.json"
        self.assertTrue(run_path.is_file())
        code, _, error = self.invoke("init", self.project_id)
        self.assertEqual(code, 2)
        self.assertIn("already exists", error)

    def test_status_and_validate_report_derived_state_without_mutating_run(self):
        self.initialize("--product-url", "https://example.test/speaker")
        before = (self.workspace / "projects" / self.project_id / "run.json").read_bytes()
        code, output, error = self.invoke("status", self.project_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["state"], "awaiting_research")
        code, output, error = self.invoke("validate", self.project_id)
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(json.loads(output)["valid"])
        self.assertEqual(
            (self.workspace / "projects" / self.project_id / "run.json").read_bytes(),
            before,
        )

    def test_approved_attachment_commands_store_only_relative_references(self):
        self.initialize("--product-name", "Local Speaker")
        self.attach("attach-product", "product")
        self.attach("attach-campaign", "campaign")
        storyboard_result = self.attach("attach-storyboard", "storyboard")
        self.assertEqual([item["shot_id"] for item in storyboard_result["shots"]], ["hero"])
        self.attach("attach-original-assets", "originals")
        self.attach("attach-derived-assets", "derived")
        self.attach("attach-editing-audio", "audio")
        result = self.attach("attach-asset-plan", "asset_plan")
        self.assertEqual(result["revision"], 7)
        self.assertEqual(result["product"]["path"], "documents/product.json")
        self.assertFalse(Path(result["product"]["path"]).is_absolute())

    def test_set_product_input_invalidates_downstream_references_only(self):
        self.initialize("--product-name", "Local Speaker")
        self.attach("attach-product", "product")
        code, output, error = self.invoke(
            "set-product-input",
            self.project_id,
            "--product-name",
            "Replacement Speaker",
        )
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["product_input"]["value"], "Replacement Speaker")
        self.assertNotIn("product", result)
        self.assertTrue((self.workspace / self.documents["product"]).is_file())

    def test_malformed_or_wrong_document_is_rejected_without_run_mutation(self):
        self.initialize()
        run_path = self.workspace / "projects" / self.project_id / "run.json"
        before = run_path.read_bytes()
        wrong = self._write("documents/not-product.json", self.campaign)
        code, _, error = self.invoke("attach-product", self.project_id, wrong)
        self.assertEqual(code, 2)
        self.assertIn("Product", error)
        self.assertEqual(run_path.read_bytes(), before)

    def test_absolute_traversal_and_symlink_paths_are_rejected(self):
        self.initialize()
        for supplied_path in (
            str(self.workspace / self.documents["product"]),
            "../outside.json",
        ):
            code, _, error = self.invoke(
                "attach-product", self.project_id, supplied_path
            )
            self.assertEqual(code, 2)
            self.assertIn("workspace-relative", error)
        outside = Path(self.directory.name).parent / "outside-product.json"
        outside.write_text(self.product.model_dump_json(), encoding="utf-8")
        link = self.workspace / "documents" / "linked-product.json"
        link.symlink_to(outside)
        try:
            code, _, error = self.invoke(
                "attach-product", self.project_id, "documents/linked-product.json"
            )
            self.assertEqual(code, 2)
            self.assertIn("symbolic link", error)
        finally:
            outside.unlink()

    def test_validate_returns_nonzero_for_a_stale_reference_but_no_failed_state(self):
        self.initialize()
        self.attach("attach-product", "product")
        (self.workspace / self.documents["product"]).write_text("{}", encoding="utf-8")
        code, output, error = self.invoke("validate", self.project_id)
        self.assertEqual((code, error), (1, ""))
        result = json.loads(output)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"]["state"], "awaiting_research")
        self.assertNotEqual(result["status"]["state"], "failed")

    def test_cli_exposes_local_reference_commands_and_no_execution_actions(self):
        help_text = build_parser().format_help()
        for command in (
            "init",
            "status",
            "validate",
            "attach-product",
            "attach-campaign",
            "attach-storyboard",
            "attach-asset-plan",
            "attach-shot-video-intent",
            "attach-editing-plan",
        ):
            self.assertIn(command, help_text)
        for forbidden in (
            "research-product",
            "download-assets",
            "generate-hero",
            "generate-video",
            "render-ad",
        ):
            self.assertNotIn(forbidden, help_text)


if __name__ == "__main__":
    unittest.main()
