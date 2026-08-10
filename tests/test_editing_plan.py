import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from ad_claims import ProductAdClaimSelection
from campaign import AdCampaign, CampaignClaimSnapshot
from editing_audio_assets import (
    EditingAudioManifest,
    add_editing_audio_asset,
    ingest_editing_audio_asset,
)
from editing_plan import (
    AudioMixPolicy,
    EditingMusicSelection,
    EditingOutputSpec,
    EditingOverlayTiming,
    EditingPlan,
    EditingPlanError,
    EditingSegmentSelection,
    EditingSfxSelection,
    build_editing_plan,
    load_editing_plan,
    save_editing_plan,
)
from product import Product
from storyboard import Storyboard, StoryboardShot, StoryboardTextOverlay
from video_shot_review import (
    EditingRuntimeVideo,
    EditingSourceHandoff,
    EditingTextOverlay,
    UsableVideoRange,
)


def make_product() -> Product:
    return Product(
        id="product-1",
        name="Select 4 Go",
        brand="Soundcore",
        category="Portable Bluetooth Speaker",
    )


def make_campaign(*, target=8, product_id="product-1") -> AdCampaign:
    return AdCampaign(
        id="campaign-1",
        claim_selection=ProductAdClaimSelection(
            product_id=product_id,
            selected_claim_ids=["spec:battery"],
        ),
        ad_format="vertical_social",
        aspect_ratio="9:16",
        target_duration_seconds=target,
        creative_style="ugc",
        environment="Poolside",
        language_tag="en",
        claim_snapshots=[
            CampaignClaimSnapshot(
                claim_id="spec:battery",
                display_text="Up to 20 hours of playback",
                verification_status_at_selection="supported",
            )
        ],
    )


def make_storyboard(*, campaign_id="campaign-1", durations=(2.5, 2.5, 3.0)):
    return Storyboard(
        id="storyboard-1",
        campaign_id=campaign_id,
        shots=[
            StoryboardShot(
                id="hook",
                purpose="hook",
                duration_seconds=durations[0],
                visual_description="Reveal the speaker.",
                text_overlays=[
                    StoryboardTextOverlay(kind="creative", text="BIG ENERGY")
                ],
            ),
            StoryboardShot(
                id="feature",
                purpose="feature",
                duration_seconds=durations[1],
                selected_claim_ids=["spec:battery"],
                visual_description="Show the product.",
                text_overlays=[
                    StoryboardTextOverlay(
                        kind="claim", claim_id="spec:battery"
                    )
                ],
            ),
            StoryboardShot(
                id="cta",
                purpose="cta",
                duration_seconds=durations[2],
                visual_description="End card.",
                text_overlays=[
                    StoryboardTextOverlay(kind="product_name"),
                    StoryboardTextOverlay(kind="cta", text="TAKE IT ANYWHERE"),
                ],
            ),
        ],
    )


def make_handoff(shot, index, *, product_id="product-1"):
    duration_ms = int(shot.duration_seconds * 1000)
    sha = f"{index + 1:064x}"
    return EditingSourceHandoff(
        product_id=product_id,
        campaign_id="campaign-1",
        storyboard_id="storyboard-1",
        asset_plan_id="asset-plan-1",
        shot_id=shot.id,
        video_artifact_id=f"video-shot:{sha}",
        video_artifact_sha256=sha,
        authorizing_video_review_context_id=f"video-review-context:{sha}",
        authorizing_video_review_id=f"video-review-{index}",
        runtime_video=EditingRuntimeVideo(
            artifact_id=f"video-shot:{sha}",
            local_path=Path(f"/runtime/source-{index}.mp4"),
            sha256=sha,
            raw_duration_ms=10000,
            width=720,
            height=1280,
            container="mp4",
            video_codec="h264",
            has_audio=True,
        ),
        approved_usable_ranges=(UsableVideoRange(start_ms=500, end_ms=9500),),
        intended_edit_duration_ms=duration_ms,
        text_overlays=tuple(
            EditingTextOverlay.model_validate(item.model_dump())
            for item in shot.text_overlays
        ),
        provider_audio_disposition="discard_before_editing",
    )


def make_inputs(storyboard):
    handoffs = [make_handoff(shot, index) for index, shot in enumerate(storyboard.shots)]
    segments = [
        EditingSegmentSelection(shot_id=shot.id, source_start_ms=1000)
        for shot in storyboard.shots
    ]
    timings = []
    timeline_start = 0
    for shot in storyboard.shots:
        duration_ms = int(shot.duration_seconds * 1000)
        for overlay_index, _ in enumerate(shot.text_overlays):
            timings.append(
                EditingOverlayTiming(
                    shot_id=shot.id,
                    overlay_index=overlay_index,
                    timeline_start_ms=timeline_start + 100,
                    timeline_end_ms=timeline_start + duration_ms - 100,
                    placement="bottom_center",
                )
            )
        timeline_start += duration_ms
    return handoffs, segments, timings


class EditingPlanTests(unittest.TestCase):
    def setUp(self):
        self.product = make_product()
        self.campaign = make_campaign()
        self.storyboard = make_storyboard()
        self.handoffs, self.selections, self.timings = make_inputs(self.storyboard)

    def build(self, **updates):
        values = {
            "product": self.product,
            "campaign": self.campaign,
            "storyboard": self.storyboard,
            "source_handoffs": self.handoffs,
            "segment_selections": self.selections,
            "overlay_timings": self.timings,
        }
        values.update(updates)
        return build_editing_plan(**values)

    def test_valid_plan_is_complete_deterministic_path_free_and_soundcore_shaped(self):
        plan = self.build()
        again = self.build()

        self.assertEqual(plan, again)
        self.assertTrue(plan.id.startswith("editing-plan:"))
        self.assertEqual([item.shot_id for item in plan.segments], ["hook", "feature", "cta"])
        self.assertEqual(plan.target_duration_ms, 8000)
        self.assertEqual(plan.segments[-1].timeline_end_ms, 8000)
        self.assertEqual(len(plan.overlays), 4)
        serialized = plan.model_dump_json()
        self.assertNotIn("local_path", serialized)
        self.assertNotIn("/runtime/", serialized)
        self.assertNotIn("ffmpeg", serialized.lower())

    def test_plan_identity_changes_with_every_editing_intent_class(self):
        original = self.build()
        cases = [
            {"segment_selections": [*self.selections[:1], EditingSegmentSelection(shot_id="feature", source_start_ms=1100), *self.selections[2:]]},
            {"overlay_timings": [self.timings[0].model_copy(update={"placement": "top_center"}), *self.timings[1:]]},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                self.assertNotEqual(original.id, self.build(**updates).id)

    def test_plan_identity_is_independent_of_runtime_video_paths(self):
        expected = self.build()
        moved = []
        for index, handoff in enumerate(self.handoffs):
            moved.append(
                handoff.model_copy(
                    update={
                        "runtime_video": handoff.runtime_video.model_copy(
                            update={"local_path": Path(f"/different/{index}.mp4")}
                        )
                    }
                )
            )
        self.assertEqual(expected.id, self.build(source_handoffs=moved).id)

    def test_product_campaign_and_storyboard_bindings_are_exact(self):
        cases = [
            {"product": self.product.model_copy(update={"id": "other"})},
            {"campaign": make_campaign(product_id="other")},
            {"storyboard": make_storyboard(campaign_id="other")},
        ]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(EditingPlanError):
                self.build(**updates)

    def test_every_shot_once_in_storyboard_order_without_extra(self):
        cases = [self.handoffs[:-1], [*self.handoffs, self.handoffs[0]], list(reversed(self.handoffs))]
        for handoffs in cases:
            with self.subTest(count=len(handoffs)), self.assertRaises(EditingPlanError):
                self.build(source_handoffs=handoffs)
        for selections in (self.selections[:-1], [*self.selections, self.selections[0]], list(reversed(self.selections))):
            with self.subTest(count=len(selections)), self.assertRaises(EditingPlanError):
                self.build(segment_selections=selections)

    def test_segment_must_fit_one_approved_range_and_match_shot_duration(self):
        too_late = self.selections[0].model_copy(update={"source_start_ms": 8000})
        with self.assertRaises(EditingPlanError):
            self.build(segment_selections=[too_late, *self.selections[1:]])

        changed_handoff = self.handoffs[0].model_copy(update={"intended_edit_duration_ms": 2600})
        with self.assertRaises(EditingPlanError):
            self.build(source_handoffs=[changed_handoff, *self.handoffs[1:]])

    def test_exact_campaign_timeline_rejects_tolerated_or_fractional_storyboard(self):
        tolerated = make_storyboard(durations=(2.5, 2.5, 2.75))
        handoffs, selections, timings = make_inputs(tolerated)
        with self.assertRaises(EditingPlanError):
            self.build(storyboard=tolerated, source_handoffs=handoffs, segment_selections=selections, overlay_timings=timings)

        fractional = make_storyboard(durations=(2.5001, 2.4999, 3.0))
        handoffs, selections, timings = make_inputs(fractional)
        with self.assertRaises(EditingPlanError):
            self.build(storyboard=fractional, source_handoffs=handoffs, segment_selections=selections, overlay_timings=timings)

    def test_plan_schema_rejects_timeline_gap_overlap_reorder_and_speed_fields(self):
        plan = self.build()
        for index, update in ((1, {"timeline_start_ms": 2600}), (1, {"timeline_start_ms": 2400})):
            data = plan.model_dump()
            data["segments"][index].update(update)
            data["id"] = "editing-plan:" + "0" * 64
            with self.subTest(update=update), self.assertRaises(ValidationError):
                EditingPlan.model_validate(data)
        data = plan.model_dump()
        data["segments"] = list(reversed(data["segments"]))
        with self.assertRaises(ValidationError):
            EditingPlan.model_validate(data)
        data = plan.model_dump()
        data["segments"][0]["playback_rate"] = 1.0
        with self.assertRaises(ValidationError):
            EditingPlan.model_validate(data)

    def test_every_storyboard_overlay_is_required_exactly_once(self):
        for timings in (self.timings[:-1], [*self.timings, self.timings[0]]):
            with self.subTest(count=len(timings)), self.assertRaises(EditingPlanError):
                self.build(overlay_timings=timings)
        extra = EditingOverlayTiming(shot_id="other", overlay_index=0, timeline_start_ms=0, timeline_end_ms=1, placement="center")
        with self.assertRaises(EditingPlanError):
            self.build(overlay_timings=[*self.timings, extra])

    def test_overlay_text_is_resolved_without_rewriting(self):
        plan = self.build()
        overlays = {(item.shot_id, item.overlay_index): item for item in plan.overlays}
        self.assertEqual(overlays[("hook", 0)].resolved_text, "BIG ENERGY")
        self.assertEqual(overlays[("feature", 0)].resolved_text, "Up to 20 hours of playback")
        self.assertEqual(overlays[("cta", 0)].resolved_text, "Soundcore Select 4 Go")
        self.assertEqual(overlays[("cta", 1)].resolved_text, "TAKE IT ANYWHERE")

    def test_overlay_timing_must_be_positive_and_inside_its_originating_shot(self):
        for update in ({"timeline_start_ms": 2499, "timeline_end_ms": 2501}, {"timeline_start_ms": 100, "timeline_end_ms": 100}):
            changed = self.timings[0].model_copy(update=update)
            with self.subTest(update=update), self.assertRaises((EditingPlanError, ValidationError)):
                self.build(overlay_timings=[changed, *self.timings[1:]])

    def test_only_small_placement_enum_and_social_style_are_accepted(self):
        for placement in ("top_center", "center", "bottom_center"):
            changed = self.timings[0].model_copy(update={"placement": placement})
            self.assertEqual(self.build(overlay_timings=[changed, *self.timings[1:]]).overlays[0].placement, placement)
        with self.assertRaises(ValidationError):
            EditingOverlayTiming(shot_id="hook", overlay_index=0, timeline_start_ms=1, timeline_end_ms=2, placement="10,10")
        data = self.build().model_dump()
        data["overlays"][0]["style_preset_id"] = "custom"
        with self.assertRaises(ValidationError):
            EditingPlan.model_validate(data)
        data = self.build().model_dump()
        data["overlays"][0]["font_path"] = "/tmp/font.ttf"
        with self.assertRaises(ValidationError):
            EditingPlan.model_validate(data)

    def test_fixed_provider_audio_mix_and_output_policy(self):
        plan = self.build()
        self.assertEqual(plan.source_video_audio, "discard")
        self.assertEqual(plan.audio_mix, AudioMixPolicy())
        self.assertEqual(plan.output, EditingOutputSpec())
        self.assertEqual((plan.output.width, plan.output.height), (720, 1280))
        self.assertEqual((plan.output.container, plan.output.video_codec, plan.output.audio_codec), ("mp4", "h264", "aac"))
        for field, value in (("source_video_audio", "preserve"), ("voice_over", {})):
            data = plan.model_dump()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EditingPlan.model_validate(data)

    def test_json_round_trip_unknown_fields_and_immutable_persistence(self):
        plan = self.build()
        with tempfile.TemporaryDirectory() as directory:
            path = save_editing_plan(plan, directory)
            self.assertEqual(load_editing_plan(path), plan)
            self.assertIn("\n  \"segments\"", path.read_text(encoding="utf-8"))
            self.assertEqual(save_editing_plan(plan, directory), path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["surprise"] = True
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(Exception):
                load_editing_plan(path)

    def test_builder_does_not_mutate_any_context_input(self):
        inputs = (self.product, self.campaign, self.storyboard, self.handoffs)
        before = deepcopy([item.model_dump() if hasattr(item, "model_dump") else [x.model_dump() for x in item] for item in inputs])
        self.build()
        after = [item.model_dump() if hasattr(item, "model_dump") else [x.model_dump() for x in item] for item in inputs]
        self.assertEqual(before, after)


class EditingPlanAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixture_directory.name)
        cls.music_source = cls.root / "music.wav"
        cls.sfx_source = cls.root / "sfx.wav"
        for path, duration, frequency in ((cls.music_source, 10, 220), (cls.sfx_source, 1, 880)):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=8000:duration={duration}", "-c:a", "pcm_s16le", "-y", str(path)],
                check=True,
                capture_output=True,
            )
        cls.music = ingest_editing_audio_asset(
            product_id="product-1",
            kind="music",
            source_path=cls.music_source,
            source_label="manual music",
            storage_root=cls.root / "store",
        )
        cls.sfx = ingest_editing_audio_asset(
            product_id="product-1",
            kind="sfx",
            source_path=cls.sfx_source,
            source_label="manual sfx",
            storage_root=cls.root / "store",
        )

    @classmethod
    def tearDownClass(cls):
        cls.fixture_directory.cleanup()

    def setUp(self):
        self.product = make_product()
        self.campaign = make_campaign()
        self.storyboard = make_storyboard()
        self.handoffs, self.selections, self.timings = make_inputs(self.storyboard)
        self.manifest = add_editing_audio_asset(add_editing_audio_asset(EditingAudioManifest(product_id="product-1"), self.music), self.sfx)

    def build(self, **updates):
        values = dict(product=self.product, campaign=self.campaign, storyboard=self.storyboard, source_handoffs=self.handoffs, segment_selections=self.selections, overlay_timings=self.timings, audio_manifest=self.manifest)
        values.update(updates)
        return build_editing_plan(**values)

    def test_zero_or_one_verified_music_and_zero_or_multiple_sfx(self):
        self.assertIsNone(self.build().music)
        plan = self.build(
            music_selection=EditingMusicSelection(asset_id=self.music.id, source_start_ms=1000, gain_db=-8.0),
            sfx_selections=(
                EditingSfxSelection(asset_id=self.sfx.id, timeline_start_ms=500, gain_db=-2.0),
                EditingSfxSelection(asset_id=self.sfx.id, timeline_start_ms=5000, gain_db=0.0),
            ),
        )
        self.assertEqual(plan.music.source_end_ms - plan.music.source_start_ms, 8000)
        self.assertFalse(plan.music.loop)
        self.assertEqual(len(plan.sfx), 2)
        self.assertTrue(all(item.source_start_ms == 0 for item in plan.sfx))
        with self.assertRaises(EditingPlanError):
            self.build(music_selection=[
                EditingMusicSelection(asset_id=self.music.id, source_start_ms=0, gain_db=-8.0),
                EditingMusicSelection(asset_id=self.music.id, source_start_ms=0, gain_db=-8.0),
            ])

    def test_audio_edits_change_identity_and_manifest_is_not_mutated(self):
        before = deepcopy(self.manifest.model_dump())
        quiet = self.build(
            music_selection=EditingMusicSelection(
                asset_id=self.music.id, source_start_ms=0, gain_db=-8.0
            )
        )
        louder = self.build(
            music_selection=EditingMusicSelection(
                asset_id=self.music.id, source_start_ms=0, gain_db=-7.0
            )
        )
        with_sfx = self.build(
            music_selection=EditingMusicSelection(
                asset_id=self.music.id, source_start_ms=0, gain_db=-8.0
            ),
            sfx_selections=(
                EditingSfxSelection(
                    asset_id=self.sfx.id, timeline_start_ms=500, gain_db=0.0
                ),
            ),
        )

        self.assertEqual(len({quiet.id, louder.id, with_sfx.id}), 3)
        self.assertEqual(self.manifest.model_dump(), before)

    def test_music_too_short_wrong_kind_or_missing_manifest_is_rejected(self):
        selection = EditingMusicSelection(asset_id=self.music.id, source_start_ms=3000, gain_db=-8.0)
        with self.assertRaises(EditingPlanError):
            self.build(music_selection=selection)
        with self.assertRaises(EditingPlanError):
            self.build(music_selection=EditingMusicSelection(asset_id=self.sfx.id, source_start_ms=0, gain_db=0.0))
        with self.assertRaises(EditingPlanError):
            self.build(audio_manifest=None, music_selection=EditingMusicSelection(asset_id=self.music.id, source_start_ms=0, gain_db=0.0))

    def test_sfx_uses_whole_asset_and_must_fit_timeline(self):
        with self.assertRaises(EditingPlanError):
            self.build(sfx_selections=(EditingSfxSelection(asset_id=self.sfx.id, timeline_start_ms=7500, gain_db=0.0),))
        data = EditingSfxSelection(asset_id=self.sfx.id, timeline_start_ms=1, gain_db=0.0).model_dump()
        data["source_end_ms"] = 100
        with self.assertRaises(ValidationError):
            EditingSfxSelection.model_validate(data)

    def test_gain_range_and_nonfinite_values_are_rejected(self):
        for gain in (-60.0, 12.0):
            self.build(music_selection=EditingMusicSelection(asset_id=self.music.id, source_start_ms=0, gain_db=gain))
        for gain in (-60.1, 12.1, float("nan")):
            with self.subTest(gain=gain), self.assertRaises(ValidationError):
                EditingMusicSelection(asset_id=self.music.id, source_start_ms=0, gain_db=gain)

    def test_stale_audio_or_unverified_asset_is_rejected(self):
        original = Path(self.sfx.local_path).read_bytes()
        Path(self.sfx.local_path).write_bytes(b"changed")
        try:
            with self.assertRaises(EditingPlanError):
                self.build(sfx_selections=(EditingSfxSelection(asset_id=self.sfx.id, timeline_start_ms=0, gain_db=0.0),))
        finally:
            Path(self.sfx.local_path).write_bytes(original)
        with self.assertRaises(EditingPlanError):
            self.build(sfx_selections=(EditingSfxSelection(asset_id="editing-audio:sfx:" + "f" * 64, timeline_start_ms=0, gain_db=0.0),))

    def test_audio_paths_do_not_affect_plan_identity_or_persisted_content(self):
        selection = EditingMusicSelection(asset_id=self.music.id, source_start_ms=0, gain_db=-5.0)
        plan = self.build(music_selection=selection)
        self.assertNotIn(str(self.root), plan.model_dump_json())
        self.assertNotIn("source_label", plan.model_dump_json())


if __name__ == "__main__":
    unittest.main()
