import hashlib
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from editing_artifact import EditingArtifact, EditingArtifactValidationError
from editing_audio_assets import (
    EditingAudioManifest,
    add_editing_audio_asset,
    ingest_editing_audio_asset,
)
from editing_plan import (
    EditingMusicSelection,
    EditingOverlayTiming,
    EditingSegmentSelection,
    EditingSfxSelection,
    build_editing_plan,
)
from final_ad_review import (
    FinalAdReview,
    FinalAdReviewChecklist,
    FinalAdReviewContext,
    FinalAdReviewError,
    FinalAdReviewHistory,
    FinalAdReviewPersistenceError,
    append_final_ad_review,
    create_final_ad_review_context,
    create_final_ad_review_history,
    final_ad_review_history_path,
    load_final_ad_review_history,
    resolve_final_ad_review_state,
    save_final_ad_review_history,
)
from storyboard import Storyboard, StoryboardShot
from tests.test_editing_plan import make_campaign, make_inputs, make_product, make_storyboard
from tests.test_ffmpeg_editor import approve, make_artifact, make_source, passing_checklist
from video_shot_review import (
    VideoShotReview,
    append_video_shot_review,
    build_editing_source_handoff,
    create_video_shot_review_context,
)


FIXED_TIME = datetime(2026, 8, 10, 22, 0, tzinfo=timezone.utc)


def final_checklist(**updates):
    values = {
        "artifact_and_plan_binding_correct": "pass",
        "shot_order_and_visual_continuity": "pass",
        "product_fidelity_across_shots": "pass",
        "overlay_completeness": "pass",
        "overlay_readability": "pass",
        "factual_claim_overlay_copy": "pass",
        "creative_or_cta_copy": "pass",
        "final_duration_and_technical_format": "pass",
        "audio_mix_quality": "pass",
        "provider_audio_absent": "pass",
        "music_sfx_synchronization": "not_applicable",
        "no_unexpected_visual_or_text_artifacts": "pass",
    }
    values.update(updates)
    return FinalAdReviewChecklist(**values)


def final_review(context, review_id="final-review-1", **updates):
    values = {
        "review_id": review_id,
        "review_context_id": context.id,
        "reviewer_actor_id": "owner:portnoy",
        "reviewed_at": FIXED_TIME,
        "decision": "approved_final_campaign_deliverable",
        "checklist": final_checklist(),
    }
    values.update(updates)
    return FinalAdReview(**values)


def make_final_artifact(path, plan, **updates):
    data = Path(path).read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    values = {
        "id": f"edited-ad:{sha256}",
        "product_id": "product-1",
        "campaign_id": "campaign-1",
        "storyboard_id": "storyboard-1",
        "editing_plan_id": plan.id,
        "sha256": sha256,
        "local_path": str(path),
        "width": 720,
        "height": 1280,
        "duration_ms": 8000,
        "container": "mp4",
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_stream_count": 1,
        "created_at": FIXED_TIME,
        "source_video_audio_discarded": True,
        "manual_final_review_required": True,
    }
    values.update(updates)
    return EditingArtifact(**values)


class FinalAdReviewFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.product = make_product()
        cls.campaign = make_campaign()
        cls.storyboard = make_storyboard()
        cls.video_artifacts = []
        cls.video_histories = []
        for index, (shot, color, frequency) in enumerate(
            zip(cls.storyboard.shots, ("red", "green", "blue"), (310, 410, 510))
        ):
            path = cls.root / f"source-{index}.mp4"
            make_source(path, color, frequency)
            artifact = make_artifact(path, shot.id, index)
            cls.video_artifacts.append(artifact)
            cls.video_histories.append(approve(artifact, cls.storyboard, index))
        cls.handoffs = [
            build_editing_source_handoff(artifact, cls.storyboard, history)
            for artifact, history in zip(cls.video_artifacts, cls.video_histories)
        ]
        _, cls.segment_selections, cls.overlay_timings = make_inputs(cls.storyboard)
        cls.plan = build_editing_plan(
            cls.product,
            cls.campaign,
            cls.storyboard,
            cls.handoffs,
            cls.segment_selections,
            cls.overlay_timings,
        )
        cls.final_path = cls.root / "final.mp4"
        make_source(cls.final_path, "purple", 700, duration=8)
        cls.artifact = make_final_artifact(cls.final_path, cls.plan)
        cls.second_final_path = cls.root / "second-final.mp4"
        make_source(cls.second_final_path, "orange", 701, duration=8)
        cls.second_artifact = make_final_artifact(cls.second_final_path, cls.plan)

        music_path = cls.root / "music.wav"
        sfx_path = cls.root / "sfx.wav"
        for path, duration, frequency in ((music_path, 10, 220), (sfx_path, 0.5, 880)):
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
                    "-c:a", "pcm_s16le", "-y", str(path),
                ],
                check=True,
                capture_output=True,
            )
        cls.music = ingest_editing_audio_asset(
            product_id=cls.product.id,
            kind="music",
            source_path=music_path,
            source_label="fixture music",
            storage_root=cls.root / "audio",
        )
        cls.sfx = ingest_editing_audio_asset(
            product_id=cls.product.id,
            kind="sfx",
            source_path=sfx_path,
            source_label="fixture sfx",
            storage_root=cls.root / "audio",
        )
        cls.audio_manifest = add_editing_audio_asset(
            add_editing_audio_asset(
                EditingAudioManifest(product_id=cls.product.id), cls.music
            ),
            cls.sfx,
        )
        cls.audio_plan = build_editing_plan(
            cls.product,
            cls.campaign,
            cls.storyboard,
            cls.handoffs,
            cls.segment_selections,
            cls.overlay_timings,
            audio_manifest=cls.audio_manifest,
            music_selection=EditingMusicSelection(
                asset_id=cls.music.id,
                source_start_ms=1000,
                gain_db=-6.0,
            ),
            sfx_selections=(
                EditingSfxSelection(
                    asset_id=cls.sfx.id,
                    timeline_start_ms=4000,
                    gain_db=-3.0,
                ),
            ),
        )

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def context(self, **updates):
        values = {
            "artifact": self.artifact,
            "plan": self.plan,
            "product": self.product,
            "campaign": self.campaign,
            "storyboard": self.storyboard,
            "video_artifacts": self.video_artifacts,
            "video_review_histories": self.video_histories,
        }
        values.update(updates)
        return create_final_ad_review_context(**values)

    def history(self, **updates):
        return FinalAdReviewHistory(review_context=self.context(**updates))


class FinalAdReviewContextTests(FinalAdReviewFixture):
    def test_context_is_deterministic_complete_path_free_and_soundcore_shaped(self):
        context = self.context()
        self.assertEqual(context, self.context())
        self.assertTrue(context.id.startswith("final-ad-review-context:"))
        self.assertEqual(context.editing_artifact_sha256, self.artifact.sha256)
        self.assertEqual(context.editing_plan_id, self.plan.id)
        self.assertEqual([item.shot_id for item in context.sources], ["hook", "feature", "cta"])
        self.assertEqual([item.resolved_text for item in context.overlays], [item.resolved_text for item in self.plan.overlays])
        serialized = context.model_dump_json()
        self.assertNotIn(self.artifact.local_path, serialized)
        self.assertNotIn("local_path", serialized)

        copied_path = self.root / "moved-final.mp4"
        copied_path.write_bytes(self.final_path.read_bytes())
        moved = self.artifact.model_copy(update={"local_path": str(copied_path)})
        self.assertEqual(context.id, self.context(artifact=moved).id)

    def test_exact_artifact_plan_and_domain_bindings_are_required(self):
        for update in (
            {"artifact": self.artifact.model_copy(update={"product_id": "other"})},
            {"artifact": self.artifact.model_copy(update={"campaign_id": "other"})},
            {"artifact": self.artifact.model_copy(update={"storyboard_id": "other"})},
            {"artifact": self.artifact.model_copy(update={"editing_plan_id": "editing-plan:" + "f" * 64})},
        ):
            with self.subTest(update=update), self.assertRaises(FinalAdReviewError):
                self.context(**update)

    def test_source_review_identity_and_selected_ranges_are_canonical_context(self):
        original = self.context()
        current_context = create_video_shot_review_context(
            self.video_artifacts[0], self.storyboard
        )
        later = VideoShotReview(
            review_id="later-source-approval",
            review_context_id=current_context.id,
            reviewer_actor_id="owner:portnoy",
            decision="approved_for_editing",
            reviewed_at=FIXED_TIME + timedelta(minutes=1),
            checklist=passing_checklist(),
            usable_ranges=self.video_histories[0].reviews[-1].usable_ranges,
        )
        later_history = append_video_shot_review(
            self.video_histories[0],
            later,
            self.video_artifacts[0],
            self.storyboard,
        )
        histories = [later_history, *self.video_histories[1:]]
        handoffs = [
            build_editing_source_handoff(item, self.storyboard, history)
            for item, history in zip(self.video_artifacts, histories)
        ]
        changed_plan = build_editing_plan(
            self.product,
            self.campaign,
            self.storyboard,
            handoffs,
            self.segment_selections,
            self.overlay_timings,
        )
        rebound_artifact = self.artifact.model_copy(
            update={"editing_plan_id": changed_plan.id}
        )
        changed = self.context(
            artifact=rebound_artifact,
            plan=changed_plan,
            video_review_histories=histories,
        )
        self.assertNotEqual(original.id, changed.id)
        self.assertEqual(changed.sources[0].authorizing_video_review_id, "later-source-approval")

        changed_selections = [
            EditingSegmentSelection(shot_id="hook", source_start_ms=1100),
            *self.segment_selections[1:],
        ]
        range_plan = build_editing_plan(
            self.product,
            self.campaign,
            self.storyboard,
            self.handoffs,
            changed_selections,
            self.overlay_timings,
        )
        range_artifact = self.artifact.model_copy(update={"editing_plan_id": range_plan.id})
        range_context = self.context(artifact=range_artifact, plan=range_plan)
        self.assertNotEqual(original.id, range_context.id)
        self.assertEqual(range_context.sources[0].source_start_ms, 1100)

    def test_overlay_copy_timing_placement_style_and_audio_are_canonical(self):
        original = self.context()
        changed_timing = [
            self.overlay_timings[0].model_copy(
                update={"timeline_start_ms": self.overlay_timings[0].timeline_start_ms + 1}
            ),
            *self.overlay_timings[1:],
        ]
        changed_plan = build_editing_plan(
            self.product,
            self.campaign,
            self.storyboard,
            self.handoffs,
            self.segment_selections,
            changed_timing,
        )
        changed_artifact = self.artifact.model_copy(update={"editing_plan_id": changed_plan.id})
        self.assertNotEqual(original.id, self.context(artifact=changed_artifact, plan=changed_plan).id)

        audio_artifact = self.artifact.model_copy(update={"editing_plan_id": self.audio_plan.id})
        audio_context = self.context(artifact=audio_artifact, plan=self.audio_plan)
        self.assertNotEqual(original.id, audio_context.id)
        self.assertEqual(audio_context.music.asset_sha256, self.music.sha256)
        self.assertEqual(audio_context.sfx[0].asset_sha256, self.sfx.sha256)
        self.assertEqual(audio_context.audio_mix.integrated_loudness_lufs, -14.0)
        self.assertEqual(audio_context.source_video_audio, "discard")

    def test_changed_resolved_overlay_copy_creates_a_different_context(self):
        changed_shots = []
        for shot in self.storyboard.shots:
            data = shot.model_dump()
            if shot.id == "hook":
                data["text_overlays"][0]["text"] = "DIFFERENT CREATIVE COPY"
            changed_shots.append(StoryboardShot.model_validate(data))
        storyboard = Storyboard(
            id=self.storyboard.id,
            campaign_id=self.storyboard.campaign_id,
            shots=changed_shots,
        )
        histories = [
            approve(artifact, storyboard, index + 200)
            for index, artifact in enumerate(self.video_artifacts)
        ]
        handoffs = [
            build_editing_source_handoff(artifact, storyboard, history)
            for artifact, history in zip(self.video_artifacts, histories)
        ]
        _, selections, timings = make_inputs(storyboard)
        plan = build_editing_plan(
            self.product,
            self.campaign,
            storyboard,
            handoffs,
            selections,
            timings,
        )
        artifact = self.artifact.model_copy(update={"editing_plan_id": plan.id})
        changed = create_final_ad_review_context(
            artifact=artifact,
            plan=plan,
            product=self.product,
            campaign=self.campaign,
            storyboard=storyboard,
            video_artifacts=self.video_artifacts,
            video_review_histories=histories,
        )
        self.assertNotEqual(self.context().id, changed.id)
        self.assertEqual(changed.overlays[0].resolved_text, "DIFFERENT CREATIVE COPY")

    def test_context_schema_rejects_identity_tampering_unknown_fields_and_paths(self):
        data = self.context().model_dump()
        for field, value in (("id", "wrong"), ("local_path", "/tmp/final.mp4"), ("rights_cleared", True)):
            changed = deepcopy(data)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                FinalAdReviewContext.model_validate(changed)

    def test_artifact_bytes_and_metadata_are_reverified(self):
        original = self.final_path.read_bytes()
        self.final_path.write_bytes(b"stale")
        try:
            with self.assertRaises(FinalAdReviewError):
                self.context()
        finally:
            self.final_path.write_bytes(original)
        wrong = self.artifact.model_copy(update={"duration_ms": 7999})
        with self.assertRaises(FinalAdReviewError):
            self.context(artifact=wrong)

    def test_different_exact_artifact_sha_has_a_different_context(self):
        self.assertNotEqual(self.artifact.sha256, self.second_artifact.sha256)
        self.assertNotEqual(
            self.context().id,
            self.context(artifact=self.second_artifact).id,
        )

    def test_context_creation_does_not_mutate_any_domain_or_authorization_input(self):
        objects = [
            self.artifact,
            self.plan,
            self.product,
            self.campaign,
            self.storyboard,
            *self.video_artifacts,
            *self.video_histories,
        ]
        before = deepcopy([item.model_dump() for item in objects])
        self.context()
        self.assertEqual(before, [item.model_dump() for item in objects])


class FinalAdReviewRecordTests(FinalAdReviewFixture):
    def context_without_overlay_kinds(self, removed_kinds):
        shots = []
        for shot in self.storyboard.shots:
            shots.append(
                StoryboardShot.model_validate(
                    {
                        **shot.model_dump(),
                        "text_overlays": [
                            item.model_dump()
                            for item in shot.text_overlays
                            if item.kind not in removed_kinds
                        ],
                    }
                )
            )
        storyboard = Storyboard(
            id=self.storyboard.id,
            campaign_id=self.storyboard.campaign_id,
            shots=shots,
        )
        histories = [
            approve(artifact, storyboard, index + 100)
            for index, artifact in enumerate(self.video_artifacts)
        ]
        handoffs = [
            build_editing_source_handoff(artifact, storyboard, history)
            for artifact, history in zip(self.video_artifacts, histories)
        ]
        segments = [
            EditingSegmentSelection(
                shot_id=shot.id,
                source_start_ms=1000,
            )
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
        plan = build_editing_plan(
            self.product,
            self.campaign,
            storyboard,
            handoffs,
            segments,
            timings,
        )
        artifact = self.artifact.model_copy(update={"editing_plan_id": plan.id})
        context = create_final_ad_review_context(
            artifact=artifact,
            plan=plan,
            product=self.product,
            campaign=self.campaign,
            storyboard=storyboard,
            video_artifacts=self.video_artifacts,
            video_review_histories=histories,
        )
        return context, artifact, plan, storyboard, histories

    def test_valid_approval_requires_complete_passing_manual_checklist(self):
        context = self.context()
        review = final_review(context)
        history = append_final_ad_review(
            FinalAdReviewHistory(review_context=context),
            review,
            self.artifact,
            self.plan,
            self.product,
            self.campaign,
            self.storyboard,
            self.video_artifacts,
            self.video_histories,
        )
        self.assertEqual(history.reviews[-1], review)
        for field in FinalAdReviewChecklist.model_fields:
            if field == "music_sfx_synchronization":
                continue
            with self.subTest(field=field), self.assertRaises((ValidationError, FinalAdReviewError)):
                append_final_ad_review(
                    FinalAdReviewHistory(review_context=context),
                    final_review(context, checklist=final_checklist(**{field: "fail"})),
                    self.artifact, self.plan, self.product, self.campaign,
                    self.storyboard, self.video_artifacts, self.video_histories,
                )

    def test_not_applicable_is_only_contextually_allowed_for_three_optional_items(self):
        with self.assertRaises(ValidationError):
            final_checklist(overlay_readability="not_applicable")
        context = self.context()
        with self.assertRaises(FinalAdReviewError):
            append_final_ad_review(
                FinalAdReviewHistory(review_context=context),
                final_review(context, checklist=final_checklist(factual_claim_overlay_copy="not_applicable")),
                self.artifact, self.plan, self.product, self.campaign,
                self.storyboard, self.video_artifacts, self.video_histories,
            )
        self.assertEqual(final_checklist().music_sfx_synchronization, "not_applicable")

        no_claim = self.context_without_overlay_kinds({"claim"})
        no_claim_review = final_review(
            no_claim[0],
            checklist=final_checklist(
                factual_claim_overlay_copy="not_applicable"
            ),
        )
        append_final_ad_review(
            FinalAdReviewHistory(review_context=no_claim[0]),
            no_claim_review,
            no_claim[1], no_claim[2], self.product, self.campaign,
            no_claim[3], self.video_artifacts, no_claim[4],
        )

        no_creative = self.context_without_overlay_kinds({"creative", "cta"})
        no_creative_review = final_review(
            no_creative[0],
            checklist=final_checklist(
                creative_or_cta_copy="not_applicable"
            ),
        )
        append_final_ad_review(
            FinalAdReviewHistory(review_context=no_creative[0]),
            no_creative_review,
            no_creative[1], no_creative[2], self.product, self.campaign,
            no_creative[3], self.video_artifacts, no_creative[4],
        )

        audio_artifact = self.artifact.model_copy(
            update={"editing_plan_id": self.audio_plan.id}
        )
        audio_context = self.context(
            artifact=audio_artifact,
            plan=self.audio_plan,
        )
        with self.assertRaises(FinalAdReviewError):
            append_final_ad_review(
                FinalAdReviewHistory(review_context=audio_context),
                final_review(audio_context, checklist=final_checklist()),
                audio_artifact, self.audio_plan, self.product, self.campaign,
                self.storyboard, self.video_artifacts, self.video_histories,
            )
        append_final_ad_review(
            FinalAdReviewHistory(review_context=audio_context),
            final_review(
                audio_context,
                checklist=final_checklist(music_sfx_synchronization="pass"),
            ),
            audio_artifact, self.audio_plan, self.product, self.campaign,
            self.storyboard, self.video_artifacts, self.video_histories,
        )

    def test_valid_rejection_requires_reason_and_other_requires_notes(self):
        context = self.context()
        with self.assertRaises(ValidationError):
            final_review(
                context,
                decision="rejected_final_campaign_deliverable",
                checklist=final_checklist(overlay_readability="fail"),
            )
        with self.assertRaises(ValidationError):
            final_review(
                context,
                decision="rejected_final_campaign_deliverable",
                checklist=final_checklist(overlay_readability="fail"),
                rejection_reasons=("other",),
            )
        rejected = final_review(
            context,
            decision="rejected_final_campaign_deliverable",
            checklist=final_checklist(overlay_readability="fail"),
            rejection_reasons=("overlay_readability_issue",),
        )
        self.assertIsNone(rejected.notes)

    def test_records_are_strict_immutable_utc_and_have_no_rights_or_audio_thresholds(self):
        context = self.context()
        review = final_review(context)
        with self.assertRaises(ValidationError):
            FinalAdReview.model_validate({**review.model_dump(), "rights_approved": True})
        with self.assertRaises(ValidationError):
            FinalAdReview.model_validate({**review.model_dump(), "reviewed_at": datetime(2026, 1, 1)})
        with self.assertRaises(ValidationError):
            FinalAdReview.model_validate({**review.model_dump(), "measured_lufs": -14.0})
        with self.assertRaises(ValidationError):
            review.notes = "changed"


class FinalAdReviewHistoryTests(FinalAdReviewFixture):
    def append(self, history, review, **updates):
        values = dict(
            current_artifact=self.artifact,
            current_plan=self.plan,
            current_product=self.product,
            current_campaign=self.campaign,
            current_storyboard=self.storyboard,
            current_video_artifacts=self.video_artifacts,
            current_video_review_histories=self.video_histories,
        )
        values.update(updates)
        return append_final_ad_review(history, review, **values)

    def state(self, history, **updates):
        values = dict(
            current_artifact=self.artifact,
            current_plan=self.plan,
            current_product=self.product,
            current_campaign=self.campaign,
            current_storyboard=self.storyboard,
            current_video_artifacts=self.video_artifacts,
            current_video_review_histories=self.video_histories,
        )
        values.update(updates)
        return resolve_final_ad_review_state(history, **values)

    def test_empty_approval_rejection_and_reapproval_follow_latest_exact_record(self):
        context = self.context()
        history = FinalAdReviewHistory(review_context=context)
        self.assertEqual(self.state(history), "pending_review")
        history = self.append(history, final_review(context, "approved-1"))
        self.assertEqual(self.state(history), "approved_final_campaign_deliverable")
        history = self.append(
            history,
            final_review(
                context,
                "rejected-1",
                decision="rejected_final_campaign_deliverable",
                checklist=final_checklist(overlay_readability="fail"),
                rejection_reasons=("overlay_readability_issue",),
                reviewed_at=FIXED_TIME + timedelta(minutes=1),
            ),
        )
        self.assertEqual(self.state(history), "rejected_final_campaign_deliverable")
        history = self.append(
            history,
            final_review(context, "approved-2", reviewed_at=FIXED_TIME + timedelta(minutes=2)),
        )
        self.assertEqual(self.state(history), "approved_final_campaign_deliverable")
        self.assertEqual([item.review_id for item in history.reviews], ["approved-1", "rejected-1", "approved-2"])

    def test_duplicate_ids_bypass_and_input_mutation_are_rejected(self):
        context = self.context()
        history = self.append(FinalAdReviewHistory(review_context=context), final_review(context))
        before = deepcopy(history.model_dump())
        with self.assertRaises(FinalAdReviewError):
            self.append(history, final_review(context))
        bypassed = history.model_copy(update={"reviews": (history.reviews[0], history.reviews[0])})
        with self.assertRaises(FinalAdReviewError):
            self.append(bypassed, final_review(context, "new"))
        self.assertEqual(before, history.model_dump())

    def test_changed_plan_or_artifact_context_never_inherits_approval(self):
        context = self.context()
        history = self.append(FinalAdReviewHistory(review_context=context), final_review(context))
        changed_timing = [
            self.overlay_timings[0].model_copy(update={"placement": "top_center"}),
            *self.overlay_timings[1:],
        ]
        changed_plan = build_editing_plan(
            self.product, self.campaign, self.storyboard, self.handoffs,
            self.segment_selections, changed_timing,
        )
        rebound = self.artifact.model_copy(update={"editing_plan_id": changed_plan.id})
        self.assertEqual(
            self.state(history, current_artifact=rebound, current_plan=changed_plan),
            "pending_review",
        )

    def test_revoked_or_stale_source_authorization_makes_final_state_pending(self):
        context = self.context()
        history = self.append(FinalAdReviewHistory(review_context=context), final_review(context))
        source_context = create_video_shot_review_context(self.video_artifacts[0], self.storyboard)
        revoked = VideoShotReview(
            review_id="source-revoked",
            review_context_id=source_context.id,
            reviewer_actor_id="owner:portnoy",
            decision="rejected_for_editing",
            reviewed_at=FIXED_TIME + timedelta(minutes=1),
            checklist=passing_checklist().model_copy(update={"camera_motion_usable": "fail"}),
            rejection_reasons=("camera_motion_unusable",),
        )
        revoked_history = append_video_shot_review(
            self.video_histories[0], revoked, self.video_artifacts[0], self.storyboard
        )
        self.assertEqual(
            self.state(history, current_video_review_histories=[revoked_history, *self.video_histories[1:]]),
            "pending_review",
        )
        source_path = Path(self.video_artifacts[0].local_path)
        original = source_path.read_bytes()
        source_path.write_bytes(b"stale")
        try:
            self.assertEqual(self.state(history), "pending_review")
        finally:
            source_path.write_bytes(original)

    def test_stale_final_artifact_makes_state_pending_and_blocks_append(self):
        context = self.context()
        history = self.append(FinalAdReviewHistory(review_context=context), final_review(context))
        original = self.final_path.read_bytes()
        self.final_path.write_bytes(b"stale")
        try:
            self.assertEqual(self.state(history), "pending_review")
            with self.assertRaises(FinalAdReviewError):
                self.append(history, final_review(context, "new"))
        finally:
            self.final_path.write_bytes(original)

    def test_identical_bytes_and_all_identical_context_inputs_retain_approval(self):
        context = self.context()
        history = self.append(FinalAdReviewHistory(review_context=context), final_review(context))
        copied_path = self.root / "byte-identical.mp4"
        copied_path.write_bytes(self.final_path.read_bytes())
        moved = self.artifact.model_copy(update={"local_path": str(copied_path)})
        self.assertEqual(self.state(history, current_artifact=moved), "approved_final_campaign_deliverable")


class FinalAdReviewPersistenceTests(FinalAdReviewFixture):
    def approved_history(self):
        context = self.context()
        return append_final_ad_review(
            FinalAdReviewHistory(review_context=context),
            final_review(context),
            self.artifact, self.plan, self.product, self.campaign,
            self.storyboard, self.video_artifacts, self.video_histories,
        )

    def test_human_json_round_trip_is_product_scoped_path_free_and_strict(self):
        history = self.approved_history()
        with tempfile.TemporaryDirectory() as directory:
            path = save_final_ad_review_history(history, directory)
            self.assertEqual(path, final_ad_review_history_path(history, directory).resolve())
            raw = path.read_text(encoding="utf-8")
            self.assertIn("\n  \"review_context\"", raw)
            self.assertNotIn(self.artifact.local_path, raw)
            loaded = load_final_ad_review_history(
                self.artifact, self.plan, self.product, self.campaign,
                self.storyboard, self.video_artifacts, self.video_histories,
                directory,
            )
            self.assertEqual(loaded, history)

    def test_persistence_is_append_only_and_rejects_unknown_fields(self):
        history = self.approved_history()
        with tempfile.TemporaryDirectory() as directory:
            path = save_final_ad_review_history(history, directory)
            with self.assertRaises(FinalAdReviewPersistenceError):
                save_final_ad_review_history(FinalAdReviewHistory(review_context=history.review_context), directory)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["rights"] = "approved"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(FinalAdReviewPersistenceError):
                load_final_ad_review_history(
                    self.artifact, self.plan, self.product, self.campaign,
                    self.storyboard, self.video_artifacts, self.video_histories,
                    directory,
                )


if __name__ == "__main__":
    unittest.main()
