import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from ad_production_run import (
    RunDocumentReference,
    RunProductInput,
    attach_run_reference,
    attach_shot_reference,
    create_ad_production_run,
    set_product_input,
)
from ad_production_status import resolve_ad_production_status
from editing_audio_assets import EditingAudioManifest
from editing_plan import build_editing_plan
from final_ad_review import FinalAdReviewHistory, append_final_ad_review
from hero_frame_artifact import store_validated_hero_frame_artifact
from hero_frame_review import (
    HeroFrameFidelityChecklist,
    HeroFrameReview,
    append_hero_frame_review,
    create_hero_frame_review_history,
)
from shot_video_intent import create_shot_video_intent
from tests.test_ffmpeg_editor import make_inputs, make_source
from tests.test_final_ad_review import (
    final_checklist,
    final_review,
    make_final_artifact,
)
from tests import test_hero_frame_request as hero_frame_fixture
from video_shot_artifact import store_validated_video_shot_artifact
from video_shot_request import VideoMotionIntent, build_video_shot_request
from video_shot_review import build_editing_source_handoff
from tests.test_ffmpeg_editor import approve as approve_video


FIXED_TIME = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)


def passing_hero_checklist() -> HeroFrameFidelityChecklist:
    return HeroFrameFidelityChecklist(
        geometry_and_proportions="pass",
        color_variant_and_materials="pass",
        controls_buttons_and_ports="pass",
        seams_and_panel_boundaries="pass",
        logos_markings_and_labels="pass",
        no_invented_or_missing_construction="pass",
        no_added_advertising_text="pass",
        scene_and_framing_usable="pass",
    )


class AdProductionStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = hero_frame_fixture.HeroFrameRequestTests(
            "test_valid_request_has_only_approved_identity_and_policy_fields"
        )
        cls.fixture.setUp()
        cls.workspace_directory = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.workspace_directory.name)
        cls.product = cls.fixture.product
        cls.campaign = cls.fixture.campaign
        cls.storyboard = cls.fixture.storyboard
        cls.asset_plan = cls.fixture.asset_plan
        cls.original_manifest = cls.fixture.original_manifest
        cls.derived_manifest = cls.fixture.derived_manifest
        cls.audio_manifest = EditingAudioManifest(product_id=cls.product.id)

        cls.hero_artifacts = []
        cls.hero_histories = []
        cls.intents = []
        cls.video_artifacts = []
        cls.video_histories = []
        colors = ("red", "green", "blue")
        for index, shot in enumerate(cls.storyboard.shots):
            hero_request = cls.fixture.build(shot.id)
            hero = store_validated_hero_frame_artifact(
                hero_frame_fixture.image_bytes(
                    "PNG", size=(720, 1280), color=(20 + index, 80, 160)
                ),
                hero_request,
                storage_root=cls.fixture.root / "generated",
                created_at=FIXED_TIME,
            )
            hero_history = create_hero_frame_review_history(hero)
            hero_review = HeroFrameReview(
                review_id=f"hero-review-{index}",
                review_context_id=hero_history.review_context.id,
                reviewer_actor_id="owner:portnoy",
                decision="approved_for_video_start_image",
                reviewed_at=FIXED_TIME,
                checklist=passing_hero_checklist(),
            )
            hero_history = append_hero_frame_review(hero_history, hero_review, hero)
            relevant_claims = tuple(shot.selected_claim_ids[:1])
            intent = create_shot_video_intent(
                product=cls.product,
                campaign=cls.campaign,
                storyboard=cls.storyboard,
                asset_plan=cls.asset_plan,
                shot_id=shot.id,
                requested_generation_duration_seconds=10.0,
                motion=VideoMotionIntent(
                    camera_motion=f"Small deliberate drift for {shot.id}."
                ),
                motion_relevant_claim_ids=relevant_claims,
            )
            request = build_video_shot_request(
                product=cls.product,
                campaign=cls.campaign,
                storyboard=cls.storyboard,
                asset_plan=cls.asset_plan,
                hero_frame_artifact=hero,
                review_history=hero_history,
                shot_id=shot.id,
                requested_generation_duration_seconds=(
                    intent.requested_generation_duration_seconds
                ),
                motion=intent.motion,
                motion_relevant_claim_ids=intent.motion_relevant_claim_ids,
            )
            source_path = cls.fixture.root / f"status-source-{index}.mp4"
            make_source(source_path, colors[index], 300 + index * 100)
            video = store_validated_video_shot_artifact(
                source_path.read_bytes(),
                request,
                storage_root=cls.fixture.root / "generated",
                created_at=FIXED_TIME,
            )
            video_history = approve_video(video, cls.storyboard, index)
            cls.hero_artifacts.append(hero)
            cls.hero_histories.append(hero_history)
            cls.intents.append(intent)
            cls.video_artifacts.append(video)
            cls.video_histories.append(video_history)

        handoffs = [
            build_editing_source_handoff(artifact, cls.storyboard, history)
            for artifact, history in zip(cls.video_artifacts, cls.video_histories)
        ]
        _, selections, overlay_timings = make_inputs(cls.storyboard)
        cls.editing_plan = build_editing_plan(
            cls.product,
            cls.campaign,
            cls.storyboard,
            handoffs,
            selections,
            overlay_timings,
        )
        final_path = cls.fixture.root / "status-final.mp4"
        make_source(final_path, "purple", 700, duration=8)
        cls.editing_artifact = make_final_artifact(
            final_path,
            cls.editing_plan,
            product_id=cls.product.id,
            campaign_id=cls.campaign.id,
            storyboard_id=cls.storyboard.id,
        )
        from final_ad_review import create_final_ad_review_context

        final_context = create_final_ad_review_context(
            artifact=cls.editing_artifact,
            plan=cls.editing_plan,
            product=cls.product,
            campaign=cls.campaign,
            storyboard=cls.storyboard,
            video_artifacts=cls.video_artifacts,
            video_review_histories=cls.video_histories,
        )
        empty_final = FinalAdReviewHistory(review_context=final_context)
        cls.final_history = append_final_ad_review(
            empty_final,
            final_review(final_context),
            cls.editing_artifact,
            cls.editing_plan,
            cls.product,
            cls.campaign,
            cls.storyboard,
            cls.video_artifacts,
            cls.video_histories,
        )
        rejected = final_review(
            final_context,
            decision="rejected_final_campaign_deliverable",
            checklist=final_checklist(overlay_readability="fail"),
            rejection_reasons=("overlay_readability_issue",),
        )
        cls.rejected_final_history = append_final_ad_review(
            empty_final,
            rejected,
            cls.editing_artifact,
            cls.editing_plan,
            cls.product,
            cls.campaign,
            cls.storyboard,
            cls.video_artifacts,
            cls.video_histories,
        )
        cls.references = cls._write_documents()

    @classmethod
    def tearDownClass(cls):
        cls.workspace_directory.cleanup()
        cls.fixture.tearDown()

    @classmethod
    def _write(cls, relative_path, model, identity):
        path = cls.workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True) + "\n",
            encoding="utf-8",
        )
        return RunDocumentReference(id=identity, path=relative_path)

    @classmethod
    def _write_documents(cls):
        refs = {
            "product": cls._write("documents/product.json", cls.product, cls.product.id),
            "campaign": cls._write("documents/campaign.json", cls.campaign, cls.campaign.id),
            "storyboard": cls._write("documents/storyboard.json", cls.storyboard, cls.storyboard.id),
            "original_asset_manifest": cls._write(
                "documents/original-assets.json", cls.original_manifest, cls.product.id
            ),
            "derived_asset_manifest": cls._write(
                "documents/derived-assets.json", cls.derived_manifest, cls.product.id
            ),
            "editing_audio_manifest": cls._write(
                "documents/editing-audio.json", cls.audio_manifest, cls.product.id
            ),
            "asset_plan": cls._write("documents/asset-plan.json", cls.asset_plan, cls.asset_plan.id),
            "editing_plan": cls._write(
                "documents/editing-plan.json", cls.editing_plan, cls.editing_plan.id
            ),
            "editing_artifact": cls._write(
                "documents/editing-artifact.json",
                cls.editing_artifact,
                cls.editing_artifact.id,
            ),
            "final_review_history": cls._write(
                "documents/final-review.json",
                cls.final_history,
                cls.final_history.review_context.id,
            ),
            "rejected_final_review_history": cls._write(
                "documents/rejected-final-review.json",
                cls.rejected_final_history,
                cls.rejected_final_history.review_context.id,
            ),
        }
        refs["shots"] = {}
        for shot, hero, hero_history, intent, video, video_history in zip(
            cls.storyboard.shots,
            cls.hero_artifacts,
            cls.hero_histories,
            cls.intents,
            cls.video_artifacts,
            cls.video_histories,
        ):
            refs["shots"][shot.id] = {
                "hero_frame_artifact": cls._write(
                    f"documents/{shot.id}-hero.json", hero, hero.id
                ),
                "hero_frame_review_history": cls._write(
                    f"documents/{shot.id}-hero-review.json",
                    hero_history,
                    hero_history.review_context.id,
                ),
                "video_intent": cls._write(
                    f"documents/{shot.id}-intent.json", intent, intent.id
                ),
                "video_artifact": cls._write(
                    f"documents/{shot.id}-video.json", video, video.id
                ),
                "video_review_history": cls._write(
                    f"documents/{shot.id}-video-review.json",
                    video_history,
                    video_history.review_context.id,
                ),
            }
        return refs

    def run_through_asset_plan(self):
        run = create_ad_production_run("soundcore-status-ad")
        run = set_product_input(
            run,
            RunProductInput(kind="product_name", value="Example Speaker"),
        )
        for kind in ("product", "campaign"):
            run = attach_run_reference(run, kind, self.references[kind])
        run = attach_run_reference(
            run,
            "storyboard",
            self.references["storyboard"],
            shot_ids=tuple(shot.id for shot in self.storyboard.shots),
        )
        for kind in (
            "original_asset_manifest",
            "derived_asset_manifest",
            "editing_audio_manifest",
            "asset_plan",
        ):
            run = attach_run_reference(run, kind, self.references[kind])
        return run

    def attach_complete_shot(self, run, shot_id):
        for kind in (
            "hero_frame_artifact",
            "hero_frame_review_history",
            "video_intent",
            "video_artifact",
            "video_review_history",
        ):
            run = attach_shot_reference(
                run,
                shot_id,
                kind,
                self.references["shots"][shot_id][kind],
            )
        return run

    def approved_shots_run(self):
        run = self.run_through_asset_plan()
        for shot in self.storyboard.shots:
            run = self.attach_complete_shot(run, shot.id)
        return run

    def complete_run(self, *, final_reference="final_review_history"):
        run = self.approved_shots_run()
        for kind in ("editing_plan", "editing_artifact"):
            run = attach_run_reference(run, kind, self.references[kind])
        return attach_run_reference(
            run,
            "final_review_history",
            self.references[final_reference],
        )

    def state(self, run):
        return resolve_ad_production_status(run, self.workspace)

    def test_top_level_missing_document_states_are_derived_in_order(self):
        run = create_ad_production_run("soundcore-status-ad")
        self.assertEqual(self.state(run).state, "awaiting_product_input")
        run = set_product_input(
            run, RunProductInput(kind="product_name", value="Example Speaker")
        )
        self.assertEqual(self.state(run).state, "awaiting_research")
        run = attach_run_reference(run, "product", self.references["product"])
        self.assertEqual(self.state(run).state, "awaiting_campaign")
        run = attach_run_reference(run, "campaign", self.references["campaign"])
        self.assertEqual(self.state(run).state, "awaiting_storyboard")
        run = attach_run_reference(
            run,
            "storyboard",
            self.references["storyboard"],
            shot_ids=tuple(shot.id for shot in self.storyboard.shots),
        )
        self.assertEqual(self.state(run).state, "awaiting_asset_discovery")
        run = attach_run_reference(
            run,
            "original_asset_manifest",
            self.references["original_asset_manifest"],
        )
        self.assertEqual(self.state(run).state, "awaiting_asset_selection")
        run = attach_run_reference(
            run,
            "derived_asset_manifest",
            self.references["derived_asset_manifest"],
        )
        run = attach_run_reference(run, "asset_plan", self.references["asset_plan"])
        self.assertEqual(self.state(run).state, "shots_in_progress")

    def test_per_shot_states_follow_exact_manual_and_paid_boundaries(self):
        run = self.run_through_asset_plan()
        shot_id = self.storyboard.shots[0].id
        self.assertEqual(self.state(run).shots[0].state, "awaiting_hero_generation")
        run = attach_shot_reference(
            run, shot_id, "hero_frame_artifact", self.references["shots"][shot_id]["hero_frame_artifact"]
        )
        self.assertEqual(self.state(run).shots[0].state, "awaiting_hero_review")
        run = attach_shot_reference(
            run, shot_id, "hero_frame_review_history", self.references["shots"][shot_id]["hero_frame_review_history"]
        )
        self.assertEqual(self.state(run).shots[0].state, "awaiting_motion_intent")
        run = attach_shot_reference(
            run, shot_id, "video_intent", self.references["shots"][shot_id]["video_intent"]
        )
        self.assertEqual(self.state(run).shots[0].state, "awaiting_video_generation")
        run = attach_shot_reference(
            run, shot_id, "video_artifact", self.references["shots"][shot_id]["video_artifact"]
        )
        self.assertEqual(self.state(run).shots[0].state, "awaiting_video_review")
        run = attach_shot_reference(
            run, shot_id, "video_review_history", self.references["shots"][shot_id]["video_review_history"]
        )
        self.assertEqual(self.state(run).shots[0].state, "approved_for_editing")

    def test_edit_render_final_review_and_complete_states_require_current_authority(self):
        run = self.approved_shots_run()
        self.assertEqual(self.state(run).state, "awaiting_editing_plan")
        run = attach_run_reference(run, "editing_plan", self.references["editing_plan"])
        self.assertEqual(self.state(run).state, "ready_to_render")
        run = attach_run_reference(run, "editing_artifact", self.references["editing_artifact"])
        self.assertEqual(self.state(run).state, "awaiting_final_review")
        approved = attach_run_reference(
            run, "final_review_history", self.references["final_review_history"]
        )
        self.assertEqual(self.state(approved).state, "complete")
        rejected = attach_run_reference(
            run,
            "final_review_history",
            self.references["rejected_final_review_history"],
        )
        self.assertEqual(self.state(rejected).state, "awaiting_final_review")

    def test_invalid_references_regress_without_a_failed_state(self):
        run = create_ad_production_run("soundcore-status-ad")
        run = attach_run_reference(run, "product", self.references["product"])
        missing = run.model_copy(
            update={
                "product": RunDocumentReference(
                    id=self.product.id,
                    path="documents/missing-product.json",
                )
            }
        )
        status = self.state(missing)
        self.assertEqual(status.state, "awaiting_research")
        self.assertTrue(status.issues)
        self.assertNotEqual(status.state, "failed")

    def test_invalid_optional_editing_audio_does_not_regress_product_asset_phase(self):
        run = self.run_through_asset_plan().model_copy(
            update={
                "editing_audio_manifest": RunDocumentReference(
                    id=self.product.id,
                    path="documents/missing-editing-audio.json",
                )
            }
        )
        status = self.state(run)
        self.assertEqual(status.state, "shots_in_progress")
        self.assertTrue(
            any(item.scope == "editing_audio_manifest" for item in status.issues)
        )

    def test_stale_media_regresses_to_the_action_that_can_replace_it(self):
        run = self.run_through_asset_plan()
        shot_id = self.storyboard.shots[0].id
        run = attach_shot_reference(
            run, shot_id, "hero_frame_artifact", self.references["shots"][shot_id]["hero_frame_artifact"]
        )
        hero_path = Path(self.hero_artifacts[0].local_path)
        original = hero_path.read_bytes()
        hero_path.write_bytes(b"stale")
        try:
            status = self.state(run)
            self.assertEqual(status.shots[0].state, "awaiting_hero_generation")
            self.assertTrue(status.issues)
        finally:
            hero_path.write_bytes(original)

        complete = self.complete_run()
        final_path = Path(self.editing_artifact.local_path)
        original_final = final_path.read_bytes()
        final_path.write_bytes(b"stale")
        try:
            status = self.state(complete)
            self.assertEqual(status.state, "ready_to_render")
            self.assertTrue(status.issues)
        finally:
            final_path.write_bytes(original_final)

    def test_status_is_deterministic_read_only_and_contains_only_latest_diagnostic(self):
        run = self.complete_run()
        before = deepcopy(run.model_dump())
        first = self.state(run)
        second = self.state(run)
        self.assertEqual(first, second)
        self.assertEqual(run.model_dump(), before)
        self.assertEqual(first.project_id, run.project_id)
        self.assertEqual(first.revision, run.revision)

    def test_workspace_reference_symlink_escape_is_reported_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.json"
            outside.write_text(json.dumps(self.product.model_dump(mode="json")), encoding="utf-8")
            link = self.workspace / "documents" / "escaped-product.json"
            link.symlink_to(outside)
            run = create_ad_production_run("soundcore-status-ad")
            run = attach_run_reference(
                run,
                "product",
                RunDocumentReference(id=self.product.id, path="documents/escaped-product.json"),
            )
            status = self.state(run)
            self.assertEqual(status.state, "awaiting_research")
            self.assertTrue(status.issues)


if __name__ == "__main__":
    unittest.main()
