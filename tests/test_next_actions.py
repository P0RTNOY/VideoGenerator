import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ad_production_cli import main
from ad_production_run import (
    AdProductionRun,
    RunDocumentReference,
    RunProductInput,
    initialize_ad_production_run,
)
from ad_production_status import (
    AdProductionStatus,
    AdProductionStatusIssue,
    ShotProductionStatus,
)
from next_actions import derive_next_actions


class NextActionsTests(unittest.TestCase):
    def run_for(
        self,
        state,
        *,
        shots=(),
        issues=(),
        product_input=None,
        originals=False,
        derived=False,
    ):
        run = AdProductionRun(
            project_id="guided-ad",
            product_input=product_input,
            product=(
                RunDocumentReference(id="product:x", path="documents/product.json")
                if originals
                else None
            ),
            original_asset_manifest=(
                RunDocumentReference(id="product:x", path="documents/assets.json")
                if originals
                else None
            ),
            derived_asset_manifest=(
                RunDocumentReference(id="product:x", path="documents/derived.json")
                if derived
                else None
            ),
        )
        status = AdProductionStatus(
            project_id=run.project_id,
            revision=run.revision,
            state=state,
            shots=shots,
            issues=issues,
        )
        before_run = deepcopy(run.model_dump())
        before_status = deepcopy(status.model_dump())
        first = derive_next_actions(run, status)
        second = derive_next_actions(run, status)
        self.assertEqual(first, second)
        self.assertEqual(run.model_dump(), before_run)
        self.assertEqual(status.model_dump(), before_status)
        return first

    def test_all_top_level_states_have_conservative_ordered_guidance(self):
        expected = {
            "awaiting_product_input": ("set-product-input",),
            "awaiting_research": ("research-product", "attach-product"),
            "awaiting_campaign": ("attach-campaign",),
            "awaiting_storyboard": ("attach-storyboard",),
            "awaiting_asset_discovery": ("download-assets", "attach-original-assets"),
            "awaiting_asset_selection": (
                "prepare-reference-assets",
                "attach-asset-plan",
            ),
            "awaiting_editing_plan": ("build-editing-plan", "attach-editing-plan"),
            "ready_to_render": ("preview-render",),
            "awaiting_final_review": ("record-final-review",),
            "complete": ("status",),
        }
        for state, commands in expected.items():
            with self.subTest(state=state):
                result = self.run_for(
                    state,
                    product_input=RunProductInput(
                        kind="product_url", value="https://example.test/product"
                    ),
                    originals=state == "awaiting_asset_selection",
                )
                self.assertEqual(tuple(item.command for item in result.actions), commands)

    def test_product_name_does_not_offer_unsupported_automatic_research(self):
        result = self.run_for(
            "awaiting_research",
            product_input=RunProductInput(kind="product_name", value="Speaker"),
        )
        self.assertEqual(tuple(item.command for item in result.actions), ("attach-product",))
        self.assertTrue(any("URL" in blocker for blocker in result.blockers))

    def test_existing_derived_assets_do_not_offer_redundant_preparation(self):
        result = self.run_for(
            "awaiting_asset_selection", originals=True, derived=True
        )
        self.assertEqual(
            tuple(item.command for item in result.actions), ("attach-asset-plan",)
        )

    def test_every_per_shot_state_maps_to_the_exact_manual_or_preview_boundary(self):
        states = (
            "awaiting_asset_selection",
            "awaiting_hero_generation",
            "awaiting_hero_review",
            "awaiting_motion_intent",
            "awaiting_video_generation",
            "awaiting_video_review",
            "approved_for_editing",
        )
        result = self.run_for(
            "shots_in_progress",
            shots=tuple(
                ShotProductionStatus(shot_id=f"shot-{index}", state=state)
                for index, state in enumerate(states)
            ),
        )
        self.assertEqual(
            tuple((item.shot_id, item.command) for item in result.actions),
            (
                ("shot-0", "attach-asset-plan"),
                ("shot-1", "preview-generate-hero"),
                ("shot-2", "record-hero-review"),
                ("shot-3", "attach-shot-video-intent"),
                ("shot-4", "preview-generate-video"),
                ("shot-5", "record-video-review"),
            ),
        )

    def test_paid_and_render_preview_labels_are_exact(self):
        shots = (
            ShotProductionStatus(
                shot_id="hero", state="awaiting_hero_generation"
            ),
            ShotProductionStatus(
                shot_id="video", state="awaiting_video_generation"
            ),
        )
        shot_result = self.run_for("shots_in_progress", shots=shots)
        for action in shot_result.actions:
            self.assertTrue(action.target_requires_confirmation)
            self.assertEqual(action.target_execution_kind, "paid_provider")
        render = self.run_for("ready_to_render")
        self.assertTrue(render.actions[0].target_requires_confirmation)
        self.assertEqual(render.actions[0].target_execution_kind, "local_render")
        review = self.run_for("awaiting_final_review")
        self.assertFalse(review.actions[0].target_requires_confirmation)
        self.assertEqual(review.actions[0].target_execution_kind, "local")

    def test_stale_issue_is_explained_and_only_replacement_action_is_offered(self):
        issue = AdProductionStatusIssue(
            scope="shot:hero",
            code="stale_artifact",
            message="Hero Frame is stale",
        )
        result = self.run_for(
            "shots_in_progress",
            shots=(
                ShotProductionStatus(
                    shot_id="hero", state="awaiting_hero_generation"
                ),
            ),
            issues=(issue,),
        )
        self.assertEqual(tuple(item.command for item in result.actions), ("preview-generate-hero",))
        self.assertIn("shot:hero: Hero Frame is stale", result.blockers)

    def test_complete_offers_inspection_only(self):
        result = self.run_for("complete")
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0].target_execution_kind, "inspection")
        self.assertFalse(result.actions[0].target_requires_confirmation)


class NextActionsCliTests(unittest.TestCase):
    def test_cli_is_read_only_and_prints_deterministic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            initialize_ad_production_run(
                workspace,
                "guided-cli",
                product_input=RunProductInput(
                    kind="product_url", value="https://example.test/product"
                ),
            )
            run_path = workspace / "projects/guided-cli/run.json"
            before = run_path.read_bytes()
            import io
            from contextlib import redirect_stderr, redirect_stdout

            outputs = []
            for _ in range(2):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        [
                            "--workspace",
                            str(workspace),
                            "next-actions",
                            "guided-cli",
                        ]
                    )
                self.assertEqual((code, stderr.getvalue()), (0, ""))
                outputs.append(json.loads(stdout.getvalue()))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0]["actions"][0]["command"], "research-product")
            self.assertEqual(run_path.read_bytes(), before)
            self.assertEqual(list((workspace / "projects/guided-cli").glob("action-previews/*")), [])


if __name__ == "__main__":
    unittest.main()
