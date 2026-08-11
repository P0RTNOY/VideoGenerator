"""Pure deterministic operator guidance derived from current production status."""

from typing import Literal, Optional, Tuple

from pydantic import ConfigDict, Field, ValidationError

from ad_production_run import AdProductionRun
from ad_production_status import (
    AdProductionStatus,
    ShotProductionStatus,
    WorkflowState,
)
from product import NonEmptyStr, ProductModel


TargetExecutionKind = Literal[
    "local",
    "paid_provider",
    "local_render",
    "inspection",
]


class FrozenGuidanceModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class NextAction(FrozenGuidanceModel):
    command: NonEmptyStr
    description: NonEmptyStr
    shot_id: Optional[NonEmptyStr] = None
    target_requires_confirmation: bool = False
    target_execution_kind: TargetExecutionKind = "local"
    blockers: Tuple[NonEmptyStr, ...] = Field(default_factory=tuple)


class NextActions(FrozenGuidanceModel):
    project_id: NonEmptyStr
    run_revision: int = Field(ge=0, strict=True)
    workflow_state: WorkflowState
    actions: Tuple[NextAction, ...] = Field(default_factory=tuple)
    blockers: Tuple[NonEmptyStr, ...] = Field(default_factory=tuple)


class NextActionsError(ValueError):
    """Raised when run and status do not describe the same current snapshot."""


def _action(
    command: str,
    description: str,
    *,
    shot_id: Optional[str] = None,
    confirmation: bool = False,
    execution_kind: TargetExecutionKind = "local",
    blockers: Tuple[str, ...] = (),
) -> NextAction:
    return NextAction(
        command=command,
        description=description,
        shot_id=shot_id,
        target_requires_confirmation=confirmation,
        target_execution_kind=execution_kind,
        blockers=blockers,
    )


def _shot_action(status: ShotProductionStatus) -> Optional[NextAction]:
    shot_id = status.shot_id
    if status.state == "awaiting_asset_selection":
        return _action(
            "attach-asset-plan",
            "Attach a current explicit asset selection for this Storyboard context.",
            shot_id=shot_id,
        )
    if status.state == "awaiting_hero_generation":
        return _action(
            "preview-generate-hero",
            "Create an exact confirmation preview for one paid Hero generation.",
            shot_id=shot_id,
            confirmation=True,
            execution_kind="paid_provider",
        )
    if status.state == "awaiting_hero_review":
        return _action(
            "record-hero-review",
            "Record a manual review of the current exact Hero Frame.",
            shot_id=shot_id,
        )
    if status.state == "awaiting_motion_intent":
        return _action(
            "attach-shot-video-intent",
            "Attach explicitly authored motion intent for this shot.",
            shot_id=shot_id,
        )
    if status.state == "awaiting_video_generation":
        return _action(
            "preview-generate-video",
            "Create an exact confirmation preview for one paid Video generation.",
            shot_id=shot_id,
            confirmation=True,
            execution_kind="paid_provider",
        )
    if status.state == "awaiting_video_review":
        return _action(
            "record-video-review",
            "Record a manual review and usable range for the current Video Shot.",
            shot_id=shot_id,
        )
    return None


def derive_next_actions(
    run: AdProductionRun,
    status: AdProductionStatus,
) -> NextActions:
    """Project read-only status into an ordered list of explicit CLI boundaries."""

    try:
        current_run = AdProductionRun.model_validate(run.model_dump())
        current_status = AdProductionStatus.model_validate(status.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise NextActionsError(f"next-actions input is invalid: {error}") from error
    if (
        current_run.project_id != current_status.project_id
        or current_run.revision != current_status.revision
    ):
        raise NextActionsError("status does not belong to the current run revision")

    blockers = tuple(
        f"{issue.scope}: {issue.message}" for issue in current_status.issues
    )
    actions = []
    state = current_status.state
    if state == "awaiting_product_input":
        actions.append(
            _action(
                "set-product-input",
                "Provide one explicit Product URL or manual Product name.",
            )
        )
    elif state == "awaiting_research":
        if (
            current_run.product_input is not None
            and current_run.product_input.kind == "product_url"
        ):
            actions.append(
                _action(
                    "research-product",
                    "Research exactly the current explicit Product URL.",
                )
            )
        else:
            blockers += (
                "A Product URL is required for research-product; attach a manually prepared Product instead.",
            )
        actions.append(
            _action(
                "attach-product",
                "Attach an existing strict Product JSON document.",
            )
        )
    elif state == "awaiting_campaign":
        actions.append(
            _action(
                "attach-campaign",
                "Attach an explicitly authored Campaign for the current Product.",
            )
        )
    elif state == "awaiting_storyboard":
        actions.append(
            _action(
                "attach-storyboard",
                "Attach an explicitly authored Storyboard for the current Campaign.",
            )
        )
    elif state == "awaiting_asset_discovery":
        actions.extend(
            (
                _action(
                    "download-assets",
                    "Download only explicitly selected current Product image IDs.",
                    blockers=("One or more explicit Product image IDs are required.",),
                ),
                _action(
                    "attach-original-assets",
                    "Attach an existing current original Product asset manifest.",
                ),
            )
        )
    elif state == "awaiting_asset_selection":
        if current_run.derived_asset_manifest is None:
            actions.append(
                _action(
                    "prepare-reference-assets",
                    "Prepare optional local reference sheets from current originals.",
                )
            )
        actions.append(
            _action(
                "attach-asset-plan",
                "Attach explicit per-shot asset selections.",
            )
        )
    elif state == "shots_in_progress":
        actions.extend(
            action
            for shot in current_status.shots
            for action in (_shot_action(shot),)
            if action is not None
        )
    elif state == "awaiting_editing_plan":
        actions.extend(
            (
                _action(
                    "build-editing-plan",
                    "Build one EditingPlan from strict authored selections and timings.",
                ),
                _action(
                    "attach-editing-plan",
                    "Attach an existing current EditingPlan.",
                ),
            )
        )
    elif state == "ready_to_render":
        actions.append(
            _action(
                "preview-render",
                "Create exact confirmation for one local final render.",
                confirmation=True,
                execution_kind="local_render",
            )
        )
    elif state == "awaiting_final_review":
        actions.append(
            _action(
                "record-final-review",
                "Record a manual review of the exact current edited artifact.",
            )
        )
    else:
        actions.append(
            _action(
                "status",
                "Inspect the complete current deliverable and its derived authority.",
                execution_kind="inspection",
            )
        )

    return NextActions(
        project_id=current_run.project_id,
        run_revision=current_run.revision,
        workflow_state=state,
        actions=tuple(actions),
        blockers=blockers,
    )
