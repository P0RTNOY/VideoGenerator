"""Deterministic, read-only workflow status derivation for local production runs."""

from pathlib import Path
from typing import List, Literal, Optional, Sequence, Tuple, Type, TypeVar, Union

from pydantic import ConfigDict, Field, ValidationError

from ad_production_run import AdProductionRun, RunDocumentReference, RunOperationDiagnostic
from campaign import AdCampaign
from campaign_deliverable import CampaignDeliverableError, build_campaign_deliverable_handoff
from editing_artifact import (
    EditingArtifact,
    EditingArtifactValidationError,
    read_verified_editing_artifact_bytes,
)
from editing_audio_assets import EditingAudioManifest
from editing_instruction import EditingInstructionError, compile_editing_instruction
from editing_plan import EditingPlan
from final_ad_review import (
    FinalAdReviewError,
    FinalAdReviewHistory,
    resolve_final_ad_review_state,
)
from hero_frame_artifact import HeroFrameArtifact, hero_frame_request_fingerprint
from hero_frame_request import HeroFrameRequestError, build_hero_frame_request
from hero_frame_review import (
    HeroFrameReviewError,
    HeroFrameReviewHistory,
    create_hero_frame_review_context,
    resolve_hero_frame_review_state,
)
from product import NonEmptyStr, Product, ProductModel
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from shot_video_intent import ShotVideoIntent, ShotVideoIntentError, validate_shot_video_intent
from storyboard import Storyboard, StoryboardContextError, validate_storyboard
from storyboard_asset_plan import (
    AssetPlanContextError,
    StoryboardAssetPlan,
    validate_storyboard_asset_plan,
)
from video_shot_artifact import (
    VideoShotArtifact,
    VideoShotArtifactValidationError,
    read_verified_video_shot_bytes,
    video_shot_request_fingerprint,
)
from video_shot_request import VideoShotRequestError, build_video_shot_request
from video_shot_review import (
    VideoShotReviewError,
    VideoShotReviewHistory,
    create_video_shot_review_context,
    resolve_video_shot_review_state,
)


WorkflowState = Literal[
    "awaiting_product_input",
    "awaiting_research",
    "awaiting_campaign",
    "awaiting_storyboard",
    "awaiting_asset_discovery",
    "awaiting_asset_selection",
    "shots_in_progress",
    "awaiting_editing_plan",
    "ready_to_render",
    "awaiting_final_review",
    "complete",
]
ShotWorkflowState = Literal[
    "awaiting_asset_selection",
    "awaiting_hero_generation",
    "awaiting_hero_review",
    "awaiting_motion_intent",
    "awaiting_video_generation",
    "awaiting_video_review",
    "approved_for_editing",
]
StatusIssueCode = Literal[
    "missing_or_unreadable_reference",
    "reference_identity_mismatch",
    "invalid_domain_context",
    "stale_artifact",
    "stale_authorization",
]


class FrozenStatusModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class AdProductionStatusIssue(FrozenStatusModel):
    scope: NonEmptyStr
    code: StatusIssueCode
    message: NonEmptyStr


class ShotProductionStatus(FrozenStatusModel):
    shot_id: NonEmptyStr
    state: ShotWorkflowState


class AdProductionStatus(FrozenStatusModel):
    project_id: NonEmptyStr
    revision: int = Field(ge=0, strict=True)
    state: WorkflowState
    shots: Tuple[ShotProductionStatus, ...] = ()
    issues: Tuple[AdProductionStatusIssue, ...] = ()
    latest_operation_diagnostic: Optional[RunOperationDiagnostic] = None


class AdProductionStatusError(ValueError):
    """Raised when the run itself cannot be trusted enough for derivation."""


ModelT = TypeVar("ModelT", bound=ProductModel)


def _issue(scope: str, code: StatusIssueCode, message: str) -> AdProductionStatusIssue:
    return AdProductionStatusIssue(scope=scope, code=code, message=message)


def _status(
    run: AdProductionRun,
    state: WorkflowState,
    *,
    shots: Sequence[ShotProductionStatus] = (),
    issues: Sequence[AdProductionStatusIssue] = (),
) -> AdProductionStatus:
    return AdProductionStatus(
        project_id=run.project_id,
        revision=run.revision,
        state=state,
        shots=tuple(shots),
        issues=tuple(issues),
        latest_operation_diagnostic=(
            None
            if run.latest_operation_diagnostic is None
            else run.latest_operation_diagnostic.model_copy(deep=True)
        ),
    )


def _reference_path(workspace_root: Path, reference: RunDocumentReference) -> Path:
    candidate = workspace_root / reference.path
    try:
        if candidate.is_symlink():
            raise ValueError("symbolic link")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace_root.resolve())
    except (OSError, ValueError) as error:
        raise AdProductionStatusError(
            f"workspace reference is unavailable or escapes its root: {reference.path}"
        ) from error
    if not resolved.is_file():
        raise AdProductionStatusError(f"workspace reference is not a file: {reference.path}")
    return resolved


def _load_model(
    workspace_root: Path,
    reference: RunDocumentReference,
    model: Type[ModelT],
) -> ModelT:
    path = _reference_path(workspace_root, reference)
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        raise AdProductionStatusError(
            f"workspace reference is not valid {model.__name__} JSON: {reference.path}"
        ) from error


def _load_phase_model(
    workspace_root: Path,
    reference: RunDocumentReference,
    model: Type[ModelT],
    *,
    scope: str,
    expected_id,
    issues: List[AdProductionStatusIssue],
) -> Optional[ModelT]:
    try:
        current = _load_model(workspace_root, reference, model)
    except AdProductionStatusError:
        issues.append(
            _issue(
                scope,
                "missing_or_unreadable_reference",
                f"{scope} reference could not be loaded",
            )
        )
        return None
    if expected_id(current) != reference.id:
        issues.append(
            _issue(
                scope,
                "reference_identity_mismatch",
                f"{scope} document identity does not match run reference",
            )
        )
        return None
    return current


def _shot_status(shot_id: str, state: ShotWorkflowState) -> ShotProductionStatus:
    return ShotProductionStatus(shot_id=shot_id, state=state)


def _resolve_one_shot(
    *,
    run_shot,
    workspace_root: Path,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    asset_plan: StoryboardAssetPlan,
    original_manifest: ProductAssetManifest,
    derived_manifest: Optional[ProductDerivedAssetManifest],
    issues: List[AdProductionStatusIssue],
):
    shot_id = run_shot.shot_id
    scope = f"shot:{shot_id}"
    if run_shot.hero_frame_artifact is None:
        return _shot_status(shot_id, "awaiting_hero_generation"), None, None
    hero = _load_phase_model(
        workspace_root,
        run_shot.hero_frame_artifact,
        HeroFrameArtifact,
        scope=scope,
        expected_id=lambda item: item.id,
        issues=issues,
    )
    if hero is None:
        return _shot_status(shot_id, "awaiting_hero_generation"), None, None
    try:
        expected_request = build_hero_frame_request(
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            asset_plan=asset_plan,
            shot_id=shot_id,
            original_manifest=original_manifest,
            derived_manifest=derived_manifest,
        )
        if (
            hero.product_id,
            hero.campaign_id,
            hero.storyboard_id,
            hero.asset_plan_id,
            hero.shot_id,
        ) != (product.id, campaign.id, storyboard.id, asset_plan.id, shot_id):
            raise ValueError("Hero Frame bindings changed")
        if hero.source_request_fingerprint != hero_frame_request_fingerprint(expected_request):
            raise ValueError("Hero Frame request fingerprint changed")
        current_hero_context = create_hero_frame_review_context(hero)
    except (HeroFrameRequestError, HeroFrameReviewError, ValueError) as error:
        issues.append(_issue(scope, "stale_artifact", f"Hero Frame is stale: {error}"))
        return _shot_status(shot_id, "awaiting_hero_generation"), None, None

    if run_shot.hero_frame_review_history is None:
        return _shot_status(shot_id, "awaiting_hero_review"), None, None
    hero_history = _load_phase_model(
        workspace_root,
        run_shot.hero_frame_review_history,
        HeroFrameReviewHistory,
        scope=scope,
        expected_id=lambda item: item.review_context.id,
        issues=issues,
    )
    if hero_history is None:
        return _shot_status(shot_id, "awaiting_hero_review"), None, None
    try:
        hero_state = resolve_hero_frame_review_state(hero_history, hero)
    except HeroFrameReviewError as error:
        issues.append(_issue(scope, "stale_authorization", f"Hero review is stale: {error}"))
        return _shot_status(shot_id, "awaiting_hero_review"), None, None
    if hero_history.review_context != current_hero_context or hero_state != "approved_for_video_start_image":
        return _shot_status(shot_id, "awaiting_hero_review"), None, None

    if run_shot.video_intent is None:
        return _shot_status(shot_id, "awaiting_motion_intent"), None, None
    intent = _load_phase_model(
        workspace_root,
        run_shot.video_intent,
        ShotVideoIntent,
        scope=scope,
        expected_id=lambda item: item.id,
        issues=issues,
    )
    if intent is None:
        return _shot_status(shot_id, "awaiting_motion_intent"), None, None
    try:
        intent = validate_shot_video_intent(intent, product, campaign, storyboard, asset_plan)
    except ShotVideoIntentError as error:
        issues.append(_issue(scope, "invalid_domain_context", f"Video intent is stale: {error}"))
        return _shot_status(shot_id, "awaiting_motion_intent"), None, None

    if run_shot.video_artifact is None:
        return _shot_status(shot_id, "awaiting_video_generation"), None, None
    video = _load_phase_model(
        workspace_root,
        run_shot.video_artifact,
        VideoShotArtifact,
        scope=scope,
        expected_id=lambda item: item.id,
        issues=issues,
    )
    if video is None:
        return _shot_status(shot_id, "awaiting_video_generation"), None, None
    try:
        expected_video_request = build_video_shot_request(
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            asset_plan=asset_plan,
            hero_frame_artifact=hero,
            review_history=hero_history,
            shot_id=shot_id,
            requested_generation_duration_seconds=intent.requested_generation_duration_seconds,
            motion=intent.motion,
            motion_relevant_claim_ids=intent.motion_relevant_claim_ids,
        )
        if video.source_request_fingerprint != video_shot_request_fingerprint(expected_video_request):
            raise ValueError("Video Shot request fingerprint changed")
        if (
            video.product_id,
            video.campaign_id,
            video.storyboard_id,
            video.asset_plan_id,
            video.shot_id,
            video.hero_frame_artifact_id,
            video.hero_frame_sha256,
            video.authorizing_review_context_id,
            video.authorizing_review_id,
        ) != (
            product.id,
            campaign.id,
            storyboard.id,
            asset_plan.id,
            shot_id,
            hero.id,
            hero.sha256,
            hero_history.review_context.id,
            hero_history.reviews[-1].review_id,
        ):
            raise ValueError("Video Shot bindings changed")
        read_verified_video_shot_bytes(video)
        current_video_context = create_video_shot_review_context(video, storyboard)
    except (
        VideoShotRequestError,
        VideoShotArtifactValidationError,
        VideoShotReviewError,
        ValueError,
    ) as error:
        issues.append(_issue(scope, "stale_artifact", f"Video Shot is stale: {error}"))
        return _shot_status(shot_id, "awaiting_video_generation"), None, None

    if run_shot.video_review_history is None:
        return _shot_status(shot_id, "awaiting_video_review"), None, None
    video_history = _load_phase_model(
        workspace_root,
        run_shot.video_review_history,
        VideoShotReviewHistory,
        scope=scope,
        expected_id=lambda item: item.review_context.id,
        issues=issues,
    )
    if video_history is None:
        return _shot_status(shot_id, "awaiting_video_review"), None, None
    try:
        video_state = resolve_video_shot_review_state(video_history, current_video_context)
    except VideoShotReviewError as error:
        issues.append(_issue(scope, "stale_authorization", f"Video review is stale: {error}"))
        return _shot_status(shot_id, "awaiting_video_review"), None, None
    if video_state != "approved_for_editing":
        return _shot_status(shot_id, "awaiting_video_review"), None, None
    return _shot_status(shot_id, "approved_for_editing"), video, video_history


def resolve_ad_production_status(
    run: AdProductionRun,
    workspace_root: Union[str, Path],
    *,
    font_candidates: Optional[Sequence[Path]] = None,
) -> AdProductionStatus:
    """Derive current workflow state without writing or invoking providers."""

    try:
        current = AdProductionRun.model_validate(run.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise AdProductionStatusError(f"Ad Production Run is structurally invalid: {error}") from error
    root = Path(workspace_root)
    issues: List[AdProductionStatusIssue] = []
    if current.product is None:
        return _status(
            current,
            "awaiting_product_input" if current.product_input is None else "awaiting_research",
        )
    product = _load_phase_model(
        root, current.product, Product, scope="product", expected_id=lambda item: item.id, issues=issues
    )
    if product is None:
        return _status(current, "awaiting_research", issues=issues)
    if current.campaign is None:
        return _status(current, "awaiting_campaign")
    campaign = _load_phase_model(
        root, current.campaign, AdCampaign, scope="campaign", expected_id=lambda item: item.id, issues=issues
    )
    if campaign is None or campaign.claim_selection.product_id != product.id:
        if campaign is not None:
            issues.append(_issue("campaign", "invalid_domain_context", "Campaign does not belong to the current Product"))
        return _status(current, "awaiting_campaign", issues=issues)
    if current.storyboard is None:
        return _status(current, "awaiting_storyboard")
    storyboard = _load_phase_model(
        root, current.storyboard, Storyboard, scope="storyboard", expected_id=lambda item: item.id, issues=issues
    )
    if storyboard is None:
        return _status(current, "awaiting_storyboard", issues=issues)
    try:
        storyboard = validate_storyboard(storyboard, campaign)
    except StoryboardContextError as error:
        issues.append(_issue("storyboard", "invalid_domain_context", str(error)))
        return _status(current, "awaiting_storyboard", issues=issues)

    asset_waiting_shots = tuple(_shot_status(shot.id, "awaiting_asset_selection") for shot in storyboard.shots)
    if current.original_asset_manifest is None:
        return _status(current, "awaiting_asset_discovery", shots=asset_waiting_shots)
    original_manifest = _load_phase_model(
        root,
        current.original_asset_manifest,
        ProductAssetManifest,
        scope="original_asset_manifest",
        expected_id=lambda item: item.product_id,
        issues=issues,
    )
    if original_manifest is None or original_manifest.product_id != product.id or not original_manifest.images:
        if original_manifest is not None and original_manifest.product_id != product.id:
            issues.append(_issue("original_asset_manifest", "invalid_domain_context", "asset manifest does not belong to the current Product"))
        return _status(current, "awaiting_asset_discovery", shots=asset_waiting_shots, issues=issues)
    derived_manifest = None
    if current.derived_asset_manifest is not None:
        derived_manifest = _load_phase_model(
            root,
            current.derived_asset_manifest,
            ProductDerivedAssetManifest,
            scope="derived_asset_manifest",
            expected_id=lambda item: item.product_id,
            issues=issues,
        )
        if derived_manifest is None or derived_manifest.product_id != product.id:
            return _status(current, "awaiting_asset_discovery", shots=asset_waiting_shots, issues=issues)
    audio_manifest = None
    if current.editing_audio_manifest is not None:
        audio_manifest = _load_phase_model(
            root,
            current.editing_audio_manifest,
            EditingAudioManifest,
            scope="editing_audio_manifest",
            expected_id=lambda item: item.product_id,
            issues=issues,
        )

    if current.asset_plan is None:
        return _status(
            current,
            "awaiting_asset_selection",
            shots=asset_waiting_shots,
            issues=issues,
        )
    asset_plan = _load_phase_model(
        root, current.asset_plan, StoryboardAssetPlan, scope="asset_plan", expected_id=lambda item: item.id, issues=issues
    )
    if asset_plan is None:
        return _status(current, "awaiting_asset_selection", shots=asset_waiting_shots, issues=issues)
    try:
        asset_plan = validate_storyboard_asset_plan(
            asset_plan, product, campaign, storyboard, original_manifest, derived_manifest
        )
    except AssetPlanContextError as error:
        issues.append(_issue("asset_plan", "invalid_domain_context", str(error)))
        return _status(current, "awaiting_asset_selection", shots=asset_waiting_shots, issues=issues)
    storyboard_ids = [item.id for item in storyboard.shots]
    if (
        [item.shot_id for item in asset_plan.shot_selections] != storyboard_ids
        or [item.shot_id for item in current.shots] != storyboard_ids
    ):
        issues.append(_issue("asset_plan", "invalid_domain_context", "asset selections and run shot references must cover Storyboard order"))
        return _status(current, "awaiting_asset_selection", shots=asset_waiting_shots, issues=issues)

    shot_statuses = []
    video_artifacts = []
    video_histories = []
    for run_shot in current.shots:
        shot_status, video, history = _resolve_one_shot(
            run_shot=run_shot,
            workspace_root=root,
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            asset_plan=asset_plan,
            original_manifest=original_manifest,
            derived_manifest=derived_manifest,
            issues=issues,
        )
        shot_statuses.append(shot_status)
        if video is not None and history is not None:
            video_artifacts.append(video)
            video_histories.append(history)
    if any(item.state != "approved_for_editing" for item in shot_statuses):
        return _status(current, "shots_in_progress", shots=shot_statuses, issues=issues)

    if current.editing_plan is None:
        return _status(
            current,
            "awaiting_editing_plan",
            shots=shot_statuses,
            issues=issues,
        )
    editing_plan = _load_phase_model(
        root, current.editing_plan, EditingPlan, scope="editing_plan", expected_id=lambda item: item.id, issues=issues
    )
    if editing_plan is None:
        return _status(current, "awaiting_editing_plan", shots=shot_statuses, issues=issues)
    try:
        compile_editing_instruction(
            plan=editing_plan,
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            video_artifacts=video_artifacts,
            video_review_histories=video_histories,
            audio_manifest=audio_manifest,
            font_candidates=font_candidates,
        )
    except EditingInstructionError as error:
        issues.append(_issue("editing_plan", "stale_authorization", str(error)))
        return _status(current, "awaiting_editing_plan", shots=shot_statuses, issues=issues)
    if current.editing_artifact is None:
        return _status(
            current,
            "ready_to_render",
            shots=shot_statuses,
            issues=issues,
        )
    editing_artifact = _load_phase_model(
        root,
        current.editing_artifact,
        EditingArtifact,
        scope="editing_artifact",
        expected_id=lambda item: item.id,
        issues=issues,
    )
    try:
        if editing_artifact is None:
            raise EditingArtifactValidationError("Editing artifact is unavailable")
        if (
            editing_artifact.product_id,
            editing_artifact.campaign_id,
            editing_artifact.storyboard_id,
            editing_artifact.editing_plan_id,
        ) != (product.id, campaign.id, storyboard.id, editing_plan.id):
            raise EditingArtifactValidationError("Editing artifact bindings changed")
        read_verified_editing_artifact_bytes(editing_artifact)
    except EditingArtifactValidationError as error:
        issues.append(_issue("editing_artifact", "stale_artifact", str(error)))
        return _status(current, "ready_to_render", shots=shot_statuses, issues=issues)
    if current.final_review_history is None:
        return _status(
            current,
            "awaiting_final_review",
            shots=shot_statuses,
            issues=issues,
        )
    final_history = _load_phase_model(
        root,
        current.final_review_history,
        FinalAdReviewHistory,
        scope="final_review_history",
        expected_id=lambda item: item.review_context.id,
        issues=issues,
    )
    if final_history is None:
        return _status(current, "awaiting_final_review", shots=shot_statuses, issues=issues)
    try:
        final_state = resolve_final_ad_review_state(
            final_history,
            editing_artifact,
            editing_plan,
            product,
            campaign,
            storyboard,
            video_artifacts,
            video_histories,
        )
    except FinalAdReviewError as error:
        issues.append(_issue("final_review_history", "stale_authorization", str(error)))
        return _status(current, "awaiting_final_review", shots=shot_statuses, issues=issues)
    if final_state != "approved_final_campaign_deliverable":
        return _status(current, "awaiting_final_review", shots=shot_statuses, issues=issues)
    try:
        build_campaign_deliverable_handoff(
            history=final_history,
            artifact=editing_artifact,
            plan=editing_plan,
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            video_artifacts=video_artifacts,
            video_review_histories=video_histories,
        )
    except CampaignDeliverableError as error:
        issues.append(_issue("deliverable", "stale_authorization", str(error)))
        return _status(current, "awaiting_final_review", shots=shot_statuses, issues=issues)
    return _status(current, "complete", shots=shot_statuses, issues=issues)
