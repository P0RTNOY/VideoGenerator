import hashlib
import io
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from demo_campaign_storyboard import build_soundcore_campaign_storyboard
from hero_frame_artifact import HeroFrameArtifact
from hero_frame_review import (
    HeroFrameFidelityChecklist,
    HeroFrameReview,
    HeroFrameReviewHistory,
    append_hero_frame_review,
    create_hero_frame_review_history,
)
from product import Product
from storyboard_asset_plan import (
    SelectedAssetSnapshot,
    ShotAssetSelection,
    StoryboardAssetPlan,
)
from video_shot_request import (
    VideoMotionIntent,
    VideoShotRequest,
    VideoShotRequestContextError,
    VideoStartImageIntegrityError,
    build_video_shot_request,
    read_verified_video_start_image_bytes,
)


FIXED_TIME = datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc)


def png_bytes(color=(20, 100, 180)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (768, 1376), color).save(output, format="PNG")
    return output.getvalue()


def passing_checklist() -> HeroFrameFidelityChecklist:
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


class VideoShotRequestTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.product = Product(
            id="soundcore-com-group-products-a31x1021",
            name="Select 4 Go",
            brand="Soundcore",
            category="Portable Bluetooth Speaker",
        )
        self.campaign, self.storyboard = build_soundcore_campaign_storyboard()
        primary = SelectedAssetSnapshot(
            asset_id="original:manual-reference",
            asset_kind="original_image",
            sha256_at_selection="1" * 64,
            width_at_selection=2000,
            height_at_selection=2000,
            format_at_selection="WEBP",
        )
        supporting = SelectedAssetSnapshot(
            asset_id="reference-sheet:" + "2" * 64,
            asset_kind="reference_sheet",
            sha256_at_selection="2" * 64,
            width_at_selection=2048,
            height_at_selection=2048,
            format_at_selection="PNG",
        )
        self.asset_plan = StoryboardAssetPlan(
            id="soundcore-select-4-go-summer-assets",
            product_id=self.product.id,
            campaign_id=self.campaign.id,
            storyboard_id=self.storyboard.id,
            shot_selections=[
                ShotAssetSelection(
                    shot_id="water-feature",
                    primary_asset=primary,
                    supporting_assets=[supporting],
                )
            ],
        )
        data = png_bytes()
        path = self.root / "approved-hero.png"
        path.write_bytes(data)
        sha256 = hashlib.sha256(data).hexdigest()
        self.artifact = HeroFrameArtifact(
            id=f"hero-frame:{sha256}",
            product_id=self.product.id,
            campaign_id=self.campaign.id,
            storyboard_id=self.storyboard.id,
            asset_plan_id=self.asset_plan.id,
            shot_id="water-feature",
            source_request_fingerprint="a" * 64,
            local_path=str(path),
            sha256=sha256,
            width=768,
            height=1376,
            format="PNG",
            created_at=FIXED_TIME,
        )
        self.history = create_hero_frame_review_history(self.artifact)
        self.motion = VideoMotionIntent(
            subject_motion=(
                "Very subtle natural hand movement while keeping the speaker "
                "orientation nearly unchanged."
            ),
            environment_motion="Gentle swimming-pool water movement.",
            camera_motion="Small natural handheld smartphone drift.",
            motion_constraints=(
                "Do not rotate the Product or reveal substantially new surfaces."
            ),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def approve(self, review_id="review-approved"):
        item = HeroFrameReview(
            review_id=review_id,
            review_context_id=self.history.review_context.id,
            reviewer_actor_id="owner:portnoy",
            decision="approved_for_video_start_image",
            reviewed_at=FIXED_TIME,
            checklist=passing_checklist(),
        )
        self.history = append_hero_frame_review(
            self.history,
            item,
            self.artifact,
        )
        return item

    def reject(self, review_id="review-rejected"):
        item = HeroFrameReview(
            review_id=review_id,
            review_context_id=self.history.review_context.id,
            reviewer_actor_id="owner:portnoy",
            decision="rejected_for_video_start_image",
            reviewed_at=FIXED_TIME,
            checklist=passing_checklist().model_copy(
                update={"geometry_and_proportions": "fail"}
            ),
            rejection_reasons=("geometry_mismatch",),
        )
        self.history = append_hero_frame_review(
            self.history,
            item,
            self.artifact,
        )
        return item

    def build(self, **updates):
        values = {
            "product": self.product,
            "campaign": self.campaign,
            "storyboard": self.storyboard,
            "asset_plan": self.asset_plan,
            "hero_frame_artifact": self.artifact,
            "review_history": self.history,
            "shot_id": "water-feature",
            "requested_generation_duration_seconds": 8.0,
            "motion": self.motion,
            "motion_relevant_claim_ids": (),
        }
        values.update(updates)
        return build_video_shot_request(**values)

    def test_valid_request_contains_only_approved_start_state_and_fixed_policies(self):
        approval = self.approve()

        request = self.build()

        self.assertEqual(request.product_id, self.product.id)
        self.assertEqual(request.campaign_id, self.campaign.id)
        self.assertEqual(request.storyboard_id, self.storyboard.id)
        self.assertEqual(request.asset_plan_id, self.asset_plan.id)
        self.assertEqual(request.shot_id, "water-feature")
        self.assertEqual(request.hero_frame_artifact_id, self.artifact.id)
        self.assertEqual(request.hero_frame_sha256, self.artifact.sha256)
        self.assertEqual(
            request.hero_frame_request_fingerprint,
            self.artifact.source_request_fingerprint,
        )
        self.assertEqual(
            request.authorizing_review_context_id,
            self.history.review_context.id,
        )
        self.assertEqual(request.authorizing_review_id, approval.review_id)
        self.assertEqual(request.start_image.local_path, Path(self.artifact.local_path))
        self.assertEqual(request.intended_edit_duration_seconds, 2.5)
        self.assertEqual(request.requested_generation_duration_seconds, 8.0)
        self.assertEqual(request.motion, self.motion)
        self.assertEqual(request.motion_relevant_claims, [])
        self.assertEqual(request.animation_fidelity, "animate_approved_hero_frame")
        self.assertEqual(request.aspect_ratio, "9:16")
        self.assertEqual(request.added_text_policy, "no_added_text")
        self.assertEqual(
            request.provider_audio_disposition,
            "discard_before_editing",
        )
        serialized = request.model_dump_json()
        for forbidden in (
            "original:manual-reference",
            "reference-sheet",
            "primary_asset",
            "supporting_assets",
            "text_overlays",
            "visual_description",
            "provider_prompt",
            "model_id",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_pending_and_rejected_review_states_fail_closed(self):
        with self.assertRaisesRegex(VideoShotRequestContextError, "pending"):
            self.build()

        self.reject()
        with self.assertRaisesRegex(VideoShotRequestContextError, "rejected"):
            self.build()

    def test_latest_exact_approval_is_authoritative(self):
        self.reject("first-rejection")
        approval = self.approve("later-approval")

        request = self.build()

        self.assertEqual(request.authorizing_review_id, approval.review_id)

    def test_stale_bytes_and_different_review_context_fail(self):
        self.approve()
        Path(self.artifact.local_path).write_bytes(b"changed")
        with self.assertRaises(VideoShotRequestContextError):
            self.build()

        replacement = png_bytes((1, 2, 3))
        Path(self.artifact.local_path).write_bytes(replacement)
        changed_sha = hashlib.sha256(replacement).hexdigest()
        changed = self.artifact.model_copy(
            update={"id": f"hero-frame:{changed_sha}", "sha256": changed_sha},
            deep=True,
        )
        with self.assertRaisesRegex(VideoShotRequestContextError, "pending"):
            self.build(hero_frame_artifact=changed)

    def test_consumer_rechecks_exact_start_image_bytes(self):
        self.approve()
        request = self.build()
        expected = Path(self.artifact.local_path).read_bytes()
        self.assertEqual(read_verified_video_start_image_bytes(request.start_image), expected)

        Path(self.artifact.local_path).write_bytes(b"changed after construction")
        with self.assertRaises(VideoStartImageIntegrityError):
            read_verified_video_start_image_bytes(request.start_image)

    def test_explicit_motion_claim_subset_resolves_exact_campaign_snapshot(self):
        self.approve()
        request = self.build(
            motion_relevant_claim_ids=("feature:floating-design",)
        )
        expected = next(
            item
            for item in self.campaign.claim_snapshots
            if item.claim_id == "feature:floating-design"
        )

        self.assertEqual(request.motion_relevant_claims, [expected])
        self.assertEqual(
            request.motion_relevant_claims[0].verification_status_at_selection,
            expected.verification_status_at_selection,
        )
        self.assertEqual(
            request.motion_relevant_claims[0].context_text,
            expected.context_text,
        )

    def test_non_shot_unselected_duplicate_and_blank_motion_claims_fail(self):
        self.approve()
        invalid_sets = (
            ("spec:playback_time",),
            ("claim:not-selected",),
            ("feature:floating-design", "feature:floating-design"),
            ("",),
        )
        for claim_ids in invalid_sets:
            with self.subTest(claim_ids=claim_ids):
                with self.assertRaises(VideoShotRequestContextError):
                    self.build(motion_relevant_claim_ids=claim_ids)

    def test_motion_is_explicit_and_never_inferred_from_storyboard(self):
        self.approve()
        request = self.build(
            motion=VideoMotionIntent(camera_motion="Locked-off camera."),
        )
        self.assertEqual(request.motion.camera_motion, "Locked-off camera.")
        self.assertIsNone(request.motion.subject_motion)
        self.assertNotIn(
            "Speaker floats at the pool surface.",
            request.model_dump_json(),
        )
        with self.assertRaises(ValidationError):
            VideoMotionIntent()
        with self.assertRaises(ValidationError):
            VideoMotionIntent(camera_motion="   ")

    def test_duration_is_strict_and_storyboard_is_not_rewritten(self):
        self.approve()
        before = self.storyboard.model_dump()
        request = self.build(requested_generation_duration_seconds=6.0)

        self.assertEqual(request.intended_edit_duration_seconds, 2.5)
        self.assertEqual(request.requested_generation_duration_seconds, 6.0)
        self.assertEqual(self.storyboard.model_dump(), before)
        for invalid in (8, True, 0.0, float("inf"), float("nan")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(VideoShotRequestContextError):
                    self.build(requested_generation_duration_seconds=invalid)

    def test_all_context_bindings_must_match_exactly(self):
        self.approve()
        cases = (
            {"product": self.product.model_copy(update={"id": "other"}, deep=True)},
            {"campaign": self.campaign.model_copy(update={"id": "other"}, deep=True)},
            {"storyboard": self.storyboard.model_copy(update={"id": "other"}, deep=True)},
            {"asset_plan": self.asset_plan.model_copy(update={"id": "other"}, deep=True)},
            {"shot_id": "hook"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(VideoShotRequestContextError):
                    self.build(**changes)

    def test_unprepared_shot_cannot_reuse_an_approved_artifact(self):
        self.approve()
        plan = self.asset_plan.model_copy(update={"shot_selections": []}, deep=True)
        with self.assertRaisesRegex(VideoShotRequestContextError, "prepared"):
            self.build(asset_plan=plan)

    def test_builder_revalidates_bypassed_typed_inputs(self):
        self.approve()
        bypassed = self.history.model_copy(update={"schema_version": 2}, deep=True)
        with self.assertRaises(VideoShotRequestContextError):
            self.build(review_history=bypassed)

    def test_request_and_motion_models_are_structurally_strict(self):
        self.approve()
        request = self.build()
        data = request.model_dump()
        data["provider"] = "veo"
        with self.assertRaises(ValidationError):
            VideoShotRequest.model_validate(data)
        motion = self.motion.model_dump()
        motion["product_spec"] = "IP67"
        with self.assertRaises(ValidationError):
            VideoMotionIntent.model_validate(motion)

    def test_builder_does_not_mutate_any_durable_input(self):
        self.approve()
        inputs = (
            self.product,
            self.campaign,
            self.storyboard,
            self.asset_plan,
            self.artifact,
            self.history,
        )
        before = tuple(deepcopy(item.model_dump()) for item in inputs)

        request = self.build(
            motion_relevant_claim_ids=("feature:floating-design",)
        )
        request.motion_relevant_claims[0].display_text = "mutated copy"

        self.assertEqual(
            tuple(item.model_dump() for item in inputs),
            before,
        )

    def test_request_has_no_persistence_or_provider_helpers(self):
        import video_shot_request

        names = set(dir(video_shot_request))
        self.assertFalse(
            names
            & {
                "save_video_shot_request",
                "load_video_shot_request",
                "render_prompt",
                "generate_video",
            }
        )


if __name__ == "__main__":
    unittest.main()
