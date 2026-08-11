import hashlib
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ad_production_run import (
    RunDocumentReference,
    RunProductInput,
    attach_run_reference,
    attach_shot_reference,
    initialize_ad_production_run,
    load_ad_production_run,
    save_ad_production_run,
)
from confirmed_production_actions import (
    ConfirmedProductionActionError,
    RenderEditingPlanOptions,
    create_generate_hero_preview,
    create_generate_video_preview,
    create_render_editing_plan_preview,
    execute_generate_hero,
    execute_generate_video,
    execute_render_editing_plan,
)
from editing_artifact import EditingArtifact, EditingReceipt
from editing_instruction import compile_editing_instruction
from ffmpeg_editor import EditingRenderResult
from hero_frame_artifact import (
    HeroFrameGenerationFailure,
    HeroFrameGenerationReceipt,
    HeroFrameGenerationResult,
    store_validated_hero_frame_artifact,
)
from hero_frame_generation import (
    HeroFrameGenerationOptions,
    HeroFrameProviderCapabilities,
)
from video_shot_artifact import (
    VideoGenerationReceipt,
    VideoGenerationResult,
    store_validated_video_shot_artifact,
)
from video_shot_generation import VideoGenerationOptions, VideoProviderCapabilities

import tests.test_ad_production_status as status_fixture
from tests.test_ffmpeg_editor import make_source
from tests.test_hero_frame_request import image_bytes


NOW = datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc)


class ConfirmedProductionActionTests(unittest.TestCase):
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
        self.project_id = "confirmed-actions"
        self.run = initialize_ad_production_run(
            self.workspace,
            self.project_id,
            product_input=RunProductInput(kind="product_name", value="Speaker"),
        )
        self.refs = self._write_documents()
        self.shot_id = self.source.storyboard.shots[0].id
        self.hero_options = HeroFrameGenerationOptions()
        self.video_options = VideoGenerationOptions()

    def tearDown(self):
        self.directory.cleanup()

    def _write(self, name, model, identity):
        path = self.workspace / "inputs" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True)
            + "\n",
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
            "storyboard": self._write(
                "storyboard", source.storyboard, source.storyboard.id
            ),
            "original_asset_manifest": self._write(
                "originals", source.original_manifest, source.product.id
            ),
            "derived_asset_manifest": self._write(
                "derived", source.derived_manifest, source.product.id
            ),
            "editing_audio_manifest": self._write(
                "audio", source.audio_manifest, source.product.id
            ),
            "asset_plan": self._write(
                "asset-plan", source.asset_plan, source.asset_plan.id
            ),
            "editing_plan": self._write(
                "editing-plan", source.editing_plan, source.editing_plan.id
            ),
            "shots": {},
        }
        for shot, hero, history, intent, video, video_history in zip(
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
                    f"{shot.id}-hero-review", history, history.review_context.id
                ),
                "video_intent": self._write(f"{shot.id}-intent", intent, intent.id),
                "video_artifact": self._write(f"{shot.id}-video", video, video.id),
                "video_review_history": self._write(
                    f"{shot.id}-video-review",
                    video_history,
                    video_history.review_context.id,
                ),
            }
        return refs

    def _install(self, stage):
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
        if stage in ("video", "render"):
            for shot in self.source.storyboard.shots:
                kinds = (
                    "hero_frame_artifact",
                    "hero_frame_review_history",
                    "video_intent",
                )
                if stage == "render" or shot.id != self.shot_id:
                    kinds += ("video_artifact", "video_review_history")
                for kind in kinds:
                    run = attach_shot_reference(
                        run, shot.id, kind, self.refs["shots"][shot.id][kind]
                    )
        if stage == "render":
            run = attach_run_reference(run, "editing_plan", self.refs["editing_plan"])
        self.run = save_ad_production_run(
            run, self.workspace, self.project_id, expected_revision=0
        )
        return self.run

    def _hero_preview(self, *, created_at=NOW, model="fake-hero-v1"):
        return create_generate_hero_preview(
            self.workspace,
            self.project_id,
            shot_id=self.shot_id,
            provider="fake-hero",
            model=model,
            options=self.hero_options,
            cost_quote_status="unavailable",
            allow_unquoted_cost=True,
            owner_cost_ceiling_usd=1.0,
            created_at=created_at,
        )

    def _video_preview(self):
        return create_generate_video_preview(
            self.workspace,
            self.project_id,
            shot_id=self.shot_id,
            provider="fake-video",
            model="fake-video-v1",
            options=self.video_options,
            cost_quote_status="unavailable",
            allow_unquoted_cost=True,
            owner_cost_ceiling_usd=2.0,
            created_at=NOW,
        )

    def test_hero_preview_is_required_consumed_and_executes_fake_once(self):
        self._install("hero")
        calls = []
        test = self

        class FakeHero:
            def capabilities(self):
                return HeroFrameProviderCapabilities(
                    provider="fake-hero",
                    supports_reference_images=True,
                    supports_multiple_object_references=True,
                    max_object_references=10,
                    supported_aspect_ratios=("9:16",),
                    supported_image_sizes=("1K",),
                    supported_output_formats=("PNG",),
                )

            def generate(self, request, options):
                calls.append((request, options))
                load_ad_production_run(test.workspace, test.project_id)
                artifact = store_validated_hero_frame_artifact(
                    image_bytes("PNG", size=(720, 1280), color=(31, 95, 61)),
                    request,
                    storage_root=test.workspace
                    / "projects"
                    / test.project_id
                    / "artifacts"
                    / "media",
                    created_at=NOW,
                )
                receipt = HeroFrameGenerationReceipt(
                    attempt_id="provider-hero-one",
                    outcome="succeeded",
                    artifact_id=artifact.id,
                    provider="fake-hero",
                    model="fake-hero-v1",
                    started_at=NOW,
                    completed_at=NOW,
                )
                return HeroFrameGenerationResult(
                    status="succeeded", artifact=artifact, receipt=receipt
                )

        with self.assertRaises(ConfirmedProductionActionError):
            execute_generate_hero(
                self.workspace,
                self.project_id,
                shot_id=self.shot_id,
                provider="fake-hero",
                model="fake-hero-v1",
                options=self.hero_options,
                generator=FakeHero(),
                confirm_preview="action-preview:" + "0" * 64,
                attempt_id="missing-preview",
                now=lambda: NOW,
            )
        preview = self._hero_preview()
        result = execute_generate_hero(
            self.workspace,
            self.project_id,
            shot_id=self.shot_id,
            provider="fake-hero",
            model="fake-hero-v1",
            options=self.hero_options,
            generator=FakeHero(),
            confirm_preview=preview.id,
            attempt_id="hero-one",
            now=lambda: NOW,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.receipt.preview_id, preview.id)
        self.assertIsNotNone(result.receipt.domain_receipt_reference)
        stored = load_ad_production_run(self.workspace, self.project_id)
        shot = next(item for item in stored.shots if item.shot_id == self.shot_id)
        self.assertEqual(shot.hero_frame_artifact, result.reference)
        self.assertIsNone(shot.hero_frame_review_history)
        with self.assertRaises(ConfirmedProductionActionError):
            execute_generate_hero(
                self.workspace,
                self.project_id,
                shot_id=self.shot_id,
                provider="fake-hero",
                model="fake-hero-v1",
                options=self.hero_options,
                generator=FakeHero(),
                confirm_preview=preview.id,
                attempt_id="hero-two",
                now=lambda: NOW,
            )
        self.assertEqual(len(calls), 1)

    def test_stale_expired_and_provider_mismatched_preview_never_calls_adapter(self):
        self._install("hero")
        preview = self._hero_preview(created_at=NOW - timedelta(minutes=31))

        class NeverHero:
            def capabilities(self):
                raise AssertionError("adapter must not be inspected")

            def generate(self, request, options):
                raise AssertionError("adapter must not be called")

        with self.assertRaises(ConfirmedProductionActionError):
            execute_generate_hero(
                self.workspace,
                self.project_id,
                shot_id=self.shot_id,
                provider="fake-hero",
                model="fake-hero-v1",
                options=self.hero_options,
                generator=NeverHero(),
                confirm_preview=preview.id,
                attempt_id="expired",
                now=lambda: NOW,
            )
        fresh = self._hero_preview()
        with self.assertRaises(ConfirmedProductionActionError):
            execute_generate_hero(
                self.workspace,
                self.project_id,
                shot_id=self.shot_id,
                provider="fake-hero",
                model="different-model",
                options=self.hero_options,
                generator=NeverHero(),
                confirm_preview=fresh.id,
                attempt_id="wrong-model",
                now=lambda: NOW,
            )
        from ad_production_run import RunOperationDiagnostic, record_operation_diagnostic

        current = load_ad_production_run(self.workspace, self.project_id)
        changed = record_operation_diagnostic(
            current,
            RunOperationDiagnostic(
                action="operator-note",
                code="revision-change",
                message="Changed after preview",
                recorded_at=NOW,
            ),
        )
        save_ad_production_run(
            changed,
            self.workspace,
            self.project_id,
            expected_revision=current.revision,
        )
        with self.assertRaises(ConfirmedProductionActionError):
            execute_generate_hero(
                self.workspace,
                self.project_id,
                shot_id=self.shot_id,
                provider="fake-hero",
                model="fake-hero-v1",
                options=self.hero_options,
                generator=NeverHero(),
                confirm_preview=fresh.id,
                attempt_id="stale-revision",
                now=lambda: NOW,
            )

    def test_failed_hero_attempt_has_no_retry_or_fallback_and_retains_receipt(self):
        self._install("hero")
        preview = self._hero_preview()
        calls = []

        class FailedHero:
            def capabilities(self):
                return HeroFrameProviderCapabilities(
                    provider="fake-hero",
                    supports_reference_images=True,
                    supports_multiple_object_references=True,
                    max_object_references=10,
                    supported_aspect_ratios=("9:16",),
                    supported_image_sizes=("1K",),
                    supported_output_formats=("PNG",),
                )

            def generate(self, request, options):
                calls.append(request)
                return HeroFrameGenerationResult(
                    status="failed",
                    receipt=HeroFrameGenerationReceipt(
                        attempt_id="provider-failed",
                        outcome="failed",
                        provider="fake-hero",
                        model="fake-hero-v1",
                        started_at=NOW,
                        completed_at=NOW,
                    ),
                    failure=HeroFrameGenerationFailure(
                        code="provider_rejected", message="Rejected"
                    ),
                )

        with self.assertRaises(ConfirmedProductionActionError):
            execute_generate_hero(
                self.workspace,
                self.project_id,
                shot_id=self.shot_id,
                provider="fake-hero",
                model="fake-hero-v1",
                options=self.hero_options,
                generator=FailedHero(),
                confirm_preview=preview.id,
                attempt_id="hero-failed",
                now=lambda: NOW,
            )
        self.assertEqual(len(calls), 1)
        project = self.workspace / "projects" / self.project_id
        receipt_text = (project / "attempts/generate_hero/hero-failed.json").read_text()
        self.assertIn("domain_receipt_reference", receipt_text)
        self.assertIsNone(
            next(
                item
                for item in load_ad_production_run(
                    self.workspace, self.project_id
                ).shots
                if item.shot_id == self.shot_id
            ).hero_frame_artifact
        )

    def test_video_generation_executes_once_and_does_not_review(self):
        self._install("video")
        preview = self._video_preview()
        calls = []
        test = self

        class FakeVideo:
            def capabilities(self):
                return VideoProviderCapabilities(
                    provider="fake-video",
                    model="fake-video-v1",
                    supports_start_image=True,
                    supported_aspect_ratios=("9:16",),
                    supported_resolutions=("720p",),
                    supported_durations_seconds=(10,),
                    videos_per_request=1,
                    audio_generation="always_on",
                )

            def generate(self, request):
                calls.append(request)
                load_ad_production_run(test.workspace, test.project_id)
                source = test.workspace / "fake-video.mp4"
                make_source(source, "teal", 410)
                artifact = store_validated_video_shot_artifact(
                    source.read_bytes(),
                    request,
                    storage_root=test.workspace
                    / "projects"
                    / test.project_id
                    / "artifacts"
                    / "media",
                    created_at=NOW,
                )
                return VideoGenerationResult(
                    status="succeeded",
                    artifact=artifact,
                    receipt=VideoGenerationReceipt(
                        attempt_id="provider-video-one",
                        outcome="succeeded",
                        artifact_id=artifact.id,
                        provider="fake-video",
                        model="fake-video-v1",
                        started_at=NOW,
                        completed_at=NOW,
                    ),
                )

        result = execute_generate_video(
            self.workspace,
            self.project_id,
            shot_id=self.shot_id,
            provider="fake-video",
            model="fake-video-v1",
            options=self.video_options,
            generator=FakeVideo(),
            confirm_preview=preview.id,
            attempt_id="video-one",
            now=lambda: NOW,
        )
        self.assertEqual(len(calls), 1)
        shot = next(
            item
            for item in load_ad_production_run(
                self.workspace, self.project_id
            ).shots
            if item.shot_id == self.shot_id
        )
        self.assertEqual(shot.video_artifact, result.reference)
        self.assertIsNone(shot.video_review_history)

    def test_render_executes_once_without_final_review_and_persists_no_secret(self):
        self._install("render")
        options = RenderEditingPlanOptions()
        preview = create_render_editing_plan_preview(
            self.workspace,
            self.project_id,
            options=options,
            created_at=NOW,
        )
        calls = []
        test = self

        def fake_renderer(*args, **kwargs):
            calls.append((args, kwargs))
            load_ad_production_run(test.workspace, test.project_id)
            media = (
                test.workspace
                / "projects"
                / test.project_id
                / "artifacts"
                / "media"
                / "edited.mp4"
            )
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(Path(test.source.editing_artifact.local_path).read_bytes())
            artifact = EditingArtifact.model_validate(
                {
                    **test.source.editing_artifact.model_dump(),
                    "local_path": str(media.resolve()),
                }
            )
            receipt = EditingReceipt(
                attempt_id="render-one-domain",
                editing_plan_id=test.source.editing_plan.id,
                artifact_id=artifact.id,
                started_at=NOW,
                completed_at=NOW,
                source_video_audio_discarded=True,
            )
            return EditingRenderResult(
                artifact=artifact,
                receipt=receipt,
                instruction=compile_editing_instruction(
                    plan=test.source.editing_plan,
                    product=test.source.product,
                    campaign=test.source.campaign,
                    storyboard=test.source.storyboard,
                    video_artifacts=test.source.video_artifacts,
                    video_review_histories=test.source.video_histories,
                    audio_manifest=test.source.audio_manifest,
                ),
            )

        os.environ["GEMINI_API_KEY"] = "must-not-persist"
        try:
            result = execute_render_editing_plan(
                self.workspace,
                self.project_id,
                options=options,
                renderer=fake_renderer,
                confirm_preview=preview.id,
                attempt_id="render-one",
                now=lambda: NOW,
            )
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
        self.assertEqual(len(calls), 1)
        stored = load_ad_production_run(self.workspace, self.project_id)
        self.assertEqual(stored.editing_artifact, result.reference)
        self.assertIsNone(stored.final_review_history)
        persisted = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.workspace / "projects" / self.project_id).rglob("*.json")
        )
        self.assertNotIn("must-not-persist", persisted)

    def test_attachment_conflict_retains_hero_artifact_and_domain_receipt(self):
        self._install("hero")
        preview = self._hero_preview()
        entered = threading.Event()
        release = threading.Event()
        results = []
        test = self

        class BlockingHero:
            def capabilities(self):
                return HeroFrameProviderCapabilities(
                    provider="fake-hero",
                    supports_reference_images=True,
                    supports_multiple_object_references=True,
                    max_object_references=10,
                    supported_aspect_ratios=("9:16",),
                    supported_image_sizes=("1K",),
                    supported_output_formats=("PNG",),
                )

            def generate(self, request, options):
                entered.set()
                release.wait(timeout=5)
                artifact = store_validated_hero_frame_artifact(
                    image_bytes("PNG", size=(720, 1280), color=(44, 99, 55)),
                    request,
                    storage_root=test.workspace
                    / "projects"
                    / test.project_id
                    / "artifacts"
                    / "media",
                    created_at=NOW,
                )
                return HeroFrameGenerationResult(
                    status="succeeded",
                    artifact=artifact,
                    receipt=HeroFrameGenerationReceipt(
                        attempt_id="provider-conflict",
                        outcome="succeeded",
                        artifact_id=artifact.id,
                        provider="fake-hero",
                        model="fake-hero-v1",
                        started_at=NOW,
                        completed_at=NOW,
                    ),
                )

        thread = threading.Thread(
            target=lambda: results.append(
                execute_generate_hero(
                    self.workspace,
                    self.project_id,
                    shot_id=self.shot_id,
                    provider="fake-hero",
                    model="fake-hero-v1",
                    options=self.hero_options,
                    generator=BlockingHero(),
                    confirm_preview=preview.id,
                    attempt_id="hero-conflict",
                    now=lambda: NOW,
                )
            )
        )
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        current = load_ad_production_run(self.workspace, self.project_id)
        changed = current.model_copy(update={"latest_operation_diagnostic": None})
        # Force a real, valid revision change without invalidating the action inputs.
        from ad_production_run import RunOperationDiagnostic, record_operation_diagnostic

        changed = record_operation_diagnostic(
            changed,
            RunOperationDiagnostic(
                action="operator-note",
                code="concurrent_change",
                message="Concurrent local change",
                recorded_at=NOW,
            ),
        )
        save_ad_production_run(
            changed,
            self.workspace,
            self.project_id,
            expected_revision=current.revision,
        )
        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].receipt.status, "artifact_created_unattached")
        self.assertTrue((self.workspace / results[0].reference.path).is_file())
        self.assertTrue(
            (
                self.workspace
                / results[0].receipt.domain_receipt_reference.path
            ).is_file()
        )


if __name__ == "__main__":
    unittest.main()
