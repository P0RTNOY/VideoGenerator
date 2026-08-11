import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from action_confirmation import (
    ACTION_PREVIEW_VALIDITY,
    ActionAttemptReceipt,
    ActionConfirmationError,
    ActionFailureDiagnostic,
    action_attempt_receipt_path,
    action_preview_path,
    claim_action_preview,
    create_action_preview,
    load_action_attempt_receipt,
    load_action_preview,
    save_action_attempt_receipt,
    save_action_preview,
)
from ad_production_run import RunDocumentReference


NOW = datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)
REQUEST = "1" * 64
OPTIONS = "2" * 64


class ActionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        (self.workspace / "projects" / "speaker-ad").mkdir(parents=True)

    def tearDown(self):
        self.directory.cleanup()

    def make_preview(self, **changes):
        values = {
            "project_id": "speaker-ad",
            "run_revision": 7,
            "action": "generate_hero",
            "request_fingerprint": REQUEST,
            "provider": "google-gemini",
            "model": "gemini-image",
            "options_fingerprint": OPTIONS,
            "requested_output_count": 1,
            "cost_quote_status": "quoted",
            "quoted_cost_usd": 0.04,
            "owner_cost_ceiling_usd": 0.10,
            "created_at": NOW,
        }
        values.update(changes)
        return create_action_preview(**values)

    def claim(self, preview, **changes):
        save_action_preview(preview, self.workspace)
        values = {
            "project_id": preview.project_id,
            "run_revision": preview.run_revision,
            "action": preview.action,
            "request_fingerprint": preview.request_fingerprint,
            "provider": preview.provider,
            "model": preview.model,
            "options_fingerprint": preview.options_fingerprint,
            "requested_output_count": preview.requested_output_count,
            "attempt_id": "attempt-one",
            "now": NOW + timedelta(minutes=1),
        }
        values.update(changes)
        return claim_action_preview(
            self.workspace,
            preview.id,
            **values,
        )

    def test_preview_is_strict_content_addressed_and_deterministic(self):
        first = self.make_preview()
        second = self.make_preview()
        self.assertEqual(first, second)
        self.assertTrue(first.id.startswith("action-preview:"))
        self.assertEqual(len(first.id.removeprefix("action-preview:")), 64)
        self.assertEqual(first.expires_at - first.created_at, ACTION_PREVIEW_VALIDITY)
        with self.assertRaises(ValidationError):
            type(first).model_validate({**first.model_dump(), "unexpected": True})
        with self.assertRaises(ValidationError):
            type(first).model_validate({**first.model_dump(), "id": "action-preview:" + "0" * 64})

    def test_preview_json_round_trip_is_human_readable_and_path_free(self):
        preview = self.make_preview()
        path = save_action_preview(preview, self.workspace)
        self.assertEqual(
            path,
            action_preview_path(self.workspace, preview.project_id, preview.id),
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("\n  \"project_id\"", text)
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn(str(self.workspace), text)
        self.assertEqual(load_action_preview(path), preview)
        with self.assertRaises(ActionConfirmationError):
            save_action_preview(preview, self.workspace)

    def test_preview_publish_failure_leaves_no_partial_final_or_temp_file(self):
        preview = self.make_preview()
        path = action_preview_path(self.workspace, preview.project_id, preview.id)
        with patch("action_confirmation.os.link", side_effect=OSError("injected")):
            with self.assertRaises(ActionConfirmationError):
                save_action_preview(preview, self.workspace)
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.glob(".action-*.tmp")), [])

    def test_preview_exactly_binds_project_revision_request_provider_model_options_and_count(self):
        preview = self.make_preview()
        mismatch_cases = (
            {"project_id": "other-ad"},
            {"run_revision": 8},
            {"action": "generate_video"},
            {"request_fingerprint": "3" * 64},
            {"provider": "other-provider"},
            {"model": "other-model"},
            {"options_fingerprint": "4" * 64},
            {"requested_output_count": 2},
        )
        for index, changes in enumerate(mismatch_cases):
            with self.subTest(changes=changes):
                isolated = self.workspace / f"case-{index}"
                (isolated / "projects" / "speaker-ad").mkdir(parents=True)
                save_action_preview(preview, isolated)
                with self.assertRaises(ActionConfirmationError):
                    claim_action_preview(
                        isolated,
                        preview.id,
                        project_id=changes.get("project_id", preview.project_id),
                        run_revision=changes.get("run_revision", preview.run_revision),
                        action=changes.get("action", preview.action),
                        request_fingerprint=changes.get(
                            "request_fingerprint", preview.request_fingerprint
                        ),
                        provider=changes.get("provider", preview.provider),
                        model=changes.get("model", preview.model),
                        options_fingerprint=changes.get(
                            "options_fingerprint", preview.options_fingerprint
                        ),
                        requested_output_count=changes.get(
                            "requested_output_count", preview.requested_output_count
                        ),
                        attempt_id="attempt-one",
                        now=NOW + timedelta(minutes=1),
                    )

    def test_exact_token_and_unexpired_preview_are_required(self):
        preview = self.make_preview()
        save_action_preview(preview, self.workspace)
        with self.assertRaises(ActionConfirmationError):
            claim_action_preview(
                self.workspace,
                "yes",
                project_id="speaker-ad",
                run_revision=7,
                action="generate_hero",
                request_fingerprint=REQUEST,
                provider="google-gemini",
                model="gemini-image",
                options_fingerprint=OPTIONS,
                requested_output_count=1,
                attempt_id="attempt-one",
                now=NOW,
            )
        with self.assertRaises(ActionConfirmationError):
            self.claim(preview, now=NOW + timedelta(minutes=30))

    def test_claim_is_one_time_even_after_failed_attempt(self):
        preview = self.make_preview()
        claim = self.claim(preview)
        self.assertEqual(claim.preview_id, preview.id)
        self.assertEqual(claim.attempt_id, "attempt-one")
        with self.assertRaises(ActionConfirmationError):
            self.claim(preview, attempt_id="attempt-two")

    def test_concurrent_duplicate_claim_allows_exactly_one(self):
        preview = self.make_preview()
        save_action_preview(preview, self.workspace)
        outcomes = []

        def worker(attempt_id):
            try:
                claim_action_preview(
                    self.workspace,
                    preview.id,
                    project_id="speaker-ad",
                    run_revision=7,
                    action="generate_hero",
                    request_fingerprint=REQUEST,
                    provider="google-gemini",
                    model="gemini-image",
                    options_fingerprint=OPTIONS,
                    requested_output_count=1,
                    attempt_id=attempt_id,
                    now=NOW + timedelta(minutes=1),
                )
                outcomes.append("claimed")
            except ActionConfirmationError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=worker, args=(f"attempt-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["claimed", "rejected"])

    def test_nested_storage_symlink_cannot_escape_project(self):
        preview = self.make_preview()
        outside = self.workspace / "outside"
        outside.mkdir()
        project = self.workspace / "projects" / "speaker-ad"
        (project / "action-previews").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ActionConfirmationError):
            save_action_preview(preview, self.workspace)
        self.assertEqual(list(outside.iterdir()), [])

    def test_cost_quote_policy_is_explicit_and_never_invented(self):
        under = self.make_preview()
        self.assertEqual(under.quoted_cost_usd, 0.04)
        with self.assertRaises(ActionConfirmationError):
            self.make_preview(quoted_cost_usd=0.11)
        with self.assertRaises(ActionConfirmationError):
            self.make_preview(
                cost_quote_status="unavailable",
                quoted_cost_usd=None,
                allow_unquoted_cost=False,
            )
        unquoted = self.make_preview(
            cost_quote_status="unavailable",
            quoted_cost_usd=None,
            allow_unquoted_cost=True,
            owner_cost_ceiling_usd=0.10,
        )
        self.assertEqual(unquoted.cost_quote_status, "unavailable")
        self.assertIsNone(unquoted.quoted_cost_usd)
        render = self.make_preview(
            action="render_editing_plan",
            provider=None,
            model=None,
            cost_quote_status="not_applicable",
            quoted_cost_usd=None,
            owner_cost_ceiling_usd=None,
        )
        self.assertIsNone(render.quoted_cost_usd)

    def test_creation_does_not_mutate_caller_values(self):
        values = {"created_at": NOW, "request_fingerprint": REQUEST}
        before = deepcopy(values)
        self.make_preview(**values)
        self.assertEqual(values, before)


class ActionAttemptReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        (self.workspace / "projects" / "speaker-ad").mkdir(parents=True)

    def tearDown(self):
        self.directory.cleanup()

    def receipt(self, **changes):
        values = {
            "attempt_id": "attempt-one",
            "project_id": "speaker-ad",
            "action": "generate_hero",
            "preview_id": "action-preview:" + "a" * 64,
            "started_at": NOW,
            "completed_at": NOW + timedelta(seconds=3),
            "status": "succeeded_and_attached",
            "run_revision_at_start": 7,
            "request_fingerprint": REQUEST,
            "cost_quote_status": "unavailable",
            "artifact_reference": RunDocumentReference(
                id="hero-frame:" + "b" * 64,
                path="products/speaker/hero.json",
            ),
            "domain_receipt_reference": RunDocumentReference(
                id="provider-attempt-one",
                path="products/speaker/hero-receipt.json",
            ),
            "attachment_outcome": "attached",
        }
        values.update(changes)
        return ActionAttemptReceipt(**values)

    def test_receipt_is_strict_sanitized_and_contextually_consistent(self):
        receipt = self.receipt()
        dumped = receipt.model_dump()
        for forbidden in ("api_key", "secret", "prompt", "raw_response", "stderr"):
            self.assertNotIn(forbidden, dumped)
        with self.assertRaises(ValidationError):
            ActionAttemptReceipt.model_validate({**dumped, "raw_prompt": "secret"})
        with self.assertRaises(ValidationError):
            self.receipt(preview_id="yes")
        with self.assertRaises(ValidationError):
            self.receipt(status="artifact_created_unattached", attachment_outcome="attached")
        failure = self.receipt(
            status="failed",
            artifact_reference=None,
            domain_receipt_reference=None,
            attachment_outcome="not_attached",
            failure=ActionFailureDiagnostic(code="provider_error", message="Attempt failed"),
        )
        self.assertEqual(failure.failure.code, "provider_error")

    def test_receipt_path_round_trip_is_append_only_and_human_readable(self):
        receipt = self.receipt()
        expected = (
            self.workspace
            / "projects"
            / "speaker-ad"
            / "attempts"
            / "generate_hero"
            / "attempt-one.json"
        )
        self.assertEqual(
            action_attempt_receipt_path(self.workspace, receipt), expected.resolve()
        )
        path = save_action_attempt_receipt(receipt, self.workspace)
        self.assertEqual(path, expected.resolve())
        self.assertEqual(load_action_attempt_receipt(path), receipt)
        self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))
        with self.assertRaises(ActionConfirmationError):
            save_action_attempt_receipt(receipt, self.workspace)

    def test_receipt_nested_storage_symlink_cannot_escape_project(self):
        receipt = self.receipt()
        outside = self.workspace / "outside"
        outside.mkdir()
        project = self.workspace / "projects" / "speaker-ad"
        (project / "attempts").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ActionConfirmationError):
            save_action_attempt_receipt(receipt, self.workspace)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
