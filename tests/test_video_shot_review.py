import hashlib
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from demo_campaign_storyboard import build_soundcore_campaign_storyboard
from product import Product
from storyboard import Storyboard, StoryboardShot, StoryboardTextOverlay
from video_shot_artifact import (
    VideoShotArtifact,
    VideoShotArtifactValidationError,
    read_verified_video_shot_bytes,
)
from video_shot_review import (
    EditingSourceHandoff,
    UsableVideoRange,
    VideoFidelityChecklist,
    VideoShotReview,
    VideoShotReviewArtifactError,
    VideoShotReviewContext,
    VideoShotReviewError,
    VideoShotReviewHistory,
    VideoShotReviewPersistenceError,
    append_video_shot_review,
    build_editing_source_handoff,
    create_video_shot_review_context,
    create_video_shot_review_history,
    editing_context_fingerprint,
    load_video_shot_review_history,
    resolve_video_shot_review_state,
    save_video_shot_review_history,
    video_shot_review_history_path,
)


FIXED_TIME = datetime(2026, 8, 10, 18, 45, tzinfo=timezone.utc)


def make_mp4(path: Path, *, duration=8) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=90x160:r=12:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=8000:duration={duration}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def make_storyboard(
    *,
    shot_id="water-feature",
    duration=2.5,
    selected_claim_ids=None,
    overlays=None,
) -> Storyboard:
    return Storyboard(
        id="storyboard-1",
        campaign_id="campaign-1",
        shots=[
            StoryboardShot(
                id=shot_id,
                purpose="feature",
                duration_seconds=duration,
                selected_claim_ids=(
                    selected_claim_ids
                    if selected_claim_ids is not None
                    else ["feature:floating-design"]
                ),
                visual_description="Speaker beside a pool.",
                camera_direction="Slow push-in.",
                text_overlays=(
                    overlays
                    if overlays is not None
                    else [
                        StoryboardTextOverlay(
                            kind="claim",
                            claim_id="feature:floating-design",
                        ),
                        StoryboardTextOverlay(kind="cta", text="Take it anywhere"),
                    ]
                ),
            )
        ],
    )


def make_artifact(path: Path, **updates) -> VideoShotArtifact:
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    values = {
        "id": f"video-shot:{sha256}",
        "product_id": "example-product",
        "campaign_id": "campaign-1",
        "storyboard_id": "storyboard-1",
        "asset_plan_id": "asset-plan-1",
        "shot_id": "water-feature",
        "hero_frame_artifact_id": "hero-frame:" + "1" * 64,
        "hero_frame_sha256": "1" * 64,
        "authorizing_review_context_id": (
            "hero-frame-review-context:" + "2" * 64
        ),
        "authorizing_review_id": "hero-review-approved",
        "source_request_fingerprint": "3" * 64,
        "sha256": sha256,
        "local_path": str(path),
        "duration_seconds": 8.0,
        "width": 90,
        "height": 160,
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "video_codec": "h264",
        "has_audio": True,
        "created_at": FIXED_TIME,
    }
    values.update(updates)
    return VideoShotArtifact(**values)


def passing_checklist(**updates) -> VideoFidelityChecklist:
    values = {
        "geometry_and_proportions_stable": "pass",
        "color_variant_and_materials_stable": "pass",
        "controls_buttons_and_ports": "not_applicable",
        "seams_and_panel_boundaries": "not_applicable",
        "logos_markings_and_labels": "not_applicable",
        "no_unintended_surface_reconstruction": "pass",
        "no_morphing_or_flicker": "pass",
        "requested_subject_motion_usable": "pass",
        "environment_motion_usable": "pass",
        "camera_motion_usable": "pass",
        "scene_consistency": "pass",
        "no_added_advertising_text": "pass",
        "sufficient_contiguous_editable_range": "pass",
    }
    values.update(updates)
    return VideoFidelityChecklist(**values)


def review(
    context: VideoShotReviewContext,
    review_id="video-review-1",
    *,
    decision="approved_for_editing",
    checklist=None,
    usable_ranges=None,
    rejection_reasons=(),
    notes=None,
    reviewed_at=FIXED_TIME,
) -> VideoShotReview:
    return VideoShotReview(
        review_id=review_id,
        review_context_id=context.id,
        reviewer_actor_id="owner:portnoy",
        decision=decision,
        reviewed_at=reviewed_at,
        checklist=checklist or passing_checklist(),
        usable_ranges=(
            usable_ranges
            if usable_ranges is not None
            else (UsableVideoRange(start_ms=1200, end_ms=4100),)
        ),
        rejection_reasons=rejection_reasons,
        notes=notes,
    )


class VideoReviewFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.fixture_path = Path(cls.fixture_directory.name) / "fixture.mp4"
        cls.fixture_bytes = make_mp4(cls.fixture_path)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_directory.cleanup()

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.video_path = self.root / "raw.mp4"
        self.video_path.write_bytes(self.fixture_bytes)
        self.artifact = make_artifact(self.video_path)
        self.storyboard = make_storyboard()
        self.context = create_video_shot_review_context(
            self.artifact,
            self.storyboard,
        )
        self.history = VideoShotReviewHistory(review_context=self.context)

    def tearDown(self):
        self.temporary_directory.cleanup()


class VideoShotReviewContextTests(VideoReviewFixture):
    def test_context_is_deterministic_complete_and_path_independent(self):
        original = create_video_shot_review_context(self.artifact, self.storyboard)
        moved_path = self.root / "moved" / "same.mp4"
        moved_path.parent.mkdir()
        moved_path.write_bytes(self.fixture_bytes)
        moved = self.artifact.model_copy(
            update={"local_path": str(moved_path)},
            deep=True,
        )

        self.assertEqual(
            original,
            create_video_shot_review_context(moved, self.storyboard),
        )
        self.assertNotIn(str(self.root), original.model_dump_json())
        self.assertEqual(original.video_artifact_id, self.artifact.id)
        self.assertEqual(original.video_artifact_sha256, self.artifact.sha256)
        self.assertEqual(original.raw_duration_seconds, 8.0)
        self.assertEqual(original.raw_duration_ms, 8000)
        self.assertEqual(original.intended_edit_duration_ms, 2500)
        self.assertTrue(original.has_audio)
        self.assertEqual(
            original.provider_audio_disposition,
            "discard_before_editing",
        )

    def test_artifact_sha_request_and_hero_authorization_affect_context(self):
        original = self.context.id
        other_path = self.root / "other.mp4"
        other_path.write_bytes(self.fixture_bytes + b"safe-trailing-change")
        mutations = {
            "sha": make_artifact(other_path),
            "request": self.artifact.model_copy(
                update={"source_request_fingerprint": "4" * 64}, deep=True
            ),
            "hero_artifact": self.artifact.model_copy(
                update={
                    "hero_frame_artifact_id": "hero-frame:" + "5" * 64,
                    "hero_frame_sha256": "5" * 64,
                },
                deep=True,
            ),
            "hero_context": self.artifact.model_copy(
                update={
                    "authorizing_review_context_id": (
                        "hero-frame-review-context:" + "6" * 64
                    )
                },
                deep=True,
            ),
            "hero_review": self.artifact.model_copy(
                update={"authorizing_review_id": "different-review"}, deep=True
            ),
        }
        for name, artifact in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    original,
                    create_video_shot_review_context(
                        artifact,
                        self.storyboard,
                    ).id,
                )

    def test_editing_fingerprint_is_deterministic_and_path_free(self):
        first = editing_context_fingerprint(self.storyboard, "water-feature")
        second = editing_context_fingerprint(
            Storyboard.model_validate(self.storyboard.model_dump()),
            "water-feature",
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_selected_claim_duration_and_overlay_changes_stale_context(self):
        changes = {
            "claim": make_storyboard(selected_claim_ids=[]),
            "duration": make_storyboard(duration=2.75),
            "overlay_value": make_storyboard(
                overlays=[StoryboardTextOverlay(kind="cta", text="Shop now")]
            ),
            "overlay_order": make_storyboard(
                overlays=list(reversed(self.storyboard.shots[0].text_overlays))
            ),
        }
        for name, storyboard in changes.items():
            with self.subTest(name=name):
                changed = create_video_shot_review_context(
                    self.artifact,
                    storyboard,
                )
                self.assertNotEqual(
                    changed.editing_context_fingerprint,
                    self.context.editing_context_fingerprint,
                )
                self.assertNotEqual(changed.id, self.context.id)

    def test_context_rejects_wrong_storyboard_or_shot_binding(self):
        wrong_storyboard = self.storyboard.model_copy(
            update={"id": "different-storyboard"}, deep=True
        )
        with self.assertRaises(VideoShotReviewError):
            create_video_shot_review_context(self.artifact, wrong_storyboard)

        missing_shot = make_storyboard(shot_id="other-shot")
        with self.assertRaises(VideoShotReviewError):
            create_video_shot_review_context(self.artifact, missing_shot)

    def test_context_schema_rejects_unknown_and_inconsistent_identity(self):
        data = self.context.model_dump()
        data["local_path"] = str(self.video_path)
        with self.assertRaises(ValidationError):
            VideoShotReviewContext.model_validate(data)
        data = self.context.model_dump()
        data["id"] = "video-shot-review-context:" + "f" * 64
        with self.assertRaises(ValidationError):
            VideoShotReviewContext.model_validate(data)


class UsableVideoRangeTests(unittest.TestCase):
    def test_valid_range_uses_strict_integer_milliseconds(self):
        value = UsableVideoRange(start_ms=1200, end_ms=4100)
        self.assertEqual(value.duration_ms, 2900)
        for invalid in ((1.2, 2000), (0, 2000.0), (True, 2000)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    UsableVideoRange(start_ms=invalid[0], end_ms=invalid[1])

    def test_negative_zero_length_and_reversed_ranges_are_rejected(self):
        for start_ms, end_ms in ((-1, 100), (100, 100), (101, 100)):
            with self.subTest(start_ms=start_ms, end_ms=end_ms):
                with self.assertRaises(ValidationError):
                    UsableVideoRange(start_ms=start_ms, end_ms=end_ms)

    def test_unsorted_or_overlapping_ranges_are_rejected_but_touching_is_allowed(self):
        checklist = passing_checklist()
        with self.assertRaises(ValidationError):
            VideoShotReview(
                review_id="unsorted",
                review_context_id="context",
                reviewer_actor_id="reviewer",
                decision="approved_for_editing",
                reviewed_at=FIXED_TIME,
                checklist=checklist,
                usable_ranges=((UsableVideoRange(start_ms=2000, end_ms=3000)),
                               (UsableVideoRange(start_ms=0, end_ms=1000))),
            )
        with self.assertRaises(ValidationError):
            VideoShotReview(
                review_id="overlap",
                review_context_id="context",
                reviewer_actor_id="reviewer",
                decision="approved_for_editing",
                reviewed_at=FIXED_TIME,
                checklist=checklist,
                usable_ranges=((UsableVideoRange(start_ms=0, end_ms=2000)),
                               (UsableVideoRange(start_ms=1999, end_ms=3000))),
            )
        touching = VideoShotReview(
            review_id="touching",
            review_context_id="context",
            reviewer_actor_id="reviewer",
            decision="approved_for_editing",
            reviewed_at=FIXED_TIME,
            checklist=checklist,
            usable_ranges=((UsableVideoRange(start_ms=0, end_ms=2500)),
                           (UsableVideoRange(start_ms=2500, end_ms=3000))),
        )
        self.assertEqual(len(touching.usable_ranges), 2)


class VideoShotReviewRecordTests(VideoReviewFixture):
    def test_approved_review_accepts_one_long_enough_range(self):
        item = review(self.context)
        history = append_video_shot_review(
            self.history,
            item,
            self.artifact,
            self.storyboard,
        )
        self.assertEqual(history.reviews[-1], item)

    def test_approved_review_requires_one_contiguous_long_enough_range(self):
        insufficient_sets = (
            (UsableVideoRange(start_ms=0, end_ms=1000),),
            (
                UsableVideoRange(start_ms=0, end_ms=1000),
                UsableVideoRange(start_ms=2000, end_ms=3000),
                UsableVideoRange(start_ms=4000, end_ms=5000),
            ),
        )
        for ranges in insufficient_sets:
            with self.subTest(ranges=ranges):
                with self.assertRaises(VideoShotReviewError):
                    append_video_shot_review(
                        self.history,
                        review(self.context, usable_ranges=ranges),
                        self.artifact,
                        self.storyboard,
                    )

    def test_approved_review_rejects_empty_or_out_of_bounds_ranges(self):
        with self.assertRaises(ValidationError):
            review(self.context, usable_ranges=())
        with self.assertRaises(VideoShotReviewError):
            append_video_shot_review(
                self.history,
                review(
                    self.context,
                    usable_ranges=(UsableVideoRange(start_ms=5000, end_ms=8100),),
                ),
                self.artifact,
                self.storyboard,
            )

    def test_visibility_na_is_allowed_but_required_na_or_fail_blocks_approval(self):
        approved = review(self.context)
        self.assertEqual(
            approved.checklist.controls_buttons_and_ports,
            "not_applicable",
        )
        with self.assertRaises(ValidationError):
            passing_checklist(scene_consistency="not_applicable")
        with self.assertRaises(ValidationError):
            review(
                self.context,
                checklist=passing_checklist(scene_consistency="fail"),
            )

    def test_valid_rejection_requires_reason_and_contains_no_authorizing_ranges(self):
        rejected = review(
            self.context,
            decision="rejected_for_editing",
            checklist=passing_checklist(geometry_and_proportions_stable="fail"),
            usable_ranges=(),
            rejection_reasons=("geometry_instability",),
        )
        history = append_video_shot_review(
            self.history,
            rejected,
            self.artifact,
            self.storyboard,
        )
        self.assertEqual(history.reviews[-1].usable_ranges, ())

        with self.assertRaises(ValidationError):
            review(
                self.context,
                decision="rejected_for_editing",
                checklist=passing_checklist(geometry_and_proportions_stable="fail"),
                usable_ranges=(),
            )

    def test_rejection_other_requires_notes_and_reason_must_be_consistent(self):
        with self.assertRaises(ValidationError):
            review(
                self.context,
                decision="rejected_for_editing",
                usable_ranges=(),
                rejection_reasons=("other",),
            )
        with self.assertRaises(ValidationError):
            review(
                self.context,
                decision="rejected_for_editing",
                usable_ranges=(),
                rejection_reasons=("geometry_instability",),
            )
        rejected = review(
            self.context,
            decision="rejected_for_editing",
            usable_ranges=(),
            rejection_reasons=("other",),
            notes="Motion becomes compositionally unusable.",
        )
        self.assertEqual(rejected.notes, "Motion becomes compositionally unusable.")

    def test_record_schema_actor_timestamp_and_audio_boundary_are_strict(self):
        item = review(self.context)
        self.assertNotIn("audio", item.model_dump_json())
        with self.assertRaises(ValidationError):
            review(self.context, reviewed_at=datetime(2026, 8, 10, 18, 45))
        data = item.model_dump()
        data["audio_approved"] = True
        with self.assertRaises(ValidationError):
            VideoShotReview.model_validate(data)


class VideoShotReviewHistoryTests(VideoReviewFixture):
    def append(self, item):
        self.history = append_video_shot_review(
            self.history,
            item,
            self.artifact,
            self.storyboard,
        )

    def test_empty_history_is_derived_pending_and_pending_is_not_persisted(self):
        self.assertEqual(
            resolve_video_shot_review_state(self.history, self.context),
            "pending_review",
        )
        self.assertNotIn("pending_review", self.history.model_dump_json())

    def test_latest_exact_review_controls_approval_rejection_and_reapproval(self):
        self.append(review(self.context, "approved-first"))
        self.assertEqual(
            resolve_video_shot_review_state(self.history, self.context),
            "approved_for_editing",
        )
        self.append(
            review(
                self.context,
                "rejected-second",
                decision="rejected_for_editing",
                checklist=passing_checklist(no_morphing_or_flicker="fail"),
                usable_ranges=(),
                rejection_reasons=("morphing_or_flicker",),
            )
        )
        self.assertEqual(
            resolve_video_shot_review_state(self.history, self.context),
            "rejected_for_editing",
        )
        self.append(
            review(
                self.context,
                "approved-third",
                reviewed_at=FIXED_TIME + timedelta(minutes=5),
            )
        )
        self.assertEqual(
            resolve_video_shot_review_state(self.history, self.context),
            "approved_for_editing",
        )

    def test_stale_video_request_hero_and_editing_context_are_pending(self):
        self.append(review(self.context))
        changed_artifact = self.artifact.model_copy(
            update={"source_request_fingerprint": "9" * 64}, deep=True
        )
        changed_storyboard = make_storyboard(duration=3.0)
        stale_contexts = (
            create_video_shot_review_context(changed_artifact, self.storyboard),
            create_video_shot_review_context(self.artifact, changed_storyboard),
        )
        for context in stale_contexts:
            with self.subTest(context=context.id):
                self.assertEqual(
                    resolve_video_shot_review_state(self.history, context),
                    "pending_review",
                )

    def test_regenerated_video_and_same_binary_other_shot_are_separate(self):
        self.append(review(self.context))
        regenerated_path = self.root / "regenerated.mp4"
        regenerated_bytes = make_mp4(regenerated_path, duration=8)
        regenerated_path.write_bytes(regenerated_bytes + b"new")
        regenerated = make_artifact(regenerated_path)
        other_storyboard = make_storyboard(shot_id="other-shot")
        other_shot = self.artifact.model_copy(update={"shot_id": "other-shot"})
        contexts = (
            create_video_shot_review_context(regenerated, self.storyboard),
            create_video_shot_review_context(other_shot, other_storyboard),
        )
        for current in contexts:
            with self.subTest(current=current.id):
                self.assertEqual(
                    resolve_video_shot_review_state(self.history, current),
                    "pending_review",
                )

    def test_duplicate_ids_wrong_context_and_unchecked_models_are_rejected(self):
        first = review(self.context)
        self.append(first)
        with self.assertRaises(VideoShotReviewError):
            self.append(first)
        wrong = review(self.context, "wrong").model_copy(
            update={"review_context_id": "different"}, deep=True
        )
        with self.assertRaises(VideoShotReviewError):
            self.append(wrong)
        bypassed = self.history.model_copy(update={"schema_version": 2}, deep=True)
        with self.assertRaises(VideoShotReviewError):
            append_video_shot_review(
                bypassed,
                review(self.context, "new"),
                self.artifact,
                self.storyboard,
            )

    def test_history_rejects_contextually_invalid_review_without_append_helper(self):
        short_approval = review(
            self.context,
            usable_ranges=(UsableVideoRange(start_ms=0, end_ms=1000),),
        )
        with self.assertRaises(ValidationError):
            VideoShotReviewHistory(
                review_context=self.context,
                reviews=(short_approval,),
            )

        out_of_bounds = review(
            self.context,
            review_id="out-of-bounds",
            usable_ranges=(UsableVideoRange(start_ms=5000, end_ms=8100),),
        )
        with self.assertRaises(ValidationError):
            VideoShotReviewHistory(
                review_context=self.context,
                reviews=(out_of_bounds,),
            )

    def test_append_preserves_order_inputs_and_nested_immutability(self):
        original_history = deepcopy(self.history.model_dump())
        first = review(self.context, "first")
        original_review = deepcopy(first.model_dump())
        self.append(first)
        self.append(review(self.context, "second"))

        self.assertEqual(original_history, VideoShotReviewHistory(
            review_context=self.context
        ).model_dump())
        self.assertEqual(first.model_dump(), original_review)
        self.assertEqual(
            [item.review_id for item in self.history.reviews],
            ["first", "second"],
        )
        with self.assertRaises(ValidationError):
            self.history.reviews[0].decision = "rejected_for_editing"
        with self.assertRaises(ValidationError):
            self.history.reviews[0].checklist.scene_consistency = "fail"
        with self.assertRaises(AttributeError):
            self.history.reviews.append(review(self.context, "third"))

    def test_append_rechecks_artifact_integrity_and_history_round_trips(self):
        self.video_path.write_bytes(b"changed")
        with self.assertRaises(VideoShotReviewArtifactError):
            self.append(review(self.context))

        self.video_path.write_bytes(self.fixture_bytes)
        self.append(review(self.context))
        restored = VideoShotReviewHistory.model_validate_json(
            self.history.model_dump_json()
        )
        self.assertEqual(restored, self.history)
        data = self.history.model_dump()
        data["unknown"] = True
        with self.assertRaises(ValidationError):
            VideoShotReviewHistory.model_validate(data)


class VideoShotReviewPersistenceTests(VideoReviewFixture):
    def test_history_path_is_product_scoped_and_context_hashed(self):
        path = video_shot_review_history_path(self.history, self.root / "products")
        context_hash = self.context.id.rsplit(":", 1)[1]
        self.assertEqual(
            path,
            self.root
            / "products"
            / "example-product"
            / "reviews"
            / "video-shots"
            / f"{context_hash}.json",
        )

    def test_human_readable_json_round_trip_is_path_free_and_strict(self):
        history = append_video_shot_review(
            self.history,
            review(self.context),
            self.artifact,
            self.storyboard,
        )
        storage_root = self.root / "products"
        path = save_video_shot_review_history(history, storage_root)
        raw = path.read_text(encoding="utf-8")
        loaded = load_video_shot_review_history(
            self.artifact,
            self.storyboard,
            storage_root,
        )

        self.assertEqual(loaded, history)
        self.assertTrue(raw.endswith("\n"))
        self.assertNotIn(str(self.video_path), raw)
        for forbidden in (
            "provider_prompt",
            "provider_operation_id",
            "audio_approved",
            "product_images",
        ):
            self.assertNotIn(forbidden, raw)

    def test_missing_history_loads_empty_pending_context(self):
        loaded = load_video_shot_review_history(
            self.artifact,
            self.storyboard,
            self.root / "products",
        )
        self.assertEqual(loaded.reviews, ())
        self.assertEqual(
            resolve_video_shot_review_state(loaded, self.context),
            "pending_review",
        )

    def test_persistence_refuses_rewrite_or_truncation(self):
        first = append_video_shot_review(
            self.history,
            review(self.context),
            self.artifact,
            self.storyboard,
        )
        storage_root = self.root / "products"
        save_video_shot_review_history(first, storage_root)
        with self.assertRaises(VideoShotReviewPersistenceError):
            save_video_shot_review_history(self.history, storage_root)

        changed = deepcopy(first.model_dump())
        changed["reviews"][0]["reviewer_actor_id"] = "rewritten"
        with self.assertRaises(VideoShotReviewPersistenceError):
            save_video_shot_review_history(
                VideoShotReviewHistory.model_validate(changed),
                storage_root,
            )


class EditingSourceHandoffTests(VideoReviewFixture):
    def approved_history(self):
        return append_video_shot_review(
            self.history,
            review(self.context),
            self.artifact,
            self.storyboard,
        )

    def test_approved_handoff_preserves_exact_ranges_duration_overlays_and_audio_policy(self):
        handoff = build_editing_source_handoff(
            self.artifact,
            self.storyboard,
            self.approved_history(),
        )

        self.assertEqual(handoff.product_id, "example-product")
        self.assertEqual(handoff.video_artifact_id, self.artifact.id)
        self.assertEqual(handoff.authorizing_video_review_id, "video-review-1")
        self.assertEqual(
            handoff.approved_usable_ranges,
            (UsableVideoRange(start_ms=1200, end_ms=4100),),
        )
        self.assertEqual(handoff.intended_edit_duration_ms, 2500)
        self.assertEqual(
            [overlay.kind for overlay in handoff.text_overlays],
            ["claim", "cta"],
        )
        self.assertEqual(
            handoff.provider_audio_disposition,
            "discard_before_editing",
        )
        self.assertTrue(handoff.runtime_video.has_audio)

    def test_pending_or_rejected_history_cannot_create_handoff(self):
        with self.assertRaises(VideoShotReviewError):
            build_editing_source_handoff(
                self.artifact,
                self.storyboard,
                self.history,
            )
        rejected = append_video_shot_review(
            self.history,
            review(
                self.context,
                decision="rejected_for_editing",
                checklist=passing_checklist(camera_motion_usable="fail"),
                usable_ranges=(),
                rejection_reasons=("camera_motion_unusable",),
            ),
            self.artifact,
            self.storyboard,
        )
        with self.assertRaises(VideoShotReviewError):
            build_editing_source_handoff(
                self.artifact,
                self.storyboard,
                rejected,
            )

    def test_post_review_file_mutation_prevents_handoff(self):
        history = self.approved_history()
        self.video_path.write_bytes(b"changed after review")
        with self.assertRaises(VideoShotReviewArtifactError):
            build_editing_source_handoff(
                self.artifact,
                self.storyboard,
                history,
            )

    def test_changed_editing_intent_prevents_handoff(self):
        history = self.approved_history()
        for changed in (
            make_storyboard(duration=3.0),
            make_storyboard(selected_claim_ids=[]),
            make_storyboard(
                overlays=[StoryboardTextOverlay(kind="creative", text="New")]
            ),
        ):
            with self.subTest(changed=changed.model_dump()):
                with self.assertRaises(VideoShotReviewError):
                    build_editing_source_handoff(
                        self.artifact,
                        changed,
                        history,
                    )

    def test_handoff_is_ephemeral_strict_and_exposes_no_forbidden_state(self):
        handoff = build_editing_source_handoff(
            self.artifact,
            self.storyboard,
            self.approved_history(),
        )
        serialized = handoff.model_dump_json()
        fields = set(EditingSourceHandoff.model_fields)
        for forbidden in (
            "product_images",
            "hero_frame_references",
            "provider_prompt",
            "provider_operation_id",
            "music",
            "sfx",
            "voice_over",
            "ffmpeg_filter_graph",
            "clip_outside_approved_ranges",
            "audio_approved",
        ):
            self.assertNotIn(forbidden, fields)
            self.assertNotIn(forbidden, serialized)
        data = handoff.model_dump()
        data["provider_prompt"] = "forbidden"
        with self.assertRaises(ValidationError):
            EditingSourceHandoff.model_validate(data)

    def test_handoff_schema_rejects_reordered_or_overlapping_authorization(self):
        handoff = build_editing_source_handoff(
            self.artifact,
            self.storyboard,
            self.approved_history(),
        )
        data = handoff.model_dump()
        data["approved_usable_ranges"] = [
            {"start_ms": 3000, "end_ms": 6000},
            {"start_ms": 2000, "end_ms": 5000},
        ]
        with self.assertRaises(ValidationError):
            EditingSourceHandoff.model_validate(data)

    def test_handoff_deep_copies_inputs_and_does_not_mutate_artifact_or_storyboard(self):
        history = self.approved_history()
        before = (
            deepcopy(self.artifact.model_dump()),
            deepcopy(self.storyboard.model_dump()),
            deepcopy(history.model_dump()),
        )
        handoff = build_editing_source_handoff(
            self.artifact,
            self.storyboard,
            history,
        )

        self.assertEqual(
            before,
            (
                self.artifact.model_dump(),
                self.storyboard.model_dump(),
                history.model_dump(),
            ),
        )
        self.storyboard.shots[0].text_overlays[1].text = "Changed later"
        self.assertEqual(handoff.text_overlays[1].text, "Take it anywhere")

    def test_verified_video_reader_rejects_sha_or_metadata_changes(self):
        self.assertEqual(
            read_verified_video_shot_bytes(self.artifact),
            self.fixture_bytes,
        )
        self.video_path.write_bytes(b"changed")
        with self.assertRaises(VideoShotArtifactValidationError):
            read_verified_video_shot_bytes(self.artifact)

        self.video_path.write_bytes(self.fixture_bytes)
        wrong_metadata = self.artifact.model_copy(update={"video_codec": "vp9"})
        with self.assertRaises(VideoShotArtifactValidationError):
            read_verified_video_shot_bytes(wrong_metadata)


class VideoShotReviewSoundcoreTests(VideoReviewFixture):
    def test_soundcore_eight_second_raw_clip_authorizes_only_manual_2900ms_range(self):
        campaign, storyboard = build_soundcore_campaign_storyboard()
        shot = next(item for item in storyboard.shots if item.id == "water-feature")
        storyboard = Storyboard(
            id=storyboard.id,
            campaign_id=storyboard.campaign_id,
            shots=[shot],
        )
        product = Product(
            id="soundcore-com-group-products-a31x1021",
            name="Select 4 Go",
            brand="Soundcore",
            category="Portable Bluetooth Speaker",
        )
        artifact = self.artifact.model_copy(
            update={
                "product_id": product.id,
                "campaign_id": campaign.id,
                "storyboard_id": storyboard.id,
                "shot_id": shot.id,
            },
            deep=True,
        )
        context = create_video_shot_review_context(artifact, storyboard)
        history = create_video_shot_review_history(artifact, storyboard)
        history = append_video_shot_review(
            history,
            review(
                context,
                "soundcore-video-review",
                usable_ranges=(UsableVideoRange(start_ms=1200, end_ms=4100),),
            ),
            artifact,
            storyboard,
        )
        handoff = build_editing_source_handoff(artifact, storyboard, history)

        self.assertEqual(context.raw_duration_ms, 8000)
        self.assertEqual(context.intended_edit_duration_ms, 2500)
        self.assertEqual(handoff.approved_usable_ranges[0].duration_ms, 2900)
        self.assertTrue(handoff.runtime_video.has_audio)
        self.assertEqual(
            handoff.provider_audio_disposition,
            "discard_before_editing",
        )


if __name__ == "__main__":
    unittest.main()
