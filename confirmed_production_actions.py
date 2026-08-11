"""Confirmed one-attempt Hero, Video, and local editing execution actions."""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, Union

from pydantic import ConfigDict, ValidationError

from action_confirmation import (
    ActionAttemptReceipt,
    ActionConfirmationError,
    ActionFailureDiagnostic,
    ActionPreview,
    CostQuoteStatus,
    claim_action_attempt,
    claim_action_preview,
    create_action_preview,
    save_action_attempt_receipt,
    save_action_preview,
)
from ad_production_run import (
    AdProductionRun,
    AdProductionRunError,
    RunDocumentReference,
    ad_production_run_lock,
    ad_production_run_path,
    attach_run_reference,
    attach_shot_reference,
    save_ad_production_run,
)
from campaign import AdCampaign
from editing_artifact import EditingArtifact, EditingReceipt
from editing_audio_assets import EditingAudioManifest
from editing_plan import EditingPlan
from ffmpeg_editor import EditingRenderResult, render_editing_plan
from hero_frame_artifact import (
    HeroFrameArtifact,
    HeroFrameGenerationReceipt,
    HeroFrameGenerationResult,
    hero_frame_request_fingerprint,
)
from hero_frame_generation import (
    HeroFrameGenerationOptions,
    HeroFrameGenerator,
    preflight_hero_frame_generation,
)
from hero_frame_request import HeroFrameRequest, build_hero_frame_request
from hero_frame_review import HeroFrameReviewHistory
from product import Product, ProductModel
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from production_actions import (
    ProductionActionError,
    ProductionActionResult,
    _clear_failure,
    _editing_context,
    _fingerprint,
    _load_reference_model,
    _project_root,
    _record_failure,
    _reference,
    _required_run_model,
    _run_shot,
    _safe_directory,
)
from shot_video_intent import ShotVideoIntent
from storyboard import Storyboard
from storyboard_asset_plan import StoryboardAssetPlan
from video_shot_artifact import (
    VideoGenerationReceipt,
    VideoGenerationResult,
    VideoShotArtifact,
    video_shot_request_fingerprint,
)
from video_shot_generation import (
    VideoGenerationOptions,
    VideoProvider,
    preflight_video_generation,
)
from video_shot_request import VideoShotRequest, build_video_shot_request


class ConfirmedProductionActionError(ProductionActionError):
    """Raised after a confirmed action is rejected or records a terminal failure."""


class RenderEditingPlanOptions(ProductModel):
    """The fixed, non-secret local rendering intent bound by a preview."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    tool: Literal["ffmpeg"] = "ffmpeg"
    output_policy: Literal["vertical-720x1280-h264-aac-mp4"] = (
        "vertical-720x1280-h264-aac-mp4"
    )


@dataclass(frozen=True)
class _HeroContext:
    run: AdProductionRun
    request: HeroFrameRequest
    fingerprint: str


@dataclass(frozen=True)
class _VideoContext:
    run: AdProductionRun
    request: VideoShotRequest
    fingerprint: str


@dataclass(frozen=True)
class _RenderContext:
    run: AdProductionRun
    plan: EditingPlan
    product: Product
    campaign: AdCampaign
    storyboard: Storyboard
    video_artifacts: tuple[VideoShotArtifact, ...]
    video_review_histories: tuple
    audio_manifest: Optional[EditingAudioManifest]
    fingerprint: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _options_fingerprint(options: ProductModel) -> str:
    return _fingerprint(options.model_dump(mode="json", exclude_none=True))


def _read_locked_run(
    workspace_root: Union[str, Path], project_id: str
) -> AdProductionRun:
    path = ad_production_run_path(workspace_root, project_id)
    if path.is_symlink():
        raise ConfirmedProductionActionError("run.json must not be a symbolic link")
    try:
        run = AdProductionRun.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        raise ConfirmedProductionActionError("run.json could not be loaded") from error
    if run.project_id != project_id:
        raise ConfirmedProductionActionError(
            "run.json project ID does not match its workspace directory"
        )
    return run


def _hero_context(
    run: AdProductionRun,
    workspace_root: Union[str, Path],
    shot_id: str,
) -> _HeroContext:
    product = _required_run_model(
        run, workspace_root, "product", Product, lambda item: item.id
    )
    campaign = _required_run_model(
        run, workspace_root, "campaign", AdCampaign, lambda item: item.id
    )
    storyboard = _required_run_model(
        run, workspace_root, "storyboard", Storyboard, lambda item: item.id
    )
    plan = _required_run_model(
        run, workspace_root, "asset_plan", StoryboardAssetPlan, lambda item: item.id
    )
    originals = _required_run_model(
        run,
        workspace_root,
        "original_asset_manifest",
        ProductAssetManifest,
        lambda item: item.product_id,
    )
    derived = None
    if run.derived_asset_manifest is not None:
        derived = _required_run_model(
            run,
            workspace_root,
            "derived_asset_manifest",
            ProductDerivedAssetManifest,
            lambda item: item.product_id,
        )
    request = build_hero_frame_request(
        product=product,
        campaign=campaign,
        storyboard=storyboard,
        asset_plan=plan,
        shot_id=shot_id,
        original_manifest=originals,
        derived_manifest=derived,
    )
    return _HeroContext(run, request, hero_frame_request_fingerprint(request))


def _video_context(
    run: AdProductionRun,
    workspace_root: Union[str, Path],
    shot_id: str,
) -> _VideoContext:
    product = _required_run_model(
        run, workspace_root, "product", Product, lambda item: item.id
    )
    campaign = _required_run_model(
        run, workspace_root, "campaign", AdCampaign, lambda item: item.id
    )
    storyboard = _required_run_model(
        run, workspace_root, "storyboard", Storyboard, lambda item: item.id
    )
    plan = _required_run_model(
        run, workspace_root, "asset_plan", StoryboardAssetPlan, lambda item: item.id
    )
    shot = _run_shot(run, shot_id)
    if (
        shot.hero_frame_artifact is None
        or shot.hero_frame_review_history is None
        or shot.video_intent is None
    ):
        raise ConfirmedProductionActionError(
            "approved Hero Frame and Shot Video intent are required"
        )
    hero = _load_reference_model(
        workspace_root, shot.hero_frame_artifact, HeroFrameArtifact
    )
    history = _load_reference_model(
        workspace_root, shot.hero_frame_review_history, HeroFrameReviewHistory
    )
    intent = _load_reference_model(
        workspace_root, shot.video_intent, ShotVideoIntent
    )
    if hero.id != shot.hero_frame_artifact.id:
        raise ConfirmedProductionActionError("Hero Frame reference identity changed")
    if history.review_context.id != shot.hero_frame_review_history.id:
        raise ConfirmedProductionActionError("Hero review reference identity changed")
    if intent.id != shot.video_intent.id or intent.shot_id != shot_id:
        raise ConfirmedProductionActionError("Shot Video intent binding changed")
    request = build_video_shot_request(
        product=product,
        campaign=campaign,
        storyboard=storyboard,
        asset_plan=plan,
        hero_frame_artifact=hero,
        review_history=history,
        shot_id=shot_id,
        requested_generation_duration_seconds=(
            intent.requested_generation_duration_seconds
        ),
        motion=intent.motion,
        motion_relevant_claim_ids=intent.motion_relevant_claim_ids,
    )
    return _VideoContext(run, request, video_shot_request_fingerprint(request))


def _render_context(
    run: AdProductionRun,
    workspace_root: Union[str, Path],
    options: RenderEditingPlanOptions,
) -> _RenderContext:
    product, campaign, storyboard, artifacts, histories = _editing_context(
        run, workspace_root
    )
    plan = _required_run_model(
        run, workspace_root, "editing_plan", EditingPlan, lambda item: item.id
    )
    audio_manifest = None
    if run.editing_audio_manifest is not None:
        audio_manifest = _load_reference_model(
            workspace_root, run.editing_audio_manifest, EditingAudioManifest
        )
        if audio_manifest.product_id != run.editing_audio_manifest.id:
            raise ConfirmedProductionActionError(
                "editing audio reference identity changed"
            )
    fingerprint = _fingerprint(
        {
            "editing_plan": plan.model_dump(mode="json", exclude_none=True),
            "video_artifacts": [
                item.model_dump(mode="json", exclude_none=True) for item in artifacts
            ],
            "video_review_histories": [
                item.model_dump(mode="json", exclude_none=True) for item in histories
            ],
            "editing_audio_manifest": (
                None
                if audio_manifest is None
                else audio_manifest.model_dump(mode="json", exclude_none=True)
            ),
            "render_options": options.model_dump(mode="json", exclude_none=True),
        }
    )
    return _RenderContext(
        run,
        plan,
        product,
        campaign,
        storyboard,
        tuple(artifacts),
        tuple(histories),
        audio_manifest,
        fingerprint,
    )


def _paid_preview(
    workspace_root: Union[str, Path],
    *,
    project_id: str,
    run_revision: int,
    action: str,
    request_fingerprint: str,
    provider: str,
    model: str,
    options: ProductModel,
    cost_quote_status: CostQuoteStatus,
    created_at: datetime,
    quoted_cost_usd: Optional[float],
    owner_cost_ceiling_usd: Optional[float],
    allow_unquoted_cost: bool,
) -> ActionPreview:
    preview = create_action_preview(
        project_id=project_id,
        run_revision=run_revision,
        action=action,
        request_fingerprint=request_fingerprint,
        provider=provider,
        model=model,
        options_fingerprint=_options_fingerprint(options),
        requested_output_count=1,
        cost_quote_status=cost_quote_status,
        quoted_cost_usd=quoted_cost_usd,
        owner_cost_ceiling_usd=owner_cost_ceiling_usd,
        allow_unquoted_cost=allow_unquoted_cost,
        created_at=created_at,
    )
    save_action_preview(preview, workspace_root)
    return preview


def create_generate_hero_preview(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    shot_id: str,
    provider: str,
    model: str,
    options: HeroFrameGenerationOptions,
    cost_quote_status: CostQuoteStatus,
    created_at: datetime,
    quoted_cost_usd: Optional[float] = None,
    owner_cost_ceiling_usd: Optional[float] = None,
    allow_unquoted_cost: bool = False,
) -> ActionPreview:
    try:
        current_options = HeroFrameGenerationOptions.model_validate(
            options.model_dump()
        )
        with ad_production_run_lock(workspace_root, project_id, exclusive=False):
            run = _read_locked_run(workspace_root, project_id)
            context = _hero_context(run, workspace_root, shot_id)
        return _paid_preview(
            workspace_root,
            project_id=project_id,
            run_revision=run.revision,
            action="generate_hero",
            request_fingerprint=context.fingerprint,
            provider=provider,
            model=model,
            options=current_options,
            cost_quote_status=cost_quote_status,
            quoted_cost_usd=quoted_cost_usd,
            owner_cost_ceiling_usd=owner_cost_ceiling_usd,
            allow_unquoted_cost=allow_unquoted_cost,
            created_at=created_at,
        )
    except (ActionConfirmationError, AdProductionRunError, ProductionActionError, ValidationError, ValueError) as error:
        raise ConfirmedProductionActionError(
            "Hero generation preview could not be created"
        ) from error


def create_generate_video_preview(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    shot_id: str,
    provider: str,
    model: str,
    options: VideoGenerationOptions,
    cost_quote_status: CostQuoteStatus,
    created_at: datetime,
    quoted_cost_usd: Optional[float] = None,
    owner_cost_ceiling_usd: Optional[float] = None,
    allow_unquoted_cost: bool = False,
) -> ActionPreview:
    try:
        current_options = VideoGenerationOptions.model_validate(options.model_dump())
        with ad_production_run_lock(workspace_root, project_id, exclusive=False):
            run = _read_locked_run(workspace_root, project_id)
            context = _video_context(run, workspace_root, shot_id)
        return _paid_preview(
            workspace_root,
            project_id=project_id,
            run_revision=run.revision,
            action="generate_video",
            request_fingerprint=context.fingerprint,
            provider=provider,
            model=model,
            options=current_options,
            cost_quote_status=cost_quote_status,
            quoted_cost_usd=quoted_cost_usd,
            owner_cost_ceiling_usd=owner_cost_ceiling_usd,
            allow_unquoted_cost=allow_unquoted_cost,
            created_at=created_at,
        )
    except (ActionConfirmationError, AdProductionRunError, ProductionActionError, ValidationError, ValueError) as error:
        raise ConfirmedProductionActionError(
            "Video generation preview could not be created"
        ) from error


def create_render_editing_plan_preview(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    options: RenderEditingPlanOptions,
    created_at: datetime,
) -> ActionPreview:
    try:
        current_options = RenderEditingPlanOptions.model_validate(options.model_dump())
        with ad_production_run_lock(workspace_root, project_id, exclusive=False):
            run = _read_locked_run(workspace_root, project_id)
            context = _render_context(run, workspace_root, current_options)
        preview = create_action_preview(
            project_id=project_id,
            run_revision=run.revision,
            action="render_editing_plan",
            request_fingerprint=context.fingerprint,
            options_fingerprint=_options_fingerprint(current_options),
            requested_output_count=1,
            cost_quote_status="not_applicable",
            created_at=created_at,
        )
        save_action_preview(preview, workspace_root)
        return preview
    except (ActionConfirmationError, AdProductionRunError, ProductionActionError, ValidationError, ValueError) as error:
        raise ConfirmedProductionActionError(
            "Editing render preview could not be created"
        ) from error


def _claim(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    action: str,
    context_builder,
    provider: Optional[str],
    model: Optional[str],
    options: ProductModel,
    confirm_preview: str,
    attempt_id: str,
    started_at: datetime,
):
    try:
        with ad_production_run_lock(workspace_root, project_id, exclusive=True):
            run = _read_locked_run(workspace_root, project_id)
            context = context_builder(run)
            claim_action_attempt(
                workspace_root,
                project_id=project_id,
                action=action,
                attempt_id=attempt_id,
                claimed_at=started_at,
            )
            claim_action_preview(
                workspace_root,
                confirm_preview,
                project_id=project_id,
                run_revision=run.revision,
                action=action,
                request_fingerprint=context.fingerprint,
                provider=provider,
                model=model,
                options_fingerprint=_options_fingerprint(options),
                requested_output_count=1,
                attempt_id=attempt_id,
                now=started_at,
            )
        return context
    except (ActionConfirmationError, AdProductionRunError, ProductionActionError, ValidationError, ValueError) as error:
        raise ConfirmedProductionActionError(
            "exact unexpired, unconsumed ActionPreview confirmation is required"
        ) from error


def _atomic_json_writer(model: ProductModel, path: Path) -> Path:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".confirmed-action-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(
                model.model_dump_json(
                    indent=2, ensure_ascii=False, exclude_none=True
                )
                + "\n"
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _persist_model(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    label: str,
    model: ProductModel,
    identity: str,
) -> RunDocumentReference:
    project = _project_root(workspace_root, project_id)
    data = model.model_dump_json(
        ensure_ascii=False, exclude_none=True
    ).encode("utf-8")
    path = _safe_directory(project, "artifacts", "domain") / (
        f"{label}-{hashlib.sha256(data).hexdigest()}.json"
    )
    expected = (
        model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True)
        + "\n"
    ).encode("utf-8")
    if path.is_symlink():
        raise ConfirmedProductionActionError(
            "confirmed action output must not be a symbolic link"
        )
    if path.exists():
        try:
            if not path.is_file() or path.read_bytes() != expected:
                raise ConfirmedProductionActionError(
                    "content-addressed confirmed output conflicts"
                )
        except OSError as error:
            raise ConfirmedProductionActionError(
                "confirmed action output cannot be verified"
            ) from error
    else:
        _atomic_json_writer(model, path)
    return _reference(workspace_root, path, identity)


def _require_project_media(
    workspace_root: Union[str, Path], project_id: str, local_path: str
) -> None:
    project = _project_root(workspace_root, project_id)
    source = Path(local_path)
    try:
        if source.is_symlink() or not source.is_file():
            raise ValueError("not a regular file")
        source.resolve(strict=True).relative_to(project)
    except (OSError, ValueError) as error:
        raise ConfirmedProductionActionError(
            "generated media must be stored inside the current project"
        ) from error


def _attempt_receipt(
    *,
    attempt_id: str,
    project_id: str,
    action: str,
    preview_id: str,
    started_at: datetime,
    completed_at: datetime,
    run_revision: int,
    request_fingerprint: str,
    cost_quote_status: CostQuoteStatus,
    status: str,
    artifact_reference: Optional[RunDocumentReference],
    domain_receipt_reference: Optional[RunDocumentReference],
    failure: Optional[ActionFailureDiagnostic],
    attachment_outcome: str,
) -> ActionAttemptReceipt:
    return ActionAttemptReceipt(
        attempt_id=attempt_id,
        project_id=project_id,
        action=action,
        preview_id=preview_id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        run_revision_at_start=run_revision,
        request_fingerprint=request_fingerprint,
        cost_quote_status=cost_quote_status,
        artifact_reference=artifact_reference,
        domain_receipt_reference=domain_receipt_reference,
        failure=failure,
        attachment_outcome=attachment_outcome,
    )


def _failed(
    workspace_root,
    project_id,
    *,
    context,
    action,
    shot_id,
    preview_id,
    attempt_id,
    started_at,
    completed_at,
    cost_quote_status,
    code,
    message,
    domain_receipt_reference=None,
):
    failure = ActionFailureDiagnostic(code=code, message=message)
    _record_failure(
        workspace_root,
        project_id,
        action=action,
        shot_id=shot_id,
        code=code,
        message=message,
        recorded_at=completed_at,
    )
    receipt = _attempt_receipt(
        attempt_id=attempt_id,
        project_id=project_id,
        action=action,
        preview_id=preview_id,
        started_at=started_at,
        completed_at=completed_at,
        run_revision=context.run.revision,
        request_fingerprint=context.fingerprint,
        cost_quote_status=cost_quote_status,
        status="failed",
        artifact_reference=None,
        domain_receipt_reference=domain_receipt_reference,
        failure=failure,
        attachment_outcome="not_attached",
    )
    save_action_attempt_receipt(receipt, workspace_root)
    raise ConfirmedProductionActionError(message)


def _attach_result(
    workspace_root,
    project_id,
    *,
    context,
    action,
    shot_id,
    preview_id,
    attempt_id,
    started_at,
    completed_at,
    cost_quote_status,
    artifact_reference,
    domain_receipt_reference,
    candidate,
) -> ProductionActionResult:
    try:
        stored = save_ad_production_run(
            _clear_failure(candidate),
            workspace_root,
            project_id,
            expected_revision=context.run.revision,
        )
    except AdProductionRunError:
        failure = ActionFailureDiagnostic(
            code="attachment_conflict",
            message="Result was preserved but run attachment conflicted",
        )
        latest = _record_failure(
            workspace_root,
            project_id,
            action=action,
            shot_id=shot_id,
            code=failure.code,
            message=failure.message,
            recorded_at=completed_at,
        )
        receipt = _attempt_receipt(
            attempt_id=attempt_id,
            project_id=project_id,
            action=action,
            preview_id=preview_id,
            started_at=started_at,
            completed_at=completed_at,
            run_revision=context.run.revision,
            request_fingerprint=context.fingerprint,
            cost_quote_status=cost_quote_status,
            status="artifact_created_unattached",
            artifact_reference=artifact_reference,
            domain_receipt_reference=domain_receipt_reference,
            failure=failure,
            attachment_outcome="conflict",
        )
        save_action_attempt_receipt(receipt, workspace_root)
        return ProductionActionResult(
            receipt=receipt,
            reference=artifact_reference,
            stored_run_revision=latest.revision,
        )
    receipt = _attempt_receipt(
        attempt_id=attempt_id,
        project_id=project_id,
        action=action,
        preview_id=preview_id,
        started_at=started_at,
        completed_at=completed_at,
        run_revision=context.run.revision,
        request_fingerprint=context.fingerprint,
        cost_quote_status=cost_quote_status,
        status="succeeded_and_attached",
        artifact_reference=artifact_reference,
        domain_receipt_reference=domain_receipt_reference,
        failure=None,
        attachment_outcome="attached",
    )
    save_action_attempt_receipt(receipt, workspace_root)
    return ProductionActionResult(
        receipt=receipt,
        reference=artifact_reference,
        stored_run_revision=stored.revision,
    )


def execute_generate_hero(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    shot_id: str,
    provider: str,
    model: str,
    options: HeroFrameGenerationOptions,
    generator: HeroFrameGenerator,
    confirm_preview: str,
    attempt_id: str,
    now: Callable[[], datetime] = _utc_now,
) -> ProductionActionResult:
    try:
        current_options = HeroFrameGenerationOptions.model_validate(
            options.model_dump()
        )
    except (AttributeError, ValidationError) as error:
        raise ConfirmedProductionActionError("Hero generation options are invalid") from error
    started_at = now()
    context = _claim(
        workspace_root,
        project_id,
        action="generate_hero",
        context_builder=lambda run: _hero_context(run, workspace_root, shot_id),
        provider=provider,
        model=model,
        options=current_options,
        confirm_preview=confirm_preview,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    try:
        capabilities = generator.capabilities()
        if capabilities.provider != provider:
            raise ConfirmedProductionActionError("Hero provider does not match preview")
        request, current_options = preflight_hero_frame_generation(
            context.request, current_options, capabilities
        )
        result = HeroFrameGenerationResult.model_validate(
            generator.generate(request, current_options).model_dump()
        )
    except Exception as error:
        if isinstance(error, ConfirmedProductionActionError):
            message = str(error)
        else:
            message = "Hero generation attempt did not return a valid result"
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="generate_hero",
            shot_id=shot_id,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status=_preview_cost_status(
                workspace_root, project_id, confirm_preview
            ),
            code="generation_failed",
            message=message,
        )
    receipt_reference = _persist_model(
        workspace_root,
        project_id,
        label="hero-generation-receipt",
        model=result.receipt,
        identity=result.receipt.attempt_id,
    )
    if result.status != "succeeded":
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="generate_hero",
            shot_id=shot_id,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status=_preview_cost_status(
                workspace_root, project_id, confirm_preview
            ),
            code="generation_failed",
            message="Hero generation attempt failed",
            domain_receipt_reference=receipt_reference,
        )
    artifact = result.artifact
    try:
        if (
            result.receipt.provider != provider
            or result.receipt.model != model
            or artifact.product_id != context.request.product.product_id
            or artifact.campaign_id != context.request.campaign_id
            or artifact.storyboard_id != context.request.storyboard_id
            or artifact.asset_plan_id != context.request.asset_plan_id
            or artifact.source_request_fingerprint != context.fingerprint
            or artifact.shot_id != shot_id
        ):
            raise ConfirmedProductionActionError(
                "Hero generation result does not match confirmed request"
            )
        _require_project_media(workspace_root, project_id, artifact.local_path)
        artifact_reference = _persist_model(
            workspace_root,
            project_id,
            label="hero-frame-artifact",
            model=artifact,
            identity=artifact.id,
        )
    except Exception as error:
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="generate_hero",
            shot_id=shot_id,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status=_preview_cost_status(
                workspace_root, project_id, confirm_preview
            ),
            code="invalid_output",
            message="Hero generation result could not be safely admitted",
            domain_receipt_reference=receipt_reference,
        )
    candidate = attach_shot_reference(
        context.run, shot_id, "hero_frame_artifact", artifact_reference
    )
    return _attach_result(
        workspace_root,
        project_id,
        context=context,
        action="generate_hero",
        shot_id=shot_id,
        preview_id=confirm_preview,
        attempt_id=attempt_id,
        started_at=started_at,
        completed_at=now(),
        cost_quote_status=_preview_cost_status(
            workspace_root, project_id, confirm_preview
        ),
        artifact_reference=artifact_reference,
        domain_receipt_reference=receipt_reference,
        candidate=candidate,
    )


def _preview_cost_status(workspace_root, project_id, preview_id):
    from action_confirmation import action_preview_path, load_action_preview

    return load_action_preview(
        action_preview_path(workspace_root, project_id, preview_id)
    ).cost_quote_status


def execute_generate_video(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    shot_id: str,
    provider: str,
    model: str,
    options: VideoGenerationOptions,
    generator: VideoProvider,
    confirm_preview: str,
    attempt_id: str,
    now: Callable[[], datetime] = _utc_now,
) -> ProductionActionResult:
    try:
        current_options = VideoGenerationOptions.model_validate(options.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ConfirmedProductionActionError("Video generation options are invalid") from error
    started_at = now()
    context = _claim(
        workspace_root,
        project_id,
        action="generate_video",
        context_builder=lambda run: _video_context(run, workspace_root, shot_id),
        provider=provider,
        model=model,
        options=current_options,
        confirm_preview=confirm_preview,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    try:
        capabilities = generator.capabilities()
        if capabilities.provider != provider or capabilities.model != model:
            raise ConfirmedProductionActionError("Video provider/model does not match preview")
        preflight_video_generation(context.request, current_options, capabilities)
        result = VideoGenerationResult.model_validate(
            generator.generate(context.request).model_dump()
        )
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, ConfirmedProductionActionError)
            else "Video generation attempt did not return a valid result"
        )
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="generate_video",
            shot_id=shot_id,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status=_preview_cost_status(
                workspace_root, project_id, confirm_preview
            ),
            code="generation_failed",
            message=message,
        )
    receipt_reference = _persist_model(
        workspace_root,
        project_id,
        label="video-generation-receipt",
        model=result.receipt,
        identity=result.receipt.attempt_id,
    )
    if result.status != "succeeded":
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="generate_video",
            shot_id=shot_id,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status=_preview_cost_status(
                workspace_root, project_id, confirm_preview
            ),
            code="generation_failed",
            message="Video generation attempt failed",
            domain_receipt_reference=receipt_reference,
        )
    artifact = result.artifact
    try:
        if (
            result.receipt.provider != provider
            or result.receipt.model != model
            or artifact.product_id != context.request.product_id
            or artifact.campaign_id != context.request.campaign_id
            or artifact.storyboard_id != context.request.storyboard_id
            or artifact.asset_plan_id != context.request.asset_plan_id
            or artifact.hero_frame_artifact_id
            != context.request.hero_frame_artifact_id
            or artifact.source_request_fingerprint != context.fingerprint
            or artifact.shot_id != shot_id
        ):
            raise ConfirmedProductionActionError(
                "Video generation result does not match confirmed request"
            )
        _require_project_media(workspace_root, project_id, artifact.local_path)
        artifact_reference = _persist_model(
            workspace_root,
            project_id,
            label="video-shot-artifact",
            model=artifact,
            identity=artifact.id,
        )
    except Exception as error:
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="generate_video",
            shot_id=shot_id,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status=_preview_cost_status(
                workspace_root, project_id, confirm_preview
            ),
            code="invalid_output",
            message="Video generation result could not be safely admitted",
            domain_receipt_reference=receipt_reference,
        )
    candidate = attach_shot_reference(
        context.run, shot_id, "video_artifact", artifact_reference
    )
    return _attach_result(
        workspace_root,
        project_id,
        context=context,
        action="generate_video",
        shot_id=shot_id,
        preview_id=confirm_preview,
        attempt_id=attempt_id,
        started_at=started_at,
        completed_at=now(),
        cost_quote_status=_preview_cost_status(
            workspace_root, project_id, confirm_preview
        ),
        artifact_reference=artifact_reference,
        domain_receipt_reference=receipt_reference,
        candidate=candidate,
    )


def execute_render_editing_plan(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    options: RenderEditingPlanOptions,
    confirm_preview: str,
    attempt_id: str,
    renderer=render_editing_plan,
    now: Callable[[], datetime] = _utc_now,
) -> ProductionActionResult:
    try:
        current_options = RenderEditingPlanOptions.model_validate(options.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ConfirmedProductionActionError("Editing render options are invalid") from error
    started_at = now()
    context = _claim(
        workspace_root,
        project_id,
        action="render_editing_plan",
        context_builder=lambda run: _render_context(
            run, workspace_root, current_options
        ),
        provider=None,
        model=None,
        options=current_options,
        confirm_preview=confirm_preview,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    try:
        project = _project_root(workspace_root, project_id)
        result = EditingRenderResult.model_validate(
            renderer(
                context.plan,
                context.product,
                context.campaign,
                context.storyboard,
                context.video_artifacts,
                context.video_review_histories,
                audio_manifest=context.audio_manifest,
                storage_root=_safe_directory(project, "artifacts", "media"),
            ).model_dump()
        )
    except Exception as error:
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="render_editing_plan",
            shot_id=None,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status="not_applicable",
            code="render_failed",
            message="Editing render attempt did not return a valid result",
        )
    receipt_reference = _persist_model(
        workspace_root,
        project_id,
        label="editing-receipt",
        model=result.receipt,
        identity=result.receipt.attempt_id,
    )
    try:
        if (
            result.artifact.editing_plan_id != context.plan.id
            or result.artifact.product_id != context.product.id
            or result.artifact.campaign_id != context.campaign.id
            or result.artifact.storyboard_id != context.storyboard.id
            or result.receipt.editing_plan_id != context.plan.id
            or result.receipt.artifact_id != result.artifact.id
        ):
            raise ConfirmedProductionActionError(
                "Editing render result does not match confirmed plan"
            )
        _require_project_media(
            workspace_root, project_id, result.artifact.local_path
        )
        artifact_reference = _persist_model(
            workspace_root,
            project_id,
            label="editing-artifact",
            model=result.artifact,
            identity=result.artifact.id,
        )
    except Exception as error:
        _failed(
            workspace_root,
            project_id,
            context=context,
            action="render_editing_plan",
            shot_id=None,
            preview_id=confirm_preview,
            attempt_id=attempt_id,
            started_at=started_at,
            completed_at=now(),
            cost_quote_status="not_applicable",
            code="invalid_output",
            message="Editing render result could not be safely admitted",
            domain_receipt_reference=receipt_reference,
        )
    candidate = attach_run_reference(
        context.run, "editing_artifact", artifact_reference
    )
    return _attach_result(
        workspace_root,
        project_id,
        context=context,
        action="render_editing_plan",
        shot_id=None,
        preview_id=confirm_preview,
        attempt_id=attempt_id,
        started_at=started_at,
        completed_at=now(),
        cost_quote_status="not_applicable",
        artifact_reference=artifact_reference,
        domain_receipt_reference=receipt_reference,
        candidate=candidate,
    )
