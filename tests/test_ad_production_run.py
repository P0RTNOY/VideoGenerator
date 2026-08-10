import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from ad_production_run import (
    AdProductionRun,
    AdProductionRunPersistenceError,
    AdProductionRunRevisionError,
    RunDocumentReference,
    RunOperationDiagnostic,
    RunProductInput,
    ad_production_run_lock,
    ad_production_run_path,
    attach_run_reference,
    attach_shot_reference,
    create_ad_production_run,
    initialize_ad_production_run,
    load_ad_production_run,
    record_operation_diagnostic,
    save_ad_production_run,
    set_product_input,
)


FIXED_TIME = datetime(2026, 8, 10, 19, 0, tzinfo=timezone.utc)


def reference(identity: str, path: str) -> RunDocumentReference:
    return RunDocumentReference(id=identity, path=path)


class AdProductionRunModelTests(unittest.TestCase):
    def test_project_is_one_run_with_caller_slug_revision_and_no_status(self):
        run = create_ad_production_run("soundcore-summer-ad")

        self.assertEqual(run.project_id, "soundcore-summer-ad")
        self.assertEqual(run.revision, 0)
        self.assertNotIn("status", AdProductionRun.model_fields)
        self.assertNotIn("state", AdProductionRun.model_fields)
        self.assertEqual(run.shots, ())
        with self.assertRaises(ValidationError):
            run.revision = 2
        with self.assertRaises(ValidationError):
            AdProductionRun.model_validate({**run.model_dump(), "status": "complete"})

    def test_project_id_is_a_strict_lowercase_caller_slug(self):
        for invalid in (
            "",
            "Soundcore",
            "soundcore_ad",
            "-soundcore",
            "soundcore-",
            "soundcore--ad",
            "../soundcore",
            "a" * 81,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                create_ad_production_run(invalid)

    def test_document_references_accept_only_normalized_workspace_relative_paths(self):
        valid = reference("product-1", "products/product-1/product.json")
        self.assertEqual(valid.path, "products/product-1/product.json")
        for invalid in (
            "/tmp/product.json",
            "../product.json",
            "products/../product.json",
            "products\\product.json",
            "file:///tmp/product.json",
            "./product.json",
            "products//product.json",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                reference("product-1", invalid)

    def test_run_contains_references_not_domain_copies_or_runtime_paths(self):
        run = create_ad_production_run(
            "soundcore-ad",
            product_input=RunProductInput(
                kind="product_url",
                value="https://example.test/products/select-4-go",
            ),
        )
        run = attach_run_reference(
            run,
            "product",
            reference("product-1", "products/product-1/product.json"),
        )
        serialized = run.model_dump_json()
        self.assertIn('"product"', serialized)
        for forbidden in (
            "local_path",
            "absolute_path",
            "claims",
            "storyboard_shots",
            "review_context",
            "reviewer_actor_id",
            "media_bytes",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_reference_updates_clear_only_downstream_references(self):
        run = self.complete_reference_shape()
        changed_campaign = attach_run_reference(
            run,
            "campaign",
            reference("campaign-2", "projects/soundcore-ad/campaign-2.json"),
        )
        self.assertEqual(changed_campaign.product, run.product)
        self.assertEqual(
            changed_campaign.original_asset_manifest,
            run.original_asset_manifest,
        )
        self.assertIsNone(changed_campaign.storyboard)
        self.assertIsNone(changed_campaign.asset_plan)
        self.assertEqual(changed_campaign.shots, ())
        self.assertIsNone(changed_campaign.editing_plan)

        changed_manifest = attach_run_reference(
            run,
            "original_asset_manifest",
            reference("manifest-2", "products/product-1/assets/manifest-2.json"),
        )
        self.assertEqual(changed_manifest.storyboard, run.storyboard)
        self.assertIsNone(changed_manifest.derived_asset_manifest)
        self.assertIsNone(changed_manifest.asset_plan)
        self.assertTrue(all(item.hero_frame_artifact is None for item in changed_manifest.shots))

    def test_shot_invalidation_is_local_until_global_editing_references(self):
        run = self.complete_reference_shape()
        original_second = run.shots[1]
        changed = attach_shot_reference(
            run,
            "hook",
            "hero_frame_artifact",
            reference("hero:new", "products/product-1/assets/hero-new.json"),
        )
        hook = changed.shots[0]
        self.assertEqual(hook.hero_frame_artifact.id, "hero:new")
        self.assertIsNone(hook.hero_frame_review_history)
        self.assertIsNone(hook.video_intent)
        self.assertIsNone(hook.video_artifact)
        self.assertIsNone(hook.video_review_history)
        self.assertEqual(changed.shots[1], original_second)
        self.assertIsNone(changed.editing_plan)
        self.assertIsNone(changed.editing_artifact)
        self.assertIsNone(changed.final_review_history)

    def test_upstream_changes_never_delete_referenced_files(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "artifact.bin"
            sentinel.write_bytes(b"keep")
            run = self.complete_reference_shape()
            set_product_input(
                run,
                RunProductInput(kind="product_name", value="Another Product"),
            )
            self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_only_latest_operation_diagnostic_is_retained(self):
        run = create_ad_production_run("soundcore-ad")
        first = RunOperationDiagnostic(
            action="research",
            code="network_error",
            message="Timed out",
            recorded_at=FIXED_TIME,
        )
        second = RunOperationDiagnostic(
            action="generate_hero",
            shot_id="hook",
            code="provider_error",
            message="Provider rejected request",
            recorded_at=FIXED_TIME,
        )
        run = record_operation_diagnostic(run, first)
        run = record_operation_diagnostic(run, second)
        self.assertEqual(run.latest_operation_diagnostic, second)
        self.assertNotIn("diagnostics", AdProductionRun.model_fields)

    def complete_reference_shape(self) -> AdProductionRun:
        run = create_ad_production_run("soundcore-ad")
        run = set_product_input(
            run,
            RunProductInput(kind="product_url", value="https://example.test/product"),
        )
        for kind, item in (
            ("product", reference("product-1", "products/product-1/product.json")),
            ("campaign", reference("campaign-1", "projects/soundcore-ad/campaign.json")),
        ):
            run = attach_run_reference(run, kind, item)
        run = attach_run_reference(
            run,
            "storyboard",
            reference("storyboard-1", "projects/soundcore-ad/storyboard.json"),
            shot_ids=("hook", "feature"),
        )
        run = attach_run_reference(
            run,
            "original_asset_manifest",
            reference("manifest-1", "products/product-1/assets/manifest.json"),
        )
        run = attach_run_reference(
            run,
            "derived_asset_manifest",
            reference("derived-1", "products/product-1/assets/derived.json"),
        )
        run = attach_run_reference(
            run,
            "asset_plan",
            reference("asset-plan-1", "projects/soundcore-ad/asset-plan.json"),
        )
        for shot_id in ("hook", "feature"):
            for kind, identity in (
                ("hero_frame_artifact", "hero:" + shot_id),
                ("hero_frame_review_history", "hero-review:" + shot_id),
                ("video_intent", "intent:" + shot_id),
                ("video_artifact", "video:" + shot_id),
                ("video_review_history", "video-review:" + shot_id),
            ):
                run = attach_shot_reference(
                    run,
                    shot_id,
                    kind,
                    reference(
                        identity,
                        f"products/product-1/{shot_id}/{kind}.json",
                    ),
                )
        for kind, item in (
            ("editing_plan", reference("editing-plan:1", "products/product-1/editing-plan.json")),
            ("editing_artifact", reference("edited-ad:1", "products/product-1/edited-ad.json")),
            ("final_review_history", reference("final-review:1", "products/product-1/final-review.json")),
        ):
            run = attach_run_reference(run, kind, item)
        return run


class AdProductionRunPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_initialize_creates_exact_workspace_layout_and_human_json(self):
        run = initialize_ad_production_run(self.root, "soundcore-ad")
        path = self.root / "projects" / "soundcore-ad" / "run.json"
        self.assertEqual(ad_production_run_path(self.root, "soundcore-ad"), path)
        self.assertTrue(path.is_file())
        self.assertEqual(load_ad_production_run(self.root, "soundcore-ad"), run)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertIn("\n  \"schema_version\"", text)
        self.assertNotIn(str(self.root), text)
        self.assertEqual(json.loads(text)["revision"], 0)

    def test_save_is_monotonic_optimistic_and_idempotent(self):
        run = initialize_ad_production_run(self.root, "soundcore-ad")
        changed = set_product_input(
            run,
            RunProductInput(kind="product_name", value="Soundcore Select 4 Go"),
        )
        saved = save_ad_production_run(
            changed,
            self.root,
            "soundcore-ad",
            expected_revision=0,
        )
        self.assertEqual(saved.revision, 1)
        same = save_ad_production_run(
            saved,
            self.root,
            "soundcore-ad",
            expected_revision=1,
        )
        self.assertEqual(same.revision, 1)
        with self.assertRaises(AdProductionRunRevisionError):
            save_ad_production_run(
                changed,
                self.root,
                "soundcore-ad",
                expected_revision=0,
            )

    def test_atomic_replace_failure_preserves_previous_run(self):
        run = initialize_ad_production_run(self.root, "soundcore-ad")
        changed = set_product_input(
            run,
            RunProductInput(kind="product_name", value="Soundcore Select 4 Go"),
        )
        with patch("ad_production_run.os.replace", side_effect=OSError("blocked")):
            with self.assertRaises(AdProductionRunPersistenceError):
                save_ad_production_run(
                    changed,
                    self.root,
                    "soundcore-ad",
                    expected_revision=0,
                )
        self.assertEqual(load_ad_production_run(self.root, "soundcore-ad"), run)

    def test_lock_blocks_other_access_until_context_releases(self):
        initialize_ad_production_run(self.root, "soundcore-ad")
        started = threading.Event()
        completed = threading.Event()

        def load_in_thread():
            started.set()
            load_ad_production_run(self.root, "soundcore-ad")
            completed.set()

        with ad_production_run_lock(self.root, "soundcore-ad", exclusive=True):
            worker = threading.Thread(target=load_in_thread)
            worker.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.05)
            self.assertFalse(completed.is_set())
        worker.join(1)
        self.assertTrue(completed.is_set())

    def test_initialization_collision_symlink_and_bypassed_model_fail_closed(self):
        run = initialize_ad_production_run(self.root, "soundcore-ad")
        with self.assertRaises(AdProductionRunPersistenceError):
            initialize_ad_production_run(self.root, "soundcore-ad")
        bypassed = run.model_copy(update={"revision": -1})
        with self.assertRaises(AdProductionRunPersistenceError):
            save_ad_production_run(
                bypassed,
                self.root,
                "soundcore-ad",
                expected_revision=0,
            )

        other = self.root / "outside.json"
        other.write_text("{}", encoding="utf-8")
        run_path = ad_production_run_path(self.root, "linked-ad")
        run_path.parent.mkdir(parents=True)
        os.symlink(other, run_path)
        with self.assertRaises(AdProductionRunPersistenceError):
            initialize_ad_production_run(self.root, "linked-ad")

    def test_save_cannot_rebind_a_candidate_to_another_project_workspace(self):
        original = initialize_ad_production_run(self.root, "first-ad")
        other = initialize_ad_production_run(self.root, "other-ad")
        rebound = original.model_copy(update={"project_id": "other-ad"})

        with self.assertRaises(AdProductionRunPersistenceError):
            save_ad_production_run(
                rebound,
                self.root,
                "first-ad",
                expected_revision=0,
            )
        self.assertEqual(load_ad_production_run(self.root, "other-ad"), other)


if __name__ == "__main__":
    unittest.main()
