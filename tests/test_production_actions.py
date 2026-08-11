import hashlib
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from ad_production_run import (
    RunDocumentReference,
    RunOperationDiagnostic,
    RunProductInput,
    attach_run_reference,
    attach_shot_reference,
    initialize_ad_production_run,
    load_ad_production_run,
    record_operation_diagnostic,
    save_ad_production_run,
    set_product_input,
)
from product import Product, ProductImage, load_product, save_product
from product_asset_manifest import load_product_asset_manifest
from product_derived_asset_manifest import load_product_derived_asset_manifest
from product_image_downloads import DownloadedProductImage
from ad_production_status import resolve_ad_production_status
from editing_plan import load_editing_plan
from final_ad_review import FinalAdReview
from hero_frame_review import HeroFrameReview
from production_actions import (
    EditingPlanActionInput,
    ProductionActionError,
    execute_build_editing_plan,
    execute_download_assets,
    execute_prepare_reference_assets,
    execute_record_final_review,
    execute_record_hero_review,
    execute_record_video_review,
    execute_research_product,
)
from video_shot_review import VideoShotReview
from hero_frame_review import HeroFrameReviewHistory

import tests.test_ad_production_status as status_fixture
import tests.test_ffmpeg_editor as editing_fixture


NOW = datetime(2026, 8, 11, 11, 0, tzinfo=timezone.utc)


class ProductionActionFoundationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.project_id = "speaker-action"
        self.url = "https://example.test/speaker"
        self.run = initialize_ad_production_run(
            self.workspace,
            self.project_id,
            product_input=RunProductInput(kind="product_url", value=self.url),
        )
        self.product = Product(
            id="product:speaker-action",
            name="Speaker",
            brand="Example",
            category="Portable speaker",
            images=[
                ProductImage(
                    id="front",
                    url="https://example.test/front.png",
                    source_url=self.url,
                    source_type="html_product_gallery_image",
                    width=600,
                    height=600,
                ),
                ProductImage(
                    id="side",
                    url="https://example.test/side.png",
                    source_url=self.url,
                    source_type="html_product_gallery_image",
                    width=600,
                    height=600,
                ),
            ],
        )

    def tearDown(self):
        self.directory.cleanup()

    def _attach_product(self):
        path = self.workspace / "documents" / "product.json"
        path.parent.mkdir()
        save_product(self.product, path)
        reference = RunDocumentReference(
            id=self.product.id,
            path="documents/product.json",
            content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        candidate = attach_run_reference(self.run, "product", reference)
        self.run = save_ad_production_run(
            candidate,
            self.workspace,
            self.project_id,
            expected_revision=self.run.revision,
        )

    def fake_downloader(self, product_id, image, *, storage_root, **_):
        output = Path(storage_root) / f"{image.id}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (600, 600), "green").save(output, format="PNG")
        data = output.read_bytes()
        return DownloadedProductImage(
            product_id=product_id,
            source_image_id=image.id,
            source_url=image.url,
            final_url=image.url,
            local_path=str(output.resolve()),
            sha256=hashlib.sha256(data).hexdigest(),
            byte_size=len(data),
            width=600,
            height=600,
            format="PNG",
            mime_type="image/png",
            downloaded_at=NOW,
        )

    def test_research_executes_once_persists_then_attaches_and_clears_failure(self):
        diagnostic = RunOperationDiagnostic(
            action="previous",
            code="old_failure",
            message="Old failure",
            recorded_at=NOW,
        )
        candidate = record_operation_diagnostic(self.run, diagnostic)
        self.run = save_ad_production_run(
            candidate,
            self.workspace,
            self.project_id,
            expected_revision=0,
        )
        calls = []

        def researcher(url, **_):
            calls.append(url)
            return self.product.model_copy(deep=True)

        result = execute_research_product(
            self.workspace,
            self.project_id,
            product_url=self.url,
            researcher=researcher,
            now=lambda: NOW,
            attempt_id="research-one",
        )
        self.assertEqual(calls, [self.url])
        self.assertEqual(result.receipt.status, "succeeded_and_attached")
        stored = load_ad_production_run(self.workspace, self.project_id)
        self.assertEqual(stored.revision, 2)
        self.assertIsNone(stored.latest_operation_diagnostic)
        self.assertIsNotNone(stored.product.content_sha256)
        self.assertEqual(
            load_product(self.workspace / stored.product.path), self.product
        )
        self.assertIsNone(stored.campaign)

    def test_duplicate_attempt_id_is_rejected_before_second_research_call(self):
        calls = []

        def researcher(url, **_):
            calls.append(url)
            return self.product.model_copy(deep=True)

        execute_research_product(
            self.workspace,
            self.project_id,
            product_url=self.url,
            researcher=researcher,
            now=lambda: NOW,
            attempt_id="research-once",
        )
        with self.assertRaises(ProductionActionError):
            execute_research_product(
                self.workspace,
                self.project_id,
                product_url=self.url,
                researcher=researcher,
                now=lambda: NOW,
                attempt_id="research-once",
            )
        self.assertEqual(calls, [self.url])

    def test_content_hash_change_is_reported_as_stale_reference(self):
        self._attach_product()
        stored = load_ad_production_run(self.workspace, self.project_id)
        path = self.workspace / stored.product.path
        path.write_text("{}", encoding="utf-8")
        status = resolve_ad_production_status(stored, self.workspace)
        self.assertEqual(status.state, "awaiting_research")
        self.assertTrue(status.issues)

    def test_attachment_conflict_retains_product_receipt_and_latest_failure(self):
        started = threading.Event()
        release = threading.Event()
        results = []

        def researcher(_url, **_):
            started.set()
            release.wait(timeout=5)
            return self.product.model_copy(deep=True)

        def execute():
            results.append(
                execute_research_product(
                    self.workspace,
                    self.project_id,
                    product_url=self.url,
                    researcher=researcher,
                    now=lambda: NOW,
                    attempt_id="research-conflict",
                )
            )

        thread = threading.Thread(target=execute)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        current = load_ad_production_run(self.workspace, self.project_id)
        changed = set_product_input(
            current,
            RunProductInput(kind="product_name", value="Different speaker"),
        )
        save_ad_production_run(
            changed,
            self.workspace,
            self.project_id,
            expected_revision=current.revision,
        )
        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].receipt.status, "artifact_created_unattached")
        self.assertTrue(
            (self.workspace / results[0].reference.path).is_file()
        )
        stored = load_ad_production_run(self.workspace, self.project_id)
        self.assertIsNone(stored.product)
        self.assertEqual(stored.latest_operation_diagnostic.code, "attachment_conflict")

    def test_download_requires_explicit_ids_and_never_creates_reference_sheets(self):
        self._attach_product()
        calls = []

        def downloader(*args, **kwargs):
            calls.append(args[1].id)
            return self.fake_downloader(*args, **kwargs)

        with self.assertRaises(ProductionActionError):
            execute_download_assets(
                self.workspace,
                self.project_id,
                image_ids=[],
                downloader=downloader,
                now=lambda: NOW,
                attempt_id="download-empty",
            )
        self.assertEqual(calls, [])
        failed_run = load_ad_production_run(self.workspace, self.project_id)
        self.assertEqual(failed_run.latest_operation_diagnostic.code, "action_failed")
        self.assertTrue(
            (
                self.workspace
                / "projects"
                / self.project_id
                / "attempts"
                / "download_assets"
                / "download-empty.json"
            ).is_file()
        )
        result = execute_download_assets(
            self.workspace,
            self.project_id,
            image_ids=["front"],
            downloader=downloader,
            now=lambda: NOW,
            attempt_id="download-front",
        )
        self.assertEqual(calls, ["front"])
        manifest = load_product_asset_manifest(
            self.workspace / result.reference.path
        )
        self.assertEqual([item.source_image_id for item in manifest.images], ["front"])
        stored = load_ad_production_run(self.workspace, self.project_id)
        self.assertIsNotNone(stored.original_asset_manifest)
        self.assertIsNone(stored.derived_asset_manifest)
        self.assertEqual(list((self.workspace / "projects" / self.project_id).rglob("reference-sheet*.png")), [])

    def test_prepare_reference_assets_uses_current_downloads_without_downloading(self):
        self._attach_product()
        execute_download_assets(
            self.workspace,
            self.project_id,
            image_ids=["front"],
            downloader=self.fake_downloader,
            now=lambda: NOW,
            attempt_id="download-front",
        )
        result = execute_prepare_reference_assets(
            self.workspace,
            self.project_id,
            now=lambda: NOW,
            attempt_id="prepare-one",
        )
        manifest = load_product_derived_asset_manifest(
            self.workspace / result.reference.path
        )
        self.assertEqual(len(manifest.reference_sheets), 1)
        self.assertTrue(Path(manifest.reference_sheets[0].local_path).is_file())
        stored = load_ad_production_run(self.workspace, self.project_id)
        self.assertEqual(stored.derived_asset_manifest, result.reference)

    def test_action_storage_rejects_project_directory_symlink_escape(self):
        outside = self.workspace / "outside"
        outside.mkdir()
        project = self.workspace / "projects" / self.project_id
        (project / "documents").symlink_to(outside, target_is_directory=True)
        calls = []
        with self.assertRaises(ProductionActionError):
            execute_research_product(
                self.workspace,
                self.project_id,
                product_url=self.url,
                researcher=lambda *_args, **_kwargs: calls.append(True) or self.product,
                now=lambda: NOW,
                attempt_id="research-escape",
            )
        self.assertEqual(calls, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_exact_output_symlink_cannot_overwrite_external_file(self):
        outside = self.workspace / "outside-product.json"
        outside.write_text("untouched", encoding="utf-8")
        project = self.workspace / "projects" / self.project_id
        documents = project / "documents"
        documents.mkdir()
        compact = self.product.model_dump_json(
            ensure_ascii=False, exclude_none=True
        ).encode("utf-8")
        digest = hashlib.sha256(compact).hexdigest()
        (documents / f"product-{digest}.json").symlink_to(outside)
        with self.assertRaises(ProductionActionError):
            execute_research_product(
                self.workspace,
                self.project_id,
                product_url=self.url,
                researcher=lambda *_args, **_kwargs: self.product,
                now=lambda: NOW,
                attempt_id="research-file-escape",
            )
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")


class ProductionReviewAndEditingActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        status_fixture.AdProductionStatusTests.setUpClass()
        cls.source = status_fixture.AdProductionStatusTests

    @classmethod
    def tearDownClass(cls):
        status_fixture.AdProductionStatusTests.tearDownClass()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.project_id = "review-actions"
        self.run = initialize_ad_production_run(
            self.workspace,
            self.project_id,
            product_input=RunProductInput(kind="product_name", value="Speaker"),
        )
        self.refs = self._write_documents()

    def tearDown(self):
        self.directory.cleanup()

    def _write(self, name, model, identity):
        path = self.workspace / "inputs" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True) + "\n",
            encoding="utf-8",
        )
        return RunDocumentReference(
            id=identity,
            path=path.relative_to(self.workspace).as_posix(),
            content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def _write_documents(self):
        source = self.source
        refs = {
            "product": self._write("product", source.product, source.product.id),
            "campaign": self._write("campaign", source.campaign, source.campaign.id),
            "storyboard": self._write("storyboard", source.storyboard, source.storyboard.id),
            "original_asset_manifest": self._write(
                "originals", source.original_manifest, source.product.id
            ),
            "derived_asset_manifest": self._write(
                "derived", source.derived_manifest, source.product.id
            ),
            "editing_audio_manifest": self._write(
                "audio", source.audio_manifest, source.product.id
            ),
            "asset_plan": self._write("asset-plan", source.asset_plan, source.asset_plan.id),
            "editing_plan": self._write(
                "editing-plan", source.editing_plan, source.editing_plan.id
            ),
            "editing_artifact": self._write(
                "editing-artifact", source.editing_artifact, source.editing_artifact.id
            ),
            "shots": {},
        }
        for shot, hero, hero_history, intent, video, video_history in zip(
            source.storyboard.shots,
            source.hero_artifacts,
            source.hero_histories,
            source.intents,
            source.video_artifacts,
            source.video_histories,
        ):
            refs["shots"][shot.id] = {
                "hero_frame_artifact": self._write(f"{shot.id}-hero", hero, hero.id),
                "hero_frame_review_history": self._write(
                    f"{shot.id}-hero-review", hero_history, hero_history.review_context.id
                ),
                "video_intent": self._write(f"{shot.id}-intent", intent, intent.id),
                "video_artifact": self._write(f"{shot.id}-video", video, video.id),
                "video_review_history": self._write(
                    f"{shot.id}-video-review", video_history, video_history.review_context.id
                ),
            }
        return refs

    def _base_run(self, *, complete_shots=False, editing=False):
        run = self.run
        for kind in ("product", "campaign"):
            run = attach_run_reference(run, kind, self.refs[kind])
        run = attach_run_reference(
            run,
            "storyboard",
            self.refs["storyboard"],
            shot_ids=tuple(shot.id for shot in self.source.storyboard.shots),
        )
        for kind in (
            "original_asset_manifest",
            "derived_asset_manifest",
            "editing_audio_manifest",
            "asset_plan",
        ):
            run = attach_run_reference(run, kind, self.refs[kind])
        if complete_shots:
            for shot in self.source.storyboard.shots:
                for kind in (
                    "hero_frame_artifact",
                    "hero_frame_review_history",
                    "video_intent",
                    "video_artifact",
                    "video_review_history",
                ):
                    run = attach_shot_reference(
                        run, shot.id, kind, self.refs["shots"][shot.id][kind]
                    )
        if editing:
            run = attach_run_reference(run, "editing_plan", self.refs["editing_plan"])
            run = attach_run_reference(run, "editing_artifact", self.refs["editing_artifact"])
        self.run = save_ad_production_run(
            run,
            self.workspace,
            self.project_id,
            expected_revision=0,
        )
        return self.run

    def test_hero_review_appends_real_history_and_does_not_create_motion_intent(self):
        self._base_run()
        shot = self.source.storyboard.shots[0]
        current = load_ad_production_run(self.workspace, self.project_id)
        candidate = attach_shot_reference(
            current,
            shot.id,
            "hero_frame_artifact",
            self.refs["shots"][shot.id]["hero_frame_artifact"],
        )
        save_ad_production_run(
            candidate,
            self.workspace,
            self.project_id,
            expected_revision=current.revision,
        )
        review = self.source.hero_histories[0].reviews[-1]
        before = deepcopy(review.model_dump())
        result = execute_record_hero_review(
            self.workspace,
            self.project_id,
            shot_id=shot.id,
            review=review,
            now=lambda: NOW,
            attempt_id="hero-review-one",
        )
        self.assertEqual(review.model_dump(), before)
        stored = load_ad_production_run(self.workspace, self.project_id)
        stored_shot = next(item for item in stored.shots if item.shot_id == shot.id)
        self.assertEqual(stored_shot.hero_frame_review_history, result.reference)
        self.assertIsNone(stored_shot.video_intent)
        second_review = HeroFrameReview.model_validate(
            {
                **review.model_dump(),
                "review_id": "hero-review-second",
                "reviewed_at": review.reviewed_at + timedelta(seconds=1),
            }
        )
        second = execute_record_hero_review(
            self.workspace,
            self.project_id,
            shot_id=shot.id,
            review=second_review,
            now=lambda: NOW,
            attempt_id="hero-review-two",
        )
        self.assertNotEqual(
            result.reference.content_sha256, second.reference.content_sha256
        )
        history = HeroFrameReviewHistory.model_validate_json(
            (self.workspace / second.reference.path).read_text(encoding="utf-8")
        )
        self.assertEqual([item.review_id for item in history.reviews], [review.review_id, "hero-review-second"])

    def test_video_review_appends_real_history_and_does_not_build_editing_plan(self):
        self._base_run()
        shot = self.source.storyboard.shots[0]
        current = load_ad_production_run(self.workspace, self.project_id)
        for kind in (
            "hero_frame_artifact",
            "hero_frame_review_history",
            "video_intent",
            "video_artifact",
        ):
            current = attach_shot_reference(
                current, shot.id, kind, self.refs["shots"][shot.id][kind]
            )
        save_ad_production_run(
            current,
            self.workspace,
            self.project_id,
            expected_revision=1,
        )
        review = self.source.video_histories[0].reviews[-1]
        result = execute_record_video_review(
            self.workspace,
            self.project_id,
            shot_id=shot.id,
            review=review,
            now=lambda: NOW,
            attempt_id="video-review-one",
        )
        stored = load_ad_production_run(self.workspace, self.project_id)
        stored_shot = next(item for item in stored.shots if item.shot_id == shot.id)
        self.assertEqual(stored_shot.video_review_history, result.reference)
        self.assertIsNone(stored.editing_plan)

    def test_build_editing_plan_uses_strict_authored_input_and_does_not_render(self):
        self._base_run(complete_shots=True)
        _, selections, timings = editing_fixture.make_inputs(self.source.storyboard)
        authored = EditingPlanActionInput(
            segment_selections=tuple(selections),
            overlay_timings=tuple(timings),
        )
        before = deepcopy(authored.model_dump())
        result = execute_build_editing_plan(
            self.workspace,
            self.project_id,
            authored_input=authored,
            now=lambda: NOW,
            attempt_id="editing-plan-one",
        )
        self.assertEqual(authored.model_dump(), before)
        plan = load_editing_plan(self.workspace / result.reference.path)
        self.assertEqual(plan, self.source.editing_plan)
        stored = load_ad_production_run(self.workspace, self.project_id)
        self.assertEqual(stored.editing_plan, result.reference)
        self.assertIsNone(stored.editing_artifact)
        with self.assertRaises(ValidationError):
            EditingPlanActionInput.model_validate(
                {**authored.model_dump(), "automatic_segments": True}
            )

    def test_final_review_appends_only_and_does_not_create_deliverable_or_media(self):
        self._base_run(complete_shots=True, editing=True)
        review = self.source.final_history.reviews[-1]
        media_before = Path(self.source.editing_artifact.local_path).read_bytes()
        result = execute_record_final_review(
            self.workspace,
            self.project_id,
            review=review,
            now=lambda: NOW,
            attempt_id="final-review-one",
        )
        stored = load_ad_production_run(self.workspace, self.project_id)
        self.assertEqual(stored.final_review_history, result.reference)
        self.assertEqual(Path(self.source.editing_artifact.local_path).read_bytes(), media_before)
        self.assertFalse(hasattr(result, "deliverable"))


if __name__ == "__main__":
    unittest.main()
