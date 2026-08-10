import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from editing_artifact import (
    EditingArtifact,
    load_editing_artifact,
    save_editing_artifact,
)
from editing_audio_assets import (
    EditingAudioManifest,
    add_editing_audio_asset,
    ingest_editing_audio_asset,
)
from editing_instruction import (
    EditingInstructionError,
    compile_editing_instruction,
    resolve_social_bold_font,
)
from editing_plan import (
    EditingMusicSelection,
    EditingSfxSelection,
    build_editing_plan,
)
from ffmpeg_editor import FFmpegEditingError, render_editing_plan
from tests.test_editing_plan import make_campaign, make_inputs, make_product, make_storyboard
from video_shot_artifact import VideoShotArtifact
from video_shot_review import (
    UsableVideoRange,
    VideoFidelityChecklist,
    VideoShotReview,
    append_video_shot_review,
    build_editing_source_handoff,
    create_video_shot_review_context,
    create_video_shot_review_history,
)


FIXED_TIME = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)


def make_source(path: Path, color: str, frequency: int, *, size="720x1280", duration=10):
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=12:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", "-y", str(path),
        ],
        check=True,
        capture_output=True,
    )


def passing_checklist():
    return VideoFidelityChecklist(
        geometry_and_proportions_stable="pass",
        color_variant_and_materials_stable="pass",
        controls_buttons_and_ports="not_applicable",
        seams_and_panel_boundaries="not_applicable",
        logos_markings_and_labels="not_applicable",
        no_unintended_surface_reconstruction="pass",
        no_morphing_or_flicker="pass",
        requested_subject_motion_usable="pass",
        environment_motion_usable="pass",
        camera_motion_usable="pass",
        scene_consistency="pass",
        no_added_advertising_text="pass",
        sufficient_contiguous_editable_range="pass",
    )


def make_artifact(path: Path, shot_id: str, index: int, *, size=(720, 1280), duration=10.0):
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    return VideoShotArtifact(
        id=f"video-shot:{sha}",
        product_id="product-1",
        campaign_id="campaign-1",
        storyboard_id="storyboard-1",
        asset_plan_id="asset-plan-1",
        shot_id=shot_id,
        hero_frame_artifact_id="hero-frame:" + f"{index + 20:064x}",
        hero_frame_sha256=f"{index + 20:064x}",
        authorizing_review_context_id=f"hero-review-context-{index}",
        authorizing_review_id=f"hero-review-{index}",
        source_request_fingerprint=f"{index + 30:064x}",
        sha256=sha,
        local_path=str(path),
        duration_seconds=duration,
        width=size[0],
        height=size[1],
        container="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        has_audio=True,
        created_at=FIXED_TIME,
    )


def approve(artifact, storyboard, index):
    context = create_video_shot_review_context(artifact, storyboard)
    history = create_video_shot_review_history(artifact, storyboard)
    review = VideoShotReview(
        review_id=f"video-review-{index}",
        review_context_id=context.id,
        reviewer_actor_id="owner:portnoy",
        decision="approved_for_editing",
        reviewed_at=FIXED_TIME,
        checklist=passing_checklist(),
        usable_ranges=(
            UsableVideoRange(
                start_ms=500,
                end_ms=int(artifact.duration_seconds * 1000) - 500,
            ),
        ),
    )
    return append_video_shot_review(history, review, artifact, storyboard)


class FFmpegEditingFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.product = make_product()
        cls.campaign = make_campaign()
        cls.storyboard = make_storyboard()
        cls.artifacts = []
        cls.histories = []
        for index, (shot, color, frequency) in enumerate(
            zip(cls.storyboard.shots, ("red", "green", "blue"), (310, 410, 510))
        ):
            path = cls.root / f"source-{index}.mp4"
            make_source(path, color, frequency)
            artifact = make_artifact(path, shot.id, index)
            cls.artifacts.append(artifact)
            cls.histories.append(approve(artifact, cls.storyboard, index))
        handoffs = [
            build_editing_source_handoff(artifact, cls.storyboard, history)
            for artifact, history in zip(cls.artifacts, cls.histories)
        ]
        _, selections, timings = make_inputs(cls.storyboard)
        cls.plan = build_editing_plan(
            cls.product,
            cls.campaign,
            cls.storyboard,
            handoffs,
            selections,
            timings,
        )

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def compile(self, **updates):
        values = dict(
            plan=self.plan,
            product=self.product,
            campaign=self.campaign,
            storyboard=self.storyboard,
            video_artifacts=self.artifacts,
            video_review_histories=self.histories,
            audio_manifest=None,
        )
        values.update(updates)
        return compile_editing_instruction(**values)


class EditingInstructionTests(FFmpegEditingFixture):
    def test_valid_instruction_rebuilds_authority_and_preserves_exact_intent(self):
        instruction = self.compile()
        self.assertEqual(instruction.editing_plan_id, self.plan.id)
        self.assertEqual([item.shot_id for item in instruction.video_segments], ["hook", "feature", "cta"])
        self.assertEqual([item.resolved_text for item in instruction.overlays], [item.resolved_text for item in self.plan.overlays])
        self.assertEqual(instruction.source_video_audio, "discard")
        self.assertEqual((instruction.output.width, instruction.output.height), (720, 1280))
        self.assertTrue(instruction.runtime_font_path.is_file())

    def test_current_artifact_bytes_and_latest_approval_are_rechecked(self):
        artifact = self.artifacts[0]
        path = Path(artifact.local_path)
        original = path.read_bytes()
        path.write_bytes(b"stale")
        try:
            with self.assertRaises(EditingInstructionError):
                self.compile()
        finally:
            path.write_bytes(original)

        context = create_video_shot_review_context(artifact, self.storyboard)
        revoked = VideoShotReview(
            review_id="revoked",
            review_context_id=context.id,
            reviewer_actor_id="owner:portnoy",
            decision="rejected_for_editing",
            reviewed_at=FIXED_TIME,
            checklist=passing_checklist().model_copy(update={"camera_motion_usable": "fail"}),
            rejection_reasons=("camera_motion_unusable",),
        )
        history = append_video_shot_review(self.histories[0], revoked, artifact, self.storyboard)
        with self.assertRaises(EditingInstructionError):
            self.compile(video_review_histories=[history, *self.histories[1:]])

    def test_old_plan_cannot_survive_regenerated_or_different_review_authority(self):
        changed = self.histories[0].model_copy(
            update={
                "reviews": (
                    self.histories[0].reviews[0].model_copy(update={"review_id": "different"}),
                )
            }
        )
        with self.assertRaises(EditingInstructionError):
            self.compile(video_review_histories=[changed, *self.histories[1:]])

    def test_resize_policy_rejects_smaller_or_wrong_aspect_and_allows_larger(self):
        instruction = self.compile()
        self.assertTrue(all(item.resize_mode == "none" for item in instruction.video_segments))
        for case_index, (size, expected_mode) in enumerate(
            (((1080, 1920), "downscale"), ((360, 640), None), ((800, 1280), None)),
            start=50,
        ):
            path = self.root / f"geometry-{size[0]}x{size[1]}.mp4"
            make_source(path, "yellow", 610, size=f"{size[0]}x{size[1]}", duration=4)
            artifact = make_artifact(path, "hook", case_index, size=size, duration=4.0)
            def prepare_and_compile():
                history = approve(artifact, self.storyboard, case_index)
                artifacts = [artifact, *self.artifacts[1:]]
                histories = [history, *self.histories[1:]]
                handoffs = [
                    build_editing_source_handoff(item, self.storyboard, item_history)
                    for item, item_history in zip(artifacts, histories)
                ]
                _, selections, timings = make_inputs(self.storyboard)
                plan = build_editing_plan(
                    self.product, self.campaign, self.storyboard,
                    handoffs, selections, timings,
                )
                compiled = self.compile(
                    plan=plan,
                    video_artifacts=artifacts,
                    video_review_histories=histories,
                )
                return compiled, plan, artifacts, histories
            if expected_mode is None:
                with self.subTest(size=size), self.assertRaises((EditingInstructionError, ValueError)):
                    prepare_and_compile()
            else:
                compiled, plan, artifacts, histories = prepare_and_compile()
                self.assertEqual(compiled.video_segments[0].resize_mode, expected_mode)
                rendered = render_editing_plan(
                    plan, self.product, self.campaign, self.storyboard,
                    artifacts, histories, audio_manifest=None,
                    storage_root=self.root / "geometry-render",
                    created_at=FIXED_TIME,
                )
                self.assertEqual(
                    (rendered.artifact.width, rendered.artifact.height),
                    (720, 1280),
                )

    def test_font_resolver_is_runtime_only_and_fails_without_supported_font(self):
        self.assertTrue(resolve_social_bold_font().is_file())
        with self.assertRaises(EditingInstructionError):
            resolve_social_bold_font([])
        with self.assertRaises(EditingInstructionError):
            resolve_social_bold_font([self.root / "missing.ttf"])
        self.assertNotIn("font", self.plan.model_dump_json().lower())

    def test_instruction_models_are_ephemeral_strict_and_have_no_voiceover(self):
        instruction = self.compile()
        data = instruction.model_dump()
        data["voice_over"] = "forbidden"
        with self.assertRaises(ValidationError):
            type(instruction).model_validate(data)
        self.assertFalse(hasattr(instruction, "model_dump_json_file"))


class FFmpegRenderTests(FFmpegEditingFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.render_root = cls.root / "rendered"
        cls.result = render_editing_plan(
            cls.plan,
            cls.product,
            cls.campaign,
            cls.storyboard,
            cls.artifacts,
            cls.histories,
            audio_manifest=None,
            storage_root=cls.render_root,
            created_at=FIXED_TIME,
        )
        music_path = cls.root / "music.wav"
        sfx_path = cls.root / "sfx.wav"
        for path, duration, frequency in ((music_path, 10, 220), (sfx_path, 0.5, 880)):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={duration}", "-c:a", "pcm_s16le", "-y", str(path)],
                check=True, capture_output=True,
            )
        cls.music = ingest_editing_audio_asset(
            product_id="product-1", kind="music", source_path=music_path,
            source_label="manual fixture music", storage_root=cls.root / "audio-store",
        )
        cls.sfx = ingest_editing_audio_asset(
            product_id="product-1", kind="sfx", source_path=sfx_path,
            source_label="manual fixture SFX", storage_root=cls.root / "audio-store",
        )
        cls.manifest = add_editing_audio_asset(
            add_editing_audio_asset(EditingAudioManifest(product_id="product-1"), cls.music),
            cls.sfx,
        )
        handoffs = [
            build_editing_source_handoff(artifact, cls.storyboard, history)
            for artifact, history in zip(cls.artifacts, cls.histories)
        ]
        _, selections, timings = make_inputs(cls.storyboard)
        cls.audio_plan = build_editing_plan(
            cls.product, cls.campaign, cls.storyboard, handoffs, selections, timings,
            audio_manifest=cls.manifest,
            music_selection=EditingMusicSelection(
                asset_id=cls.music.id, source_start_ms=1000, gain_db=-6.0,
            ),
            sfx_selections=(
                EditingSfxSelection(
                    asset_id=cls.sfx.id, timeline_start_ms=4000, gain_db=-3.0,
                ),
            ),
        )
        cls.audio_result = render_editing_plan(
            cls.audio_plan, cls.product, cls.campaign, cls.storyboard,
            cls.artifacts, cls.histories, audio_manifest=cls.manifest,
            storage_root=cls.render_root, created_at=FIXED_TIME,
        )

    def test_three_shot_soundcore_style_render_is_exact_technical_artifact(self):
        artifact = self.result.artifact
        self.assertEqual((artifact.width, artifact.height), (720, 1280))
        self.assertEqual((artifact.container, artifact.video_codec, artifact.audio_codec), ("mp4", "h264", "aac"))
        self.assertLessEqual(abs(artifact.duration_ms - 8000), 100)
        self.assertEqual(artifact.id, f"edited-ad:{artifact.sha256}")
        self.assertEqual(hashlib.sha256(Path(artifact.local_path).read_bytes()).hexdigest(), artifact.sha256)
        self.assertIn("assets/derived/edited-ads", artifact.local_path)
        self.assertTrue(artifact.manual_final_review_required)
        self.assertFalse(hasattr(artifact, "approved_for_campaign"))

    def test_no_selected_audio_produces_one_silent_aac_track_and_discards_sources(self):
        probe = json.loads(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", self.result.artifact.local_path],
                check=True, capture_output=True, text=True,
            ).stdout
        )
        audio = [item for item in probe["streams"] if item["codec_type"] == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["codec_name"], "aac")
        volume = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", self.result.artifact.local_path, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        ).stderr
        maximum = re.search(r"max_volume: (-?[0-9.]+) dB", volume)
        self.assertIsNotNone(maximum)
        self.assertLessEqual(float(maximum.group(1)), -90.0)

    def test_render_uses_cut_only_exact_plan_timing_and_no_command_leaks_to_receipt(self):
        instruction = self.result.instruction
        self.assertEqual([(item.source_start_ms, item.source_end_ms) for item in instruction.video_segments], [(1000, 3500), (1000, 3500), (1000, 4000)])
        self.assertTrue(all(item.transition == "cut" for item in instruction.video_segments))
        receipt = self.result.receipt
        serialized = receipt.model_dump_json()
        self.assertEqual(receipt.tool, "ffmpeg")
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("command", serialized)
        self.assertNotIn("stderr", serialized)

    def test_shot_order_is_visible_and_concat_remains_cut_only(self):
        colors = []
        for timestamp in (1, 3, 6):
            pixel = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", self.result.artifact.local_path, "-vf", "crop=2:2:10:10", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                check=True, capture_output=True,
            ).stdout[:3]
            colors.append(tuple(pixel))
        self.assertGreater(colors[0][0], max(colors[0][1:]))
        self.assertGreater(colors[1][1], max(colors[1][0], colors[1][2]))
        self.assertGreater(colors[2][2], max(colors[2][:2]))

    def test_music_and_sfx_are_exact_verified_cues_with_normalized_final_mix(self):
        instruction = self.audio_result.instruction
        self.assertEqual((instruction.music.source_start_ms, instruction.music.source_end_ms), (1000, 9000))
        self.assertFalse(instruction.music.loop)
        self.assertEqual((instruction.sfx[0].timeline_start_ms, instruction.sfx[0].gain_db), (4000, -3.0))
        analysis = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", self.audio_result.artifact.local_path, "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
            capture_output=True, text=True,
        ).stderr
        report = json.loads(analysis[analysis.rfind("{"):analysis.rfind("}") + 1])
        self.assertLessEqual(abs(float(report["input_i"]) - (-14.0)), 0.7)
        self.assertLessEqual(float(report["input_tp"]), -0.8)

    def test_selected_audio_is_reverified_and_provider_audio_is_not_a_second_track(self):
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", self.audio_result.artifact.local_path],
            check=True, capture_output=True, text=True,
        ).stdout)
        self.assertEqual(len([item for item in probe["streams"] if item["codec_type"] == "audio"]), 1)
        path = Path(self.sfx.local_path)
        original = path.read_bytes()
        path.write_bytes(b"stale")
        try:
            with self.assertRaises(EditingInstructionError):
                compile_editing_instruction(
                    plan=self.audio_plan, product=self.product, campaign=self.campaign,
                    storyboard=self.storyboard, video_artifacts=self.artifacts,
                    video_review_histories=self.histories, audio_manifest=self.manifest,
                )
        finally:
            path.write_bytes(original)

    def test_artifact_json_round_trip_is_strict_and_human_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = save_editing_artifact(self.result.artifact, Path(directory) / "artifact.json")
            self.assertEqual(load_editing_artifact(path), self.result.artifact)
            self.assertIn("\n  \"product_id\"", path.read_text(encoding="utf-8"))
        data = self.result.artifact.model_dump()
        data["automatic_final_approval"] = True
        with self.assertRaises(ValidationError):
            EditingArtifact.model_validate(data)

    def test_render_does_not_mutate_domain_review_or_plan_inputs(self):
        objects = [self.product, self.campaign, self.storyboard, self.plan, *self.artifacts, *self.histories]
        before = deepcopy([item.model_dump() for item in objects])
        rerendered = render_editing_plan(
            self.plan, self.product, self.campaign, self.storyboard,
            self.artifacts, self.histories, audio_manifest=None,
            storage_root=self.render_root, created_at=FIXED_TIME,
        )
        self.assertEqual(before, [item.model_dump() for item in objects])
        self.assertEqual(rerendered.artifact.sha256, self.result.artifact.sha256)


if __name__ == "__main__":
    unittest.main()
