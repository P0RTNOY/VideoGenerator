import io
import json
import threading
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ad_claims import ProductAdClaimSelection
import ad_production_cli
from action_confirmation import create_action_preview
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

    def test_cli_exposes_all_explicit_execution_commands_without_generic_execute(self):
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
        for command in (
            "research-product",
            "download-assets",
            "prepare-reference-assets",
            "preview-generate-hero",
            "generate-hero",
            "record-hero-review",
            "preview-generate-video",
            "generate-video",
            "record-video-review",
            "build-editing-plan",
            "preview-render",
            "render-editing-plan",
            "record-final-review",
        ):
            self.assertIn(command, help_text)
        self.assertNotIn("execute-anything", help_text)

    def test_nonpaid_commands_dispatch_exactly_one_action_without_confirmation(self):
        self.initialize("--product-url", "https://example.test/speaker")
        input_path = self.documents["product"]
        cases = (
            (
                "execute_research_product",
                ("research-product", self.project_id, "--product-url", "https://example.test/speaker", "--attempt-id", "a1"),
            ),
            (
                "execute_download_assets",
                ("download-assets", self.project_id, "--image-id", "image:one", "--attempt-id", "a2"),
            ),
            (
                "execute_prepare_reference_assets",
                ("prepare-reference-assets", self.project_id, "--attempt-id", "a3"),
            ),
            (
                "execute_record_hero_review",
                ("record-hero-review", self.project_id, "hero", "--review-file", input_path, "--attempt-id", "a4"),
            ),
            (
                "execute_record_video_review",
                ("record-video-review", self.project_id, "hero", "--review-file", input_path, "--attempt-id", "a5"),
            ),
            (
                "execute_build_editing_plan",
                ("build-editing-plan", self.project_id, "--input-file", input_path, "--attempt-id", "a6"),
            ),
            (
                "execute_record_final_review",
                ("record-final-review", self.project_id, "--review-file", input_path, "--attempt-id", "a7"),
            ),
        )
        for target, arguments in cases:
            with self.subTest(command=arguments[0]), patch.object(
                ad_production_cli, target, return_value=self.product
            ) as action, patch.object(
                ad_production_cli, "_load_action_input", return_value=self.product
            ):
                    code, _, error = self.invoke(*arguments)
                    self.assertEqual((code, error), (0, ""))
                    action.assert_called_once()

    def test_preview_commands_print_exact_token_and_execution_template(self):
        self.initialize("--product-name", "Speaker")
        now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
        paid = create_action_preview(
            project_id=self.project_id,
            run_revision=0,
            action="generate_hero",
            request_fingerprint="1" * 64,
            provider="google-gemini",
            model="gemini-3.1-flash-image",
            options_fingerprint="2" * 64,
            requested_output_count=1,
            cost_quote_status="unavailable",
            owner_cost_ceiling_usd=1.0,
            allow_unquoted_cost=True,
            created_at=now,
        )
        render = create_action_preview(
            project_id=self.project_id,
            run_revision=0,
            action="render_editing_plan",
            request_fingerprint="3" * 64,
            options_fingerprint="4" * 64,
            requested_output_count=1,
            cost_quote_status="not_applicable",
            created_at=now,
        )
        cases = (
            (
                "create_generate_hero_preview",
                paid,
                ("preview-generate-hero", self.project_id, "hero", "--provider", "google-gemini", "--model", "gemini-3.1-flash-image", "--cost-quote-status", "unavailable", "--allow-unquoted-cost", "--owner-cost-ceiling-usd", "1"),
            ),
            (
                "create_generate_video_preview",
                paid.model_copy(update={"action": "generate_video"}),
                ("preview-generate-video", self.project_id, "hero", "--provider", "google-gemini-api", "--model", "veo-3.1-fast-generate-preview", "--cost-quote-status", "unavailable", "--allow-unquoted-cost", "--owner-cost-ceiling-usd", "1"),
            ),
            (
                "create_render_editing_plan_preview",
                render,
                ("preview-render", self.project_id),
            ),
        )
        for target, preview, arguments in cases:
            with self.subTest(command=arguments[0]), patch.object(
                ad_production_cli, target, return_value=preview
            ) as action:
                code, output, error = self.invoke(*arguments)
                self.assertEqual((code, error), (0, ""))
                payload = json.loads(output)
                self.assertEqual(payload["preview"]["id"], preview.id)
                self.assertIn(preview.id, payload["execution_command"])
                action.assert_called_once()

    def test_paid_and_render_commands_require_exact_confirmation_and_call_one_action(self):
        self.initialize("--product-name", "Speaker")
        token = "action-preview:" + "a" * 64
        cases = (
            (
                "execute_generate_hero",
                ("generate-hero", self.project_id, "hero", "--provider", "google-gemini", "--model", "gemini-3.1-flash-image"),
            ),
            (
                "execute_generate_video",
                ("generate-video", self.project_id, "hero", "--provider", "google-gemini-api", "--model", "veo-3.1-fast-generate-preview"),
            ),
            (
                "execute_render_editing_plan",
                ("render-editing-plan", self.project_id),
            ),
        )
        for target, prefix in cases:
            with self.subTest(command=prefix[0]):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args([*prefix, "--attempt-id", "attempt"])
                with patch.object(
                    ad_production_cli, target, return_value=self.product
                ) as action, patch.object(
                    ad_production_cli, "_hero_generator", return_value=object()
                ), patch.object(
                    ad_production_cli, "_video_generator", return_value=object()
                ):
                    code, _, error = self.invoke(
                        *prefix,
                        "--confirm-preview",
                        token,
                        "--attempt-id",
                        "attempt",
                    )
                    self.assertEqual((code, error), (0, ""))
                    action.assert_called_once()

    def test_review_file_is_strict_and_workspace_confined(self):
        self.initialize("--product-name", "Speaker")
        malformed = self.workspace / "documents" / "bad-review.json"
        malformed.write_text("{}", encoding="utf-8")
        for supplied in ("documents/bad-review.json", str(malformed)):
            code, _, error = self.invoke(
                "record-hero-review",
                self.project_id,
                "hero",
                "--review-file",
                supplied,
                "--attempt-id",
                "review-one",
            )
            self.assertEqual(code, 2)
            self.assertTrue(error)

    def _hero_review(self):
        from hero_frame_review import HeroFrameFidelityChecklist, HeroFrameReview

        return HeroFrameReview(
            review_id="review:hero",
            review_context_id="hero-frame-review-context:" + "a" * 64,
            reviewer_actor_id="owner:test",
            decision="approved_for_video_start_image",
            reviewed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            checklist=HeroFrameFidelityChecklist(
                geometry_and_proportions="pass",
                color_variant_and_materials="pass",
                controls_buttons_and_ports="pass",
                seams_and_panel_boundaries="pass",
                logos_markings_and_labels="pass",
                no_invented_or_missing_construction="pass",
                no_added_advertising_text="pass",
                scene_and_framing_usable="pass",
            ),
        )

    def test_local_fixture_smoke_researches_and_stops_at_campaign_boundary(self):
        fixture_root = Path(__file__).parent / "fixtures"

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(QuietHandler, directory=str(fixture_root)),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/jsonld_product.html"
            self.initialize("--product-url", url)
            code, output, error = self.invoke(
                "research-product",
                self.project_id,
                "--product-url",
                url,
                "--attempt-id",
                "local-fixture-research",
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["receipt"]["status"], "succeeded_and_attached")
            run = load_ad_production_run(self.workspace, self.project_id)
            self.assertIsNotNone(run.product)
            self.assertIsNone(run.campaign)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
