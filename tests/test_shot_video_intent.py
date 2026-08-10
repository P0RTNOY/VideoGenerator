import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from demo_campaign_storyboard import build_soundcore_campaign_storyboard
from product import Product
from shot_video_intent import (
    ShotVideoIntent,
    ShotVideoIntentContextError,
    create_shot_video_intent,
    load_shot_video_intent,
    save_shot_video_intent,
    validate_shot_video_intent,
)
from storyboard_asset_plan import (
    SelectedAssetSnapshot,
    ShotAssetSelection,
    StoryboardAssetPlan,
)
from video_shot_request import VideoMotionIntent


class ShotVideoIntentTests(unittest.TestCase):
    def setUp(self):
        self.product = Product(
            id="soundcore-com-group-products-a31x1021",
            name="Select 4 Go",
            brand="Soundcore",
            category="Portable Bluetooth Speaker",
        )
        self.campaign, self.storyboard = build_soundcore_campaign_storyboard()
        self.asset_plan = StoryboardAssetPlan(
            id="soundcore-select-4-go-summer-assets",
            product_id=self.product.id,
            campaign_id=self.campaign.id,
            storyboard_id=self.storyboard.id,
            shot_selections=[
                ShotAssetSelection(
                    shot_id="water-feature",
                    primary_asset=SelectedAssetSnapshot(
                        asset_id="original:manual-reference",
                        asset_kind="original_image",
                        sha256_at_selection="1" * 64,
                        width_at_selection=2000,
                        height_at_selection=2000,
                        format_at_selection="WEBP",
                    ),
                )
            ],
        )
        self.motion = VideoMotionIntent(
            subject_motion="Subtle natural hand movement.",
            environment_motion="Gentle swimming-pool water movement.",
            camera_motion="Small natural handheld smartphone drift.",
            motion_constraints="Keep the speaker orientation nearly unchanged.",
        )

    def create(self, **updates):
        values = {
            "product": self.product,
            "campaign": self.campaign,
            "storyboard": self.storyboard,
            "asset_plan": self.asset_plan,
            "shot_id": "water-feature",
            "requested_generation_duration_seconds": 8.0,
            "motion": self.motion,
            "motion_relevant_claim_ids": (
                "spec:water_resistance",
                "feature:floating-design",
            ),
        }
        values.update(updates)
        return create_shot_video_intent(**values)

    def test_soundcore_intent_is_deterministic_complete_and_path_free(self):
        intent = self.create()

        self.assertEqual(intent, self.create())
        self.assertTrue(intent.id.startswith("shot-video-intent:"))
        self.assertEqual(intent.product_id, self.product.id)
        self.assertEqual(intent.campaign_id, self.campaign.id)
        self.assertEqual(intent.storyboard_id, self.storyboard.id)
        self.assertEqual(intent.asset_plan_id, self.asset_plan.id)
        self.assertEqual(intent.shot_id, "water-feature")
        self.assertEqual(intent.requested_generation_duration_seconds, 8.0)
        self.assertEqual(intent.motion, self.motion)
        self.assertEqual(
            intent.motion_relevant_claim_ids,
            ("spec:water_resistance", "feature:floating-design"),
        )
        serialized = intent.model_dump_json()
        for forbidden in (
            "local_path",
            "provider",
            "model_id",
            "artifact",
            "review",
            "prompt",
            "created_at",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_every_authored_value_changes_content_identity(self):
        baseline = self.create()
        variants = (
            self.create(
                motion=VideoMotionIntent(camera_motion="Locked-off camera.")
            ),
            self.create(requested_generation_duration_seconds=6.0),
            self.create(motion_relevant_claim_ids=("feature:floating-design",)),
            self.create(
                motion_relevant_claim_ids=(
                    "feature:floating-design",
                    "spec:water_resistance",
                )
            ),
            self.create(
                asset_plan=self.asset_plan.model_copy(update={"id": "new-assets"})
            ),
        )
        self.assertTrue(all(item.id != baseline.id for item in variants))

    def test_exact_domain_and_asset_plan_bindings_are_required(self):
        cases = (
            {"product": self.product.model_copy(update={"id": "other"})},
            {"campaign": self.campaign.model_copy(update={"id": "other"})},
            {"storyboard": self.storyboard.model_copy(update={"id": "other"})},
            {
                "asset_plan": self.asset_plan.model_copy(
                    update={"storyboard_id": "other"}
                )
            },
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ShotVideoIntentContextError):
                    self.create(**changes)

    def test_shot_must_exist_and_have_an_asset_selection(self):
        with self.assertRaises(ShotVideoIntentContextError):
            self.create(shot_id="unknown")
        empty_plan = self.asset_plan.model_copy(update={"shot_selections": []})
        with self.assertRaises(ShotVideoIntentContextError):
            self.create(asset_plan=empty_plan)

    def test_motion_claim_ids_must_be_unique_selected_and_snapshot_backed(self):
        invalid_sets = (
            ("feature:floating-design", "feature:floating-design"),
            ("spec:playback_time",),
            ("claim:not-selected",),
            ("",),
        )
        for claim_ids in invalid_sets:
            with self.subTest(claim_ids=claim_ids):
                with self.assertRaises(ShotVideoIntentContextError):
                    self.create(motion_relevant_claim_ids=claim_ids)

    def test_generation_duration_and_motion_are_strict(self):
        for invalid in (8, True, 0.0, -1.0, float("inf"), float("nan")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ShotVideoIntentContextError):
                    self.create(requested_generation_duration_seconds=invalid)
        with self.assertRaises(ValidationError):
            VideoMotionIntent()

    def test_identity_and_nested_motion_are_immutable_and_revalidated(self):
        intent = self.create()
        with self.assertRaises(ValidationError):
            intent.motion.camera_motion = "Changed later"
        with self.assertRaises(ValidationError):
            ShotVideoIntent.model_validate(
                {**intent.model_dump(), "id": "shot-video-intent:" + "f" * 64}
            )
        bypassed = intent.model_copy(update={"schema_version": 2})
        with self.assertRaises(ShotVideoIntentContextError):
            validate_shot_video_intent(
                bypassed,
                self.product,
                self.campaign,
                self.storyboard,
                self.asset_plan,
            )

    def test_json_persistence_is_human_readable_atomic_and_strict(self):
        intent = self.create()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            saved = save_shot_video_intent(intent, path)
            self.assertEqual(saved, path.resolve())
            self.assertEqual(load_shot_video_intent(path), intent)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("\n"))
            self.assertIn("\n  \"schema_version\"", text)
            data = json.loads(text)
            data["provider"] = "veo"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_shot_video_intent(path)

    def test_builder_and_validator_do_not_mutate_inputs(self):
        objects = (self.product, self.campaign, self.storyboard, self.asset_plan, self.motion)
        before = deepcopy([item.model_dump() for item in objects])
        intent = self.create()
        validated = validate_shot_video_intent(
            intent,
            self.product,
            self.campaign,
            self.storyboard,
            self.asset_plan,
        )
        self.assertEqual(validated, intent)
        self.assertIsNot(validated, intent)
        self.assertEqual(before, [item.model_dump() for item in objects])


if __name__ == "__main__":
    unittest.main()
