import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from campaign import AdCampaign
from demo_campaign_storyboard import build_soundcore_campaign_storyboard
from storyboard import (
    Storyboard,
    StoryboardContextError,
    StoryboardShot,
    StoryboardTextOverlay,
    load_storyboard,
    save_storyboard,
    validate_storyboard,
)
from tests.test_storyboard import (
    SELECTED_CLAIMS,
    sample_campaign,
    sample_storyboard,
)


class StoryboardTextOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.campaign = sample_campaign()

    def test_valid_overlay_kinds_preserve_order_and_allow_multiple(self) -> None:
        overlays = [
            StoryboardTextOverlay(kind="creative", text="MADE FOR SUMMER."),
            StoryboardTextOverlay(
                kind="claim",
                claim_id="spec:water_resistance",
            ),
            StoryboardTextOverlay(kind="product_name"),
            StoryboardTextOverlay(kind="cta", text="TAKE IT ANYWHERE."),
        ]
        shot = StoryboardShot(
            id="overlay-shot",
            purpose="feature",
            duration_seconds=8.0,
            selected_claim_ids=["spec:water_resistance"],
            visual_description="Speaker beside the pool.",
            text_overlays=overlays,
        )
        storyboard = Storyboard(
            id="overlay-board",
            campaign_id=self.campaign.id,
            shots=[shot],
        )

        validated = validate_storyboard(storyboard, self.campaign)

        self.assertEqual(
            [overlay.kind for overlay in validated.shots[0].text_overlays],
            ["creative", "claim", "product_name", "cta"],
        )
        self.assertEqual(validated.shots[0].text_overlays, overlays)

    def test_zero_overlays_is_the_backward_compatible_default(self) -> None:
        legacy_shot_json = {
            "id": "legacy-shot",
            "purpose": "hook",
            "duration_seconds": 8.0,
            "selected_claim_ids": [],
            "visual_description": "Legacy core Storyboard shot.",
        }

        restored = StoryboardShot.model_validate(legacy_shot_json)

        self.assertEqual(restored.text_overlays, [])
        self.assertNotIn("text_overlays", legacy_shot_json)

    def test_claim_overlay_requires_only_a_claim_id(self) -> None:
        valid = StoryboardTextOverlay(
            kind="claim",
            claim_id="spec:playback_time",
        )
        self.assertEqual(valid.claim_id, "spec:playback_time")
        self.assertIsNone(valid.text)

        invalid_values = (
            {"kind": "claim"},
            {
                "kind": "claim",
                "claim_id": "spec:playback_time",
                "text": "Up to 20 hours playback",
            },
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    StoryboardTextOverlay.model_validate(value)

    def test_creative_and_cta_require_only_custom_text(self) -> None:
        for kind in ("creative", "cta"):
            valid = StoryboardTextOverlay(kind=kind, text="Original creative copy")
            self.assertEqual(valid.text, "Original creative copy")
            self.assertIsNone(valid.claim_id)
            for value in (
                {"kind": kind},
                {
                    "kind": kind,
                    "text": "Copy",
                    "claim_id": "spec:playback_time",
                },
            ):
                with self.subTest(value=value):
                    with self.assertRaises(ValidationError):
                        StoryboardTextOverlay.model_validate(value)

    def test_product_name_forbids_text_and_claim_id(self) -> None:
        self.assertEqual(
            StoryboardTextOverlay(kind="product_name"),
            StoryboardTextOverlay.model_validate({"kind": "product_name"}),
        )
        for value in (
            {"kind": "product_name", "text": "Select 4 Go"},
            {
                "kind": "product_name",
                "claim_id": "spec:playback_time",
            },
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    StoryboardTextOverlay.model_validate(value)

    def test_claim_overlay_must_be_selected_by_campaign_and_assigned_to_shot(self) -> None:
        valid = sample_storyboard()
        same_campaign_unassigned = StoryboardTextOverlay(
            kind="claim",
            claim_id="spec:playback_time",
        )
        shot = valid.shots[1].model_copy(
            update={"text_overlays": [same_campaign_unassigned]},
            deep=True,
        )
        malformed = valid.model_copy(
            update={"shots": [valid.shots[0], shot, valid.shots[2]]},
            deep=True,
        )
        with self.assertRaisesRegex(StoryboardContextError, "same shot"):
            validate_storyboard(malformed, self.campaign)

        unselected = StoryboardTextOverlay(
            kind="claim",
            claim_id="spec:not-selected",
        )
        shot = valid.shots[0].model_copy(
            update={"text_overlays": [unselected]}, deep=True
        )
        malformed = valid.model_copy(
            update={"shots": [shot, valid.shots[1], valid.shots[2]]},
            deep=True,
        )
        with self.assertRaisesRegex(StoryboardContextError, "Campaign"):
            validate_storyboard(malformed, self.campaign)

    def test_context_rejects_unchecked_overlay_shape_bypass(self) -> None:
        valid = sample_storyboard()
        malformed_overlay = StoryboardTextOverlay(
            kind="claim",
            claim_id="spec:water_resistance",
        ).model_copy(update={"text": "Manual factual rewrite"}, deep=True)
        shot = valid.shots[1].model_copy(
            update={"text_overlays": [malformed_overlay]}, deep=True
        )
        malformed = valid.model_copy(
            update={"shots": [valid.shots[0], shot, valid.shots[2]]},
            deep=True,
        )

        with self.assertRaisesRegex(StoryboardContextError, "valid"):
            validate_storyboard(malformed, self.campaign)

    def test_claim_wording_stays_only_in_unchanged_campaign_snapshot(self) -> None:
        claim_id = "spec:playback_time"
        campaign_data = self.campaign.model_dump()
        for snapshot in campaign_data["claim_snapshots"]:
            if snapshot["claim_id"] == claim_id:
                snapshot["context_text"] = "Playback varies with use conditions."
        campaign = AdCampaign.model_validate(campaign_data)
        before = deepcopy(campaign)
        shot = StoryboardShot(
            id="claim-shot",
            purpose="feature",
            duration_seconds=8.0,
            selected_claim_ids=[claim_id],
            visual_description="Friends enjoy music by the pool.",
            text_overlays=[
                StoryboardTextOverlay(kind="claim", claim_id=claim_id)
            ],
        )
        storyboard = Storyboard(
            id="claim-board",
            campaign_id=campaign.id,
            shots=[shot],
        )

        validated = validate_storyboard(storyboard, campaign)

        serialized_overlay = validated.shots[0].text_overlays[0].model_dump(
            exclude_none=True
        )
        self.assertEqual(serialized_overlay, {"kind": "claim", "claim_id": claim_id})
        self.assertEqual(campaign, before)
        snapshot = next(
            item for item in campaign.claim_snapshots if item.claim_id == claim_id
        )
        self.assertEqual(snapshot.display_text, f"Snapshot for {claim_id}")
        self.assertEqual(
            snapshot.context_text,
            "Playback varies with use conditions.",
        )

    def test_overlay_json_round_trip_is_strict_and_does_not_require_product(self) -> None:
        storyboard = Storyboard(
            id="standalone-document",
            campaign_id=self.campaign.id,
            shots=[
                StoryboardShot(
                    id="standalone-shot",
                    purpose="cta",
                    duration_seconds=8.0,
                    selected_claim_ids=[],
                    visual_description="Product remains centered.",
                    text_overlays=[
                        StoryboardTextOverlay(kind="cta", text="TAKE IT ANYWHERE."),
                        StoryboardTextOverlay(kind="product_name"),
                    ],
                )
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "storyboard.json"
            save_storyboard(storyboard, path)
            restored = load_storyboard(path)
            self.assertEqual(restored, storyboard)

            malformed = json.loads(path.read_text(encoding="utf-8"))
            malformed["shots"][0]["text_overlays"][0]["font"] = "bold"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_storyboard(path)

    def test_soundcore_domain_demo_matches_approved_campaign_and_storyboard(self) -> None:
        campaign, storyboard = build_soundcore_campaign_storyboard()

        self.assertEqual(
            campaign.claim_selection.product_id,
            "soundcore-com-group-products-a31x1021",
        )
        self.assertEqual(
            campaign.claim_selection.selected_claim_ids,
            SELECTED_CLAIMS,
        )
        self.assertEqual(campaign.target_duration_seconds, 8)
        self.assertEqual(campaign.creative_style, "ugc")
        self.assertEqual(campaign.language_tag, "en")
        self.assertEqual(
            [shot.duration_seconds for shot in storyboard.shots],
            [2.5, 2.5, 3.0],
        )
        self.assertEqual(storyboard.shots[0].selected_claim_ids, [])
        self.assertEqual(
            [overlay.kind for overlay in storyboard.shots[2].text_overlays],
            ["claim", "creative", "product_name"],
        )
        self.assertEqual(
            storyboard.shots[2].text_overlays[0].claim_id,
            "spec:playback_time",
        )
        self.assertIsNone(storyboard.shots[2].text_overlays[0].text)


if __name__ == "__main__":
    unittest.main()
