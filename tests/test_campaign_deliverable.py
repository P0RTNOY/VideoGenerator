import tempfile
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

import campaign_deliverable
from campaign_deliverable import (
    CampaignDeliverableError,
    CampaignDeliverableHandoff,
    build_campaign_deliverable_handoff,
    read_verified_campaign_deliverable_bytes,
)
from final_ad_review import (
    FinalAdReviewHistory,
    append_final_ad_review,
    create_final_ad_review_context,
)
from tests.test_final_ad_review import (
    FIXED_TIME,
    FinalAdReviewFixture,
    final_checklist,
    final_review,
)
from video_shot_review import (
    VideoShotReview,
    append_video_shot_review,
    create_video_shot_review_context,
)
from tests.test_ffmpeg_editor import passing_checklist


class CampaignDeliverableTests(FinalAdReviewFixture):
    def history_with(self, decision="approved"):
        context = self.context()
        history = FinalAdReviewHistory(review_context=context)
        if decision == "pending":
            return history
        if decision == "approved":
            review = final_review(context)
        else:
            review = final_review(
                context,
                decision="rejected_final_campaign_deliverable",
                checklist=final_checklist(overlay_readability="fail"),
                rejection_reasons=("overlay_readability_issue",),
            )
        return append_final_ad_review(
            history,
            review,
            self.artifact,
            self.plan,
            self.product,
            self.campaign,
            self.storyboard,
            self.video_artifacts,
            self.video_histories,
        )

    def build(self, **updates):
        history = updates.pop("history", None)
        values = {
            "history": history if history is not None else self.history_with(),
            "artifact": self.artifact,
            "plan": self.plan,
            "product": self.product,
            "campaign": self.campaign,
            "storyboard": self.storyboard,
            "video_artifacts": self.video_artifacts,
            "video_review_histories": self.video_histories,
        }
        values.update(updates)
        return build_campaign_deliverable_handoff(**values)

    def test_approved_soundcore_handoff_preserves_exact_authority_and_metadata(self):
        handoff = self.build()
        context = self.context()
        self.assertEqual(handoff.product_id, self.product.id)
        self.assertEqual(handoff.campaign_id, self.campaign.id)
        self.assertEqual(handoff.storyboard_id, self.storyboard.id)
        self.assertEqual(handoff.editing_plan_id, self.plan.id)
        self.assertEqual(handoff.editing_artifact_id, self.artifact.id)
        self.assertEqual(handoff.editing_artifact_sha256, self.artifact.sha256)
        self.assertEqual(handoff.final_review_context_id, context.id)
        self.assertEqual(handoff.authorizing_final_review_id, "final-review-1")
        self.assertEqual(handoff.runtime_artifact.local_path, Path(self.artifact.local_path).resolve())
        self.assertEqual(
            (
                handoff.runtime_artifact.width,
                handoff.runtime_artifact.height,
                handoff.runtime_artifact.duration_ms,
                handoff.runtime_artifact.container,
                handoff.runtime_artifact.video_codec,
                handoff.runtime_artifact.audio_codec,
            ),
            (720, 1280, 8000, "mp4", "h264", "aac"),
        )

    def test_pending_or_rejected_final_review_cannot_authorize_handoff(self):
        for state in ("pending", "rejected"):
            with self.subTest(state=state), self.assertRaises(CampaignDeliverableError):
                self.build(history=self.history_with(state))

    def test_stale_final_bytes_changed_plan_or_review_context_are_rejected(self):
        history = self.history_with()
        original = self.final_path.read_bytes()
        self.final_path.write_bytes(b"stale")
        try:
            with self.assertRaises(CampaignDeliverableError):
                self.build(history=history)
        finally:
            self.final_path.write_bytes(original)

        changed_plan = self.audio_plan
        rebound = self.artifact.model_copy(update={"editing_plan_id": changed_plan.id})
        with self.assertRaises(CampaignDeliverableError):
            self.build(history=history, artifact=rebound, plan=changed_plan)

        other_context = self.context(artifact=self.second_artifact)
        other_history = FinalAdReviewHistory(review_context=other_context)
        with self.assertRaises(CampaignDeliverableError):
            self.build(history=other_history)

    def test_revoked_or_stale_source_review_cannot_authorize_handoff(self):
        history = self.history_with()
        source_context = create_video_shot_review_context(
            self.video_artifacts[0], self.storyboard
        )
        revoked = VideoShotReview(
            review_id="source-revoked-for-delivery",
            review_context_id=source_context.id,
            reviewer_actor_id="owner:portnoy",
            decision="rejected_for_editing",
            reviewed_at=FIXED_TIME + timedelta(minutes=1),
            checklist=passing_checklist().model_copy(
                update={"camera_motion_usable": "fail"}
            ),
            rejection_reasons=("camera_motion_unusable",),
        )
        revoked_history = append_video_shot_review(
            self.video_histories[0],
            revoked,
            self.video_artifacts[0],
            self.storyboard,
        )
        with self.assertRaises(CampaignDeliverableError):
            self.build(
                history=history,
                video_review_histories=[revoked_history, *self.video_histories[1:]],
            )

        source_path = Path(self.video_artifacts[0].local_path)
        original = source_path.read_bytes()
        source_path.write_bytes(b"stale")
        try:
            with self.assertRaises(CampaignDeliverableError):
                self.build(history=history)
        finally:
            source_path.write_bytes(original)

    def test_consumer_rechecks_exact_sha_metadata_and_post_handoff_mutation(self):
        handoff = self.build()
        expected = self.final_path.read_bytes()
        self.assertEqual(read_verified_campaign_deliverable_bytes(handoff), expected)
        self.final_path.write_bytes(b"changed after handoff")
        try:
            with self.assertRaises(CampaignDeliverableError):
                read_verified_campaign_deliverable_bytes(handoff)
        finally:
            self.final_path.write_bytes(expected)

    def test_handoff_is_strict_ephemeral_and_excludes_forbidden_domains(self):
        handoff = self.build()
        fields = set(CampaignDeliverableHandoff.model_fields)
        serialized = handoff.model_dump_json()
        for forbidden in (
            "rights",
            "license",
            "legal",
            "distribution",
            "platform",
            "upload_destination",
            "source_videos",
            "hero_frames",
            "review_history",
            "product",
            "campaign",
            "storyboard",
            "editing_plan",
        ):
            self.assertNotIn(forbidden, fields)
            self.assertNotIn(f'"{forbidden}"', serialized)
        data = handoff.model_dump()
        data["distribution_platform"] = "TikTok"
        with self.assertRaises(ValidationError):
            CampaignDeliverableHandoff.model_validate(data)
        self.assertFalse(hasattr(campaign_deliverable, "save_campaign_deliverable_handoff"))
        self.assertFalse(hasattr(campaign_deliverable, "load_campaign_deliverable_handoff"))

    def test_builder_and_reader_revalidate_bypassed_models(self):
        handoff = self.build()
        bypassed = handoff.model_copy(
            update={
                "editing_artifact_sha256": "f" * 64,
            }
        )
        with self.assertRaises(CampaignDeliverableError):
            read_verified_campaign_deliverable_bytes(bypassed)

        history = self.history_with().model_copy(
            update={"reviews": (self.history_with().reviews[0],) * 2}
        )
        with self.assertRaises(CampaignDeliverableError):
            self.build(history=history)

    def test_build_does_not_mutate_any_input(self):
        history = self.history_with()
        objects = [
            history,
            self.artifact,
            self.plan,
            self.product,
            self.campaign,
            self.storyboard,
            *self.video_artifacts,
            *self.video_histories,
        ]
        before = deepcopy([item.model_dump() for item in objects])
        self.build(history=history)
        self.assertEqual(before, [item.model_dump() for item in objects])


if __name__ == "__main__":
    unittest.main()
