import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from ad_claims import ProductAdClaimSelection
from campaign import AdCampaign, CampaignClaimSnapshot
from storyboard import (
    Storyboard,
    StoryboardContextError,
    StoryboardShot,
    load_storyboard,
    save_storyboard,
    validate_storyboard,
)


SELECTED_CLAIMS = [
    "spec:water_resistance",
    "spec:playback_time",
    "feature:floating-design",
]


def sample_campaign() -> AdCampaign:
    return AdCampaign(
        id="soundcore-summer",
        claim_selection=ProductAdClaimSelection(
            product_id="soundcore-speaker",
            selected_claim_ids=SELECTED_CLAIMS,
        ),
        ad_format="vertical_social",
        aspect_ratio="9:16",
        target_duration_seconds=8,
        creative_style="ugc",
        environment="Sunny poolside",
        language_tag="en",
        claim_snapshots=[
            CampaignClaimSnapshot(
                claim_id=claim_id,
                display_text=f"Snapshot for {claim_id}",
                verification_status_at_selection="supported",
            )
            for claim_id in SELECTED_CLAIMS
        ],
    )


def sample_storyboard(**overrides: object) -> Storyboard:
    values = {
        "id": "soundcore-summer-board",
        "campaign_id": "soundcore-summer",
        "shots": [
            StoryboardShot(
                id="hook",
                purpose="hook",
                duration_seconds=2.5,
                selected_claim_ids=[],
                visual_description="Speaker arrives beside a sunny pool.",
                camera_direction="Natural handheld medium close-up.",
            ),
            StoryboardShot(
                id="feature",
                purpose="feature",
                duration_seconds=2.5,
                selected_claim_ids=[
                    "spec:water_resistance",
                    "feature:floating-design",
                ],
                visual_description="Speaker floats beside the swimmer.",
            ),
            StoryboardShot(
                id="lifestyle",
                purpose="lifestyle",
                duration_seconds=3.0,
                selected_claim_ids=["spec:playback_time"],
                visual_description="Friends listen beside the pool.",
                camera_direction="Slow controlled push-in.",
            ),
        ],
    }
    values.update(overrides)
    return Storyboard.model_validate(values)


class StoryboardDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.campaign = sample_campaign()

    def test_valid_storyboard_preserves_shot_and_claim_order(self) -> None:
        proposed = sample_storyboard()

        validated = validate_storyboard(proposed, self.campaign)

        self.assertEqual(validated, proposed)
        self.assertIsNot(validated, proposed)
        self.assertEqual(
            [shot.id for shot in validated.shots],
            ["hook", "feature", "lifestyle"],
        )
        self.assertEqual(
            validated.shots[1].selected_claim_ids,
            ["spec:water_resistance", "feature:floating-design"],
        )
        self.assertEqual(validated.shots[0].selected_claim_ids, [])
        self.assertEqual(
            validated.shots[0].camera_direction,
            "Natural handheld medium close-up.",
        )

    def test_storyboard_must_match_campaign_id(self) -> None:
        proposed = sample_storyboard(campaign_id="another-campaign")

        with self.assertRaisesRegex(StoryboardContextError, "campaign"):
            validate_storyboard(proposed, self.campaign)

    def test_storyboard_requires_shots_with_unique_ids_and_no_order_field(self) -> None:
        valid = sample_storyboard().model_dump()
        with self.assertRaises(ValidationError):
            Storyboard.model_validate({**valid, "shots": []})

        duplicate = deepcopy(valid)
        duplicate["shots"][1]["id"] = duplicate["shots"][0]["id"]
        with self.assertRaises(ValidationError):
            Storyboard.model_validate(duplicate)

        extra_order = deepcopy(valid)
        extra_order["shots"][0]["order"] = 1
        with self.assertRaises(ValidationError):
            Storyboard.model_validate(extra_order)

    def test_shot_purpose_and_text_fields_are_strict(self) -> None:
        valid = sample_storyboard().shots[0].model_dump()
        for purpose in ("hook", "feature", "lifestyle", "cta"):
            self.assertEqual(
                StoryboardShot.model_validate(
                    {**valid, "purpose": purpose}
                ).purpose,
                purpose,
            )
        with self.assertRaises(ValidationError):
            StoryboardShot.model_validate({**valid, "purpose": "demo"})
        with self.assertRaises(ValidationError):
            StoryboardShot.model_validate(
                {**valid, "visual_description": "   "}
            )
        with self.assertRaises(ValidationError):
            StoryboardShot.model_validate({**valid, "camera_direction": " "})

    def test_shot_claims_must_be_unique_and_selected_by_campaign(self) -> None:
        valid = sample_storyboard()
        duplicate_shot = valid.shots[1].model_copy(
            update={
                "selected_claim_ids": [
                    "spec:water_resistance",
                    "spec:water_resistance",
                ]
            },
            deep=True,
        )
        malformed = valid.model_copy(
            update={"shots": [valid.shots[0], duplicate_shot, valid.shots[2]]},
            deep=True,
        )
        with self.assertRaisesRegex(StoryboardContextError, "duplicate"):
            validate_storyboard(malformed, self.campaign)

        unselected_shot = valid.shots[1].model_copy(
            update={"selected_claim_ids": ["feature:not-selected"]},
            deep=True,
        )
        malformed = valid.model_copy(
            update={"shots": [valid.shots[0], unselected_shot, valid.shots[2]]},
            deep=True,
        )
        with self.assertRaisesRegex(StoryboardContextError, "not selected"):
            validate_storyboard(malformed, self.campaign)

    def test_shot_duration_is_a_strict_finite_positive_float(self) -> None:
        valid = sample_storyboard().shots[0].model_dump()
        self.assertEqual(
            StoryboardShot.model_validate(
                {**valid, "duration_seconds": 0.1}
            ).duration_seconds,
            0.1,
        )
        for duration in (0.0, -1.0, math.nan, math.inf, -math.inf, 2, True):
            with self.subTest(duration=duration):
                with self.assertRaises(ValidationError):
                    StoryboardShot.model_validate(
                        {**valid, "duration_seconds": duration}
                    )

    def test_context_rechecks_required_shots_and_safe_durations(self) -> None:
        valid = sample_storyboard()
        empty = valid.model_copy(update={"shots": []}, deep=True)
        with self.assertRaisesRegex(StoryboardContextError, "shot"):
            validate_storyboard(empty, self.campaign)

        for duration in (0.0, -1.0, math.nan, math.inf, 2, True):
            invalid_shot = valid.shots[0].model_copy(
                update={"duration_seconds": duration}, deep=True
            )
            malformed = valid.model_copy(
                update={
                    "shots": [
                        invalid_shot,
                        valid.shots[1],
                        valid.shots[2],
                    ]
                },
                deep=True,
            )
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(StoryboardContextError, "duration"):
                    validate_storyboard(malformed, self.campaign)

    def test_total_duration_accepts_exact_and_inclusive_tolerance_boundaries(self) -> None:
        for duration in (7.5, 8.0, 8.5):
            storyboard = Storyboard(
                id="duration-board",
                campaign_id=self.campaign.id,
                shots=[
                    StoryboardShot(
                        id="only-shot",
                        purpose="hook",
                        duration_seconds=duration,
                        selected_claim_ids=[],
                        visual_description="One continuous poolside shot.",
                    )
                ],
            )
            with self.subTest(duration=duration):
                self.assertEqual(
                    validate_storyboard(storyboard, self.campaign),
                    storyboard,
                )

    def test_total_duration_outside_tolerance_is_rejected(self) -> None:
        for duration in (7.49, 8.51):
            storyboard = Storyboard(
                id="duration-board",
                campaign_id=self.campaign.id,
                shots=[
                    StoryboardShot(
                        id="only-shot",
                        purpose="hook",
                        duration_seconds=duration,
                        selected_claim_ids=[],
                        visual_description="One continuous poolside shot.",
                    )
                ],
            )
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(StoryboardContextError, "duration"):
                    validate_storyboard(storyboard, self.campaign)

    def test_storyboard_json_round_trip_is_readable_and_strict(self) -> None:
        original = sample_storyboard()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "storyboard.json"
            returned_path = save_storyboard(original, path)
            raw = path.read_text(encoding="utf-8")

            self.assertEqual(returned_path, path)
            self.assertIn("\n  \"schema_version\"", raw)
            self.assertEqual(load_storyboard(path), original)

            for malformed in (
                {**json.loads(raw), "provider": "video-service"},
                {**original.model_dump(), "schema_version": 2},
                {
                    **original.model_dump(),
                    "shots": [
                        {**original.shots[0].model_dump(), "prompt": "Generate"}
                    ],
                },
            ):
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.subTest(malformed=malformed):
                    with self.assertRaises(ValidationError):
                        load_storyboard(path)

    def test_validation_does_not_mutate_storyboard_campaign_or_selection(self) -> None:
        proposed = sample_storyboard()
        before = deepcopy((proposed, self.campaign))

        validated = validate_storyboard(proposed, self.campaign)

        self.assertEqual((proposed, self.campaign), before)
        validated.shots[0].visual_description = "Changed result"
        self.assertNotEqual(
            validated.shots[0].visual_description,
            proposed.shots[0].visual_description,
        )
        restored_campaign = AdCampaign.model_validate_json(
            self.campaign.model_dump_json()
        )
        self.assertEqual(restored_campaign, self.campaign)
        self.assertEqual(
            restored_campaign.claim_selection,
            self.campaign.claim_selection,
        )


if __name__ == "__main__":
    unittest.main()
