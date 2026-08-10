import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from demo_campaign_storyboard import build_soundcore_campaign_storyboard
from hero_frame_artifact import HeroFrameArtifact
from hero_frame_review import (
    HeroFrameFidelityChecklist,
    HeroFrameReview,
    HeroFrameReviewArtifactError,
    HeroFrameReviewContext,
    HeroFrameReviewError,
    HeroFrameReviewHistory,
    HeroFrameReviewPersistenceError,
    append_hero_frame_review,
    create_hero_frame_review_context,
    create_hero_frame_review_history,
    load_hero_frame_review_history,
    resolve_hero_frame_review_state,
    review_history_path,
    save_hero_frame_review_history,
)
from product import Product


FIXED_TIME = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def png_bytes(color=(20, 100, 180)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (768, 1376), color).save(output, format="PNG")
    return output.getvalue()


def make_artifact(
    root: Path,
    *,
    product_id="example-product",
    campaign_id="campaign-1",
    storyboard_id="storyboard-1",
    asset_plan_id="asset-plan-1",
    shot_id="hook",
    request_fingerprint="a" * 64,
    color=(20, 100, 180),
    filename="hero.png",
) -> HeroFrameArtifact:
    data = png_bytes(color)
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    return HeroFrameArtifact(
        id=f"hero-frame:{sha256}",
        product_id=product_id,
        campaign_id=campaign_id,
        storyboard_id=storyboard_id,
        asset_plan_id=asset_plan_id,
        shot_id=shot_id,
        source_request_fingerprint=request_fingerprint,
        local_path=str(path),
        sha256=sha256,
        width=768,
        height=1376,
        format="PNG",
        created_at=FIXED_TIME,
    )


def passing_checklist(**updates) -> HeroFrameFidelityChecklist:
    values = {
        "geometry_and_proportions": "pass",
        "color_variant_and_materials": "pass",
        "controls_buttons_and_ports": "not_applicable",
        "seams_and_panel_boundaries": "pass",
        "logos_markings_and_labels": "pass",
        "no_invented_or_missing_construction": "pass",
        "no_added_advertising_text": "pass",
        "scene_and_framing_usable": "pass",
    }
    values.update(updates)
    return HeroFrameFidelityChecklist(**values)


def review(
    context: HeroFrameReviewContext,
    review_id: str,
    decision="approved_for_video_start_image",
    *,
    checklist=None,
    rejection_reasons=None,
    notes=None,
    reviewed_at=FIXED_TIME,
    reviewer_actor_id="owner:portnoy",
) -> HeroFrameReview:
    return HeroFrameReview(
        review_id=review_id,
        review_context_id=context.id,
        reviewer_actor_id=reviewer_actor_id,
        decision=decision,
        reviewed_at=reviewed_at,
        checklist=checklist or passing_checklist(),
        rejection_reasons=rejection_reasons or [],
        notes=notes,
    )


class HeroFrameReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.artifact = make_artifact(self.root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_context_is_deterministic_complete_and_path_independent(self):
        original = create_hero_frame_review_context(self.artifact)
        moved_path = self.root / "moved" / "hero.png"
        moved_path.parent.mkdir()
        moved_path.write_bytes(Path(self.artifact.local_path).read_bytes())
        moved = self.artifact.model_copy(
            update={"local_path": str(moved_path)},
            deep=True,
        )
        moved_context = create_hero_frame_review_context(moved)

        self.assertEqual(original, moved_context)
        self.assertEqual(
            original.id,
            f"hero-frame-review-context:{original.id.rsplit(':', 1)[1]}",
        )
        self.assertEqual(original.artifact_id, self.artifact.id)
        self.assertEqual(original.artifact_sha256, self.artifact.sha256)
        self.assertEqual(
            original.source_request_fingerprint,
            self.artifact.source_request_fingerprint,
        )
        self.assertEqual(original.product_id, "example-product")
        self.assertEqual(original.campaign_id, "campaign-1")
        self.assertEqual(original.storyboard_id, "storyboard-1")
        self.assertEqual(original.asset_plan_id, "asset-plan-1")
        self.assertEqual(original.shot_id, "hook")
        self.assertEqual((original.width, original.height), (768, 1376))
        self.assertEqual(original.format, "PNG")
        serialized = original.model_dump_json()
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("created_at", serialized)

    def test_each_meaningful_binding_changes_context_identity(self):
        original = create_hero_frame_review_context(self.artifact).id
        mutations = {
            "sha": make_artifact(self.root, color=(1, 2, 3), filename="new.png"),
            "request": self.artifact.model_copy(
                update={"source_request_fingerprint": "b" * 64}, deep=True
            ),
            "product": self.artifact.model_copy(
                update={"product_id": "other-product"}, deep=True
            ),
            "campaign": self.artifact.model_copy(
                update={"campaign_id": "campaign-2"}, deep=True
            ),
            "storyboard": self.artifact.model_copy(
                update={"storyboard_id": "storyboard-2"}, deep=True
            ),
            "plan": self.artifact.model_copy(
                update={"asset_plan_id": "asset-plan-2"}, deep=True
            ),
            "shot": self.artifact.model_copy(
                update={"shot_id": "feature"}, deep=True
            ),
        }
        for name, artifact in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    original,
                    create_hero_frame_review_context(artifact).id,
                )

    def test_context_rejects_stale_or_structurally_bypassed_artifact(self):
        Path(self.artifact.local_path).write_bytes(b"changed")
        with self.assertRaises(HeroFrameReviewArtifactError):
            create_hero_frame_review_context(self.artifact)

        bypassed = self.artifact.model_copy(
            update={"id": "not-content-addressed"},
            deep=True,
        )
        with self.assertRaises(HeroFrameReviewArtifactError):
            create_hero_frame_review_context(bypassed)

    def test_context_schema_rejects_unknown_or_inconsistent_identity(self):
        context = create_hero_frame_review_context(self.artifact)
        data = context.model_dump()
        data["unexpected"] = True
        with self.assertRaises(ValidationError):
            HeroFrameReviewContext.model_validate(data)
        data = context.model_dump()
        data["id"] = "hero-frame-review-context:" + "f" * 64
        with self.assertRaises(ValidationError):
            HeroFrameReviewContext.model_validate(data)


class HeroFrameReviewRecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.artifact = make_artifact(self.root)
        self.context = create_hero_frame_review_context(self.artifact)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_approved_review_accepts_pass_and_not_applicable(self):
        item = review(self.context, "review-1")

        self.assertEqual(item.decision, "approved_for_video_start_image")
        self.assertEqual(item.checklist.controls_buttons_and_ports, "not_applicable")
        self.assertEqual(item.reviewer_actor_id, "owner:portnoy")

    def test_approved_review_rejects_failures_or_rejection_reasons(self):
        with self.assertRaises(ValidationError):
            review(
                self.context,
                "review-failed-check",
                checklist=passing_checklist(geometry_and_proportions="fail"),
            )
        with self.assertRaises(ValidationError):
            review(
                self.context,
                "review-contradictory-reason",
                rejection_reasons=["geometry_mismatch"],
            )

    def test_rejected_review_requires_reason_and_consistent_failure(self):
        with self.assertRaises(ValidationError):
            review(
                self.context,
                "review-no-reason",
                decision="rejected_for_video_start_image",
            )
        with self.assertRaises(ValidationError):
            review(
                self.context,
                "review-no-failure",
                decision="rejected_for_video_start_image",
                rejection_reasons=["geometry_mismatch"],
            )

        rejected = review(
            self.context,
            "review-rejected",
            decision="rejected_for_video_start_image",
            checklist=passing_checklist(logos_markings_and_labels="fail"),
            rejection_reasons=["logo_or_marking_mismatch"],
        )
        self.assertEqual(rejected.rejection_reasons, ("logo_or_marking_mismatch",))

    def test_scene_rejection_is_valid_without_other_checklist_failure(self):
        rejected = review(
            self.context,
            "review-scene",
            decision="rejected_for_video_start_image",
            checklist=passing_checklist(scene_and_framing_usable="not_applicable"),
            rejection_reasons=["scene_or_framing_unusable"],
        )
        self.assertEqual(rejected.decision, "rejected_for_video_start_image")

    def test_other_rejection_requires_nonempty_notes(self):
        for notes in (None, "   "):
            with self.subTest(notes=notes):
                with self.assertRaises(ValidationError):
                    review(
                        self.context,
                        "review-other",
                        decision="rejected_for_video_start_image",
                        checklist=passing_checklist(
                            no_invented_or_missing_construction="fail"
                        ),
                        rejection_reasons=["other"],
                        notes=notes,
                    )

        item = review(
            self.context,
            "review-other-explained",
            decision="rejected_for_video_start_image",
            checklist=passing_checklist(
                no_invented_or_missing_construction="fail"
            ),
            rejection_reasons=["other"],
            notes="Unexpected grille construction.",
        )
        self.assertEqual(item.notes, "Unexpected grille construction.")

    def test_checklist_is_complete_strict_and_rejects_unknown_fields(self):
        data = passing_checklist().model_dump()
        del data["geometry_and_proportions"]
        with self.assertRaises(ValidationError):
            HeroFrameFidelityChecklist.model_validate(data)
        data = passing_checklist().model_dump()
        data["geometry_and_proportions"] = "maybe"
        with self.assertRaises(ValidationError):
            HeroFrameFidelityChecklist.model_validate(data)
        data = passing_checklist().model_dump()
        data["legal_approval"] = "pass"
        with self.assertRaises(ValidationError):
            HeroFrameFidelityChecklist.model_validate(data)

    def test_actor_timestamp_and_record_schema_are_strict(self):
        with self.assertRaises(ValidationError):
            review(self.context, "review-blank-actor", reviewer_actor_id=" ")
        with self.assertRaises(ValidationError):
            review(
                self.context,
                "review-naive-time",
                reviewed_at=datetime(2026, 8, 10, 15, 0),
            )
        offset = timezone(timedelta(hours=3))
        item = review(
            self.context,
            "review-offset-time",
            reviewed_at=datetime(2026, 8, 10, 18, 0, tzinfo=offset),
        )
        self.assertEqual(item.reviewed_at, FIXED_TIME)
        data = item.model_dump()
        data["rights_approved"] = True
        with self.assertRaises(ValidationError):
            HeroFrameReview.model_validate(data)


class HeroFrameReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.artifact = make_artifact(self.root)
        self.history = create_hero_frame_review_history(self.artifact)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def append(self, item):
        self.history = append_hero_frame_review(
            self.history,
            item,
            self.artifact,
        )

    def test_empty_history_resolves_pending_and_does_not_persist_pending(self):
        self.assertEqual(
            resolve_hero_frame_review_state(self.history, self.artifact),
            "pending_review",
        )
        self.assertNotIn("pending_review", self.history.model_dump_json())

    def test_latest_exact_context_review_determines_current_state(self):
        context = self.history.review_context
        sequences = [
            (
                [review(context, "approved")],
                "approved_for_video_start_image",
            ),
            (
                [
                    review(context, "approved-first"),
                    review(
                        context,
                        "rejected-second",
                        decision="rejected_for_video_start_image",
                        checklist=passing_checklist(
                            geometry_and_proportions="fail"
                        ),
                        rejection_reasons=["geometry_mismatch"],
                    ),
                ],
                "rejected_for_video_start_image",
            ),
            (
                [
                    review(
                        context,
                        "rejected-first",
                        decision="rejected_for_video_start_image",
                        checklist=passing_checklist(
                            geometry_and_proportions="fail"
                        ),
                        rejection_reasons=["geometry_mismatch"],
                    ),
                    review(context, "approved-second"),
                ],
                "approved_for_video_start_image",
            ),
        ]
        for items, expected in sequences:
            with self.subTest(expected=expected):
                history = create_hero_frame_review_history(self.artifact)
                for item in items:
                    history = append_hero_frame_review(
                        history,
                        item,
                        self.artifact,
                    )
                self.assertEqual(
                    resolve_hero_frame_review_state(history, self.artifact),
                    expected,
                )

    def test_stale_contexts_do_not_inherit_approval(self):
        approved = append_hero_frame_review(
            self.history,
            review(self.history.review_context, "approved"),
            self.artifact,
        )
        new_binary = make_artifact(
            self.root,
            color=(1, 2, 3),
            filename="regenerated.png",
        )
        changed_request = self.artifact.model_copy(
            update={"source_request_fingerprint": "b" * 64},
            deep=True,
        )
        changed_shot = self.artifact.model_copy(
            update={"shot_id": "feature"},
            deep=True,
        )
        for current in (new_binary, changed_request, changed_shot):
            with self.subTest(current=current):
                self.assertEqual(
                    resolve_hero_frame_review_state(approved, current),
                    "pending_review",
                )

    def test_identical_binary_in_another_shot_has_separate_context(self):
        other_shot = self.artifact.model_copy(
            update={"shot_id": "water-feature"},
            deep=True,
        )
        approved = append_hero_frame_review(
            self.history,
            review(self.history.review_context, "approved"),
            self.artifact,
        )

        self.assertEqual(other_shot.sha256, self.artifact.sha256)
        self.assertNotEqual(
            create_hero_frame_review_context(other_shot).id,
            self.history.review_context.id,
        )
        self.assertEqual(
            resolve_hero_frame_review_state(approved, other_shot),
            "pending_review",
        )

    def test_append_preserves_order_isolated_inputs_and_rejects_duplicates(self):
        context = self.history.review_context
        first = review(context, "review-1")
        original_history = self.history.model_dump()
        original_review = first.model_dump()
        updated = append_hero_frame_review(self.history, first, self.artifact)
        second = review(context, "review-2")
        updated = append_hero_frame_review(updated, second, self.artifact)

        self.assertEqual(self.history.model_dump(), original_history)
        self.assertEqual(first.model_dump(), original_review)
        self.assertEqual(
            [item.review_id for item in updated.reviews],
            ["review-1", "review-2"],
        )
        with self.assertRaises(HeroFrameReviewError):
            append_hero_frame_review(updated, first, self.artifact)

    def test_history_and_existing_review_records_are_immutable(self):
        updated = append_hero_frame_review(
            self.history,
            review(self.history.review_context, "review-1"),
            self.artifact,
        )

        with self.assertRaises(ValidationError):
            updated.reviews[0].decision = "rejected_for_video_start_image"
        with self.assertRaises(ValidationError):
            updated.reviews[0].checklist.geometry_and_proportions = "fail"
        with self.assertRaises(AttributeError):
            updated.reviews.append(review(updated.review_context, "review-2"))

    def test_append_rejects_wrong_context_and_revalidates_unchecked_models(self):
        wrong = review(
            self.history.review_context,
            "wrong-context",
        ).model_copy(update={"review_context_id": "other"}, deep=True)
        with self.assertRaises(HeroFrameReviewError):
            append_hero_frame_review(self.history, wrong, self.artifact)

        bypassed_history = self.history.model_copy(
            update={"schema_version": 2},
            deep=True,
        )
        with self.assertRaises(HeroFrameReviewError):
            append_hero_frame_review(
                bypassed_history,
                review(self.history.review_context, "review-valid"),
                self.artifact,
            )

    def test_append_rechecks_current_artifact_before_accepting_review(self):
        Path(self.artifact.local_path).write_bytes(b"changed")

        with self.assertRaises(HeroFrameReviewArtifactError):
            append_hero_frame_review(
                self.history,
                review(self.history.review_context, "review-1"),
                self.artifact,
            )

    def test_history_round_trip_is_strict(self):
        self.append(review(self.history.review_context, "review-1"))
        restored = HeroFrameReviewHistory.model_validate_json(
            self.history.model_dump_json()
        )
        self.assertEqual(restored, self.history)
        data = self.history.model_dump()
        data["unexpected"] = True
        with self.assertRaises(ValidationError):
            HeroFrameReviewHistory.model_validate(data)


class HeroFrameReviewPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.artifact = make_artifact(self.root / "runtime")
        self.history = create_hero_frame_review_history(self.artifact)
        self.storage_root = self.root / "products"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_storage_is_product_scoped_and_uses_only_context_hash_filename(self):
        path = review_history_path(self.history, self.storage_root)
        context_hash = self.history.review_context.id.rsplit(":", 1)[1]

        self.assertEqual(
            path,
            self.storage_root
            / "example-product"
            / "reviews"
            / "hero-frames"
            / f"{context_hash}.json",
        )

    def test_save_and_load_are_human_readable_path_free_and_strict(self):
        updated = append_hero_frame_review(
            self.history,
            review(self.history.review_context, "review-1"),
            self.artifact,
        )
        path = save_hero_frame_review_history(updated, self.storage_root)
        raw = path.read_text(encoding="utf-8")
        loaded = load_hero_frame_review_history(
            self.artifact,
            self.storage_root,
        )

        self.assertEqual(loaded, updated)
        self.assertTrue(raw.endswith("\n"))
        self.assertIn("\n  \"schema_version\"", raw)
        self.assertNotIn(str(self.root / "runtime"), raw)
        for forbidden in (
            "provider_prompt",
            "api_response",
            "image_bytes",
            "commercial_rights",
            "legal_approval",
        ):
            self.assertNotIn(forbidden, raw)

    def test_missing_file_loads_new_pending_history(self):
        loaded = load_hero_frame_review_history(
            self.artifact,
            self.storage_root,
        )
        self.assertEqual(loaded.reviews, ())
        self.assertEqual(
            resolve_hero_frame_review_state(loaded, self.artifact),
            "pending_review",
        )

    def test_persistence_refuses_history_rewrite_or_truncation(self):
        first = append_hero_frame_review(
            self.history,
            review(self.history.review_context, "review-1"),
            self.artifact,
        )
        save_hero_frame_review_history(first, self.storage_root)
        with self.assertRaises(HeroFrameReviewPersistenceError):
            save_hero_frame_review_history(self.history, self.storage_root)

        changed = deepcopy(first.model_dump())
        changed["reviews"][0]["reviewer_actor_id"] = "rewritten"
        with self.assertRaises(HeroFrameReviewPersistenceError):
            save_hero_frame_review_history(
                HeroFrameReviewHistory.model_validate(changed),
                self.storage_root,
            )


class HeroFrameReviewSoundcoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.campaign, self.storyboard = build_soundcore_campaign_storyboard()
        self.product = Product(
            id="soundcore-com-group-products-a31x1021",
            name="Select 4 Go",
            brand="Soundcore",
            category="Portable Bluetooth Speaker",
        )
        self.artifact = make_artifact(
            self.root,
            product_id=self.product.id,
            campaign_id=self.campaign.id,
            storyboard_id=self.storyboard.id,
            asset_plan_id="soundcore-select-4-go-summer-assets",
            shot_id="water-feature",
            request_fingerprint="c" * 64,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_soundcore_rejection_then_explicit_later_approval_preserves_history(self):
        inputs_before = (
            self.product.model_dump(),
            self.campaign.model_dump(),
            self.storyboard.model_dump(),
            self.artifact.model_dump(),
        )
        history = create_hero_frame_review_history(self.artifact)
        rejected = review(
            history.review_context,
            "soundcore-review-1",
            decision="rejected_for_video_start_image",
            checklist=passing_checklist(
                controls_buttons_and_ports="fail",
                logos_markings_and_labels="fail",
                seams_and_panel_boundaries="fail",
            ),
            rejection_reasons=[
                "controls_or_ports_mismatch",
                "panel_or_seam_mismatch",
                "logo_or_marking_mismatch",
            ],
            notes="Panel, logo, and button fidelity are wrong.",
        )
        history = append_hero_frame_review(history, rejected, self.artifact)
        approved = review(
            history.review_context,
            "soundcore-review-2",
            reviewed_at=FIXED_TIME + timedelta(minutes=5),
        )
        history = append_hero_frame_review(history, approved, self.artifact)

        self.assertEqual(len(history.reviews), 2)
        self.assertEqual(
            resolve_hero_frame_review_state(history, self.artifact),
            "approved_for_video_start_image",
        )
        self.assertEqual(
            inputs_before,
            (
                self.product.model_dump(),
                self.campaign.model_dump(),
                self.storyboard.model_dump(),
                self.artifact.model_dump(),
            ),
        )

    def test_soundcore_regeneration_is_pending_without_provider_activity(self):
        history = create_hero_frame_review_history(self.artifact)
        history = append_hero_frame_review(
            history,
            review(history.review_context, "soundcore-approved"),
            self.artifact,
        )
        regenerated = make_artifact(
            self.root,
            product_id=self.product.id,
            campaign_id=self.campaign.id,
            storyboard_id=self.storyboard.id,
            asset_plan_id="soundcore-select-4-go-summer-assets",
            shot_id="water-feature",
            request_fingerprint="c" * 64,
            color=(200, 100, 20),
            filename="regenerated.png",
        )

        self.assertEqual(
            resolve_hero_frame_review_state(history, regenerated),
            "pending_review",
        )


if __name__ == "__main__":
    unittest.main()
