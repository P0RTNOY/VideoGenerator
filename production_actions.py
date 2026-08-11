"""Explicit one-operation local actions layered over AdProductionRun."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple, Union

from pydantic import ConfigDict, Field, ValidationError

from action_confirmation import (
    ActionAttemptReceipt,
    ActionConfirmationError,
    ActionFailureDiagnostic,
    claim_action_attempt,
    save_action_attempt_receipt,
)
from ad_production_run import (
    AdProductionRun,
    AdProductionRunError,
    RunDocumentReference,
    RunOperationDiagnostic,
    attach_run_reference,
    attach_shot_reference,
    load_ad_production_run,
    record_operation_diagnostic,
    save_ad_production_run,
)
from campaign import AdCampaign
from editing_artifact import EditingArtifact
from editing_audio_assets import EditingAudioManifest
from editing_plan import (
    EditingMusicSelection,
    EditingOverlayTiming,
    EditingPlan,
    EditingSegmentSelection,
    EditingSfxSelection,
    build_editing_plan,
    save_editing_plan,
)
from final_ad_review import (
    FinalAdReview,
    FinalAdReviewHistory,
    append_final_ad_review,
    load_final_ad_review_history,
    save_final_ad_review_history,
)
from hero_frame_artifact import HeroFrameArtifact
from hero_frame_review import (
    HeroFrameReview,
    HeroFrameReviewHistory,
    append_hero_frame_review,
    load_hero_frame_review_history,
    save_hero_frame_review_history,
)
from product import NonEmptyStr, Product, ProductModel, save_product
from product_asset_manifest import (
    ProductAssetManifest,
    load_product_asset_manifest,
    save_product_asset_manifest,
)
from product_derived_asset_manifest import (
    build_product_derived_asset_manifest,
    save_product_derived_asset_manifest,
)
from product_image_downloads import DownloadedProductImage, download_product_image
from product_reference_sheets import build_product_reference_sheet_set
from product_research import research_product
from storyboard import Storyboard
from video_shot_artifact import VideoShotArtifact
from video_shot_review import (
    VideoShotReview,
    VideoShotReviewHistory,
    append_video_shot_review,
    build_editing_source_handoff,
    load_video_shot_review_history,
    save_video_shot_review_history,
)


class ProductionActionResult(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    receipt: ActionAttemptReceipt
    reference: RunDocumentReference
    stored_run_revision: int


class ProductionActionError(RuntimeError):
    """Raised after a failed local action has recorded its safe diagnostics."""


class EditingPlanActionInput(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    segment_selections: Tuple[EditingSegmentSelection, ...] = Field(min_length=1)
    overlay_timings: Tuple[EditingOverlayTiming, ...] = ()
    music_selection: Optional[EditingMusicSelection] = None
    sfx_selections: Tuple[EditingSfxSelection, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _project_root(workspace_root: Union[str, Path], project_id: str) -> Path:
    workspace = Path(workspace_root).resolve(strict=True)
    try:
        project = (workspace / "projects" / project_id).resolve(strict=True)
        project.relative_to(workspace)
    except (OSError, ValueError) as error:
        raise ProductionActionError("project directory is unavailable or unsafe") from error
    if not project.is_dir():
        raise ProductionActionError("project directory is not a directory")
    return project


def _safe_directory(project: Path, *parts: str) -> Path:
    current = project
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ProductionActionError(
                "action output path must not contain a symbolic link"
            )
        try:
            current.mkdir(exist_ok=True)
            current.resolve(strict=True).relative_to(project)
        except (OSError, ValueError) as error:
            raise ProductionActionError("action output directory is unsafe") from error
    return current


def _document_path(project: Path, label: str, model: ProductModel) -> Path:
    data = model.model_dump_json(
        ensure_ascii=False,
        exclude_none=True,
    ).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    return _safe_directory(project, "documents") / f"{label}-{digest}.json"


def _persist_document(model: ProductModel, path: Path, writer) -> Path:
    expected = (
        model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True) + "\n"
    ).encode("utf-8")
    if path.is_symlink():
        raise ProductionActionError("action output file must not be a symbolic link")
    if path.exists():
        try:
            if not path.is_file() or path.read_bytes() != expected:
                raise ProductionActionError(
                    "content-addressed action output contains different data"
                )
        except OSError as error:
            raise ProductionActionError("action output file is unreadable") from error
        return path
    writer(model, path)
    return path


def _reference(
    workspace_root: Union[str, Path],
    path: Path,
    identity: str,
) -> RunDocumentReference:
    workspace = Path(workspace_root).resolve(strict=True)
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(workspace).as_posix()
        data = resolved.read_bytes()
    except (OSError, ValueError) as error:
        raise ProductionActionError("persisted action document is outside workspace") from error
    return RunDocumentReference(
        id=identity,
        path=relative,
        content_sha256=hashlib.sha256(data).hexdigest(),
    )


def _load_reference_model(
    workspace_root: Union[str, Path],
    reference: RunDocumentReference,
    model_type,
):
    workspace = Path(workspace_root).resolve(strict=True)
    candidate = workspace / reference.path
    try:
        if candidate.is_symlink():
            raise ValueError("symbolic link")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace)
        data = resolved.read_bytes()
    except (OSError, ValueError) as error:
        raise ProductionActionError("run reference is unavailable or unsafe") from error
    if (
        reference.content_sha256 is not None
        and hashlib.sha256(data).hexdigest() != reference.content_sha256
    ):
        raise ProductionActionError("run reference content changed")
    try:
        return model_type.model_validate_json(data)
    except ValidationError as error:
        raise ProductionActionError(
            f"run reference is not valid {model_type.__name__} JSON"
        ) from error


def _clear_failure(run: AdProductionRun) -> AdProductionRun:
    return AdProductionRun.model_validate(
        {**run.model_dump(), "latest_operation_diagnostic": None}
    )


def _record_failure(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    action: str,
    code: str,
    message: str,
    recorded_at: datetime,
    shot_id: Optional[str] = None,
) -> AdProductionRun:
    current = load_ad_production_run(workspace_root, project_id)
    candidate = record_operation_diagnostic(
        current,
        RunOperationDiagnostic(
            action=action,
            shot_id=shot_id,
            code=code,
            message=message,
            recorded_at=recorded_at,
        ),
    )
    return save_ad_production_run(
        candidate,
        workspace_root,
        project_id,
        expected_revision=current.revision,
    )


def _receipt(
    *,
    attempt_id: str,
    project_id: str,
    action: str,
    started_at: datetime,
    completed_at: datetime,
    run_revision: int,
    request_fingerprint: str,
    status: str,
    reference: Optional[RunDocumentReference],
    failure: Optional[ActionFailureDiagnostic],
    attachment_outcome: str,
) -> ActionAttemptReceipt:
    return ActionAttemptReceipt(
        attempt_id=attempt_id,
        project_id=project_id,
        action=action,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        run_revision_at_start=run_revision,
        request_fingerprint=request_fingerprint,
        artifact_reference=reference,
        failure=failure,
        attachment_outcome=attachment_outcome,
    )


def _execute_nonpaid(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    action: str,
    request_fingerprint: str,
    attempt_id: str,
    now: Callable[[], datetime],
    operation: Callable[[AdProductionRun], Tuple[RunDocumentReference, AdProductionRun]],
) -> ProductionActionResult:
    run = load_ad_production_run(workspace_root, project_id)
    started_at = now()
    try:
        claim_action_attempt(
            workspace_root,
            project_id=project_id,
            action=action,
            attempt_id=attempt_id,
            claimed_at=started_at,
        )
    except ActionConfirmationError as error:
        raise ProductionActionError("action attempt ID is invalid or already used") from error
    reference = None
    try:
        reference, candidate = operation(run)
    except Exception as error:
        completed_at = now()
        message = f"{action} did not produce a valid result"
        failure = ActionFailureDiagnostic(code="action_failed", message=message)
        _record_failure(
            workspace_root,
            project_id,
            action=action,
            code=failure.code,
            message=failure.message,
            recorded_at=completed_at,
        )
        save_action_attempt_receipt(
            _receipt(
                attempt_id=attempt_id,
                project_id=project_id,
                action=action,
                started_at=started_at,
                completed_at=completed_at,
                run_revision=run.revision,
                request_fingerprint=request_fingerprint,
                status="failed",
                reference=None,
                failure=failure,
                attachment_outcome="not_attached",
            ),
            workspace_root,
        )
        raise ProductionActionError(message) from error

    try:
        stored = save_ad_production_run(
            _clear_failure(candidate),
            workspace_root,
            project_id,
            expected_revision=run.revision,
        )
    except AdProductionRunError:
        completed_at = now()
        failure = ActionFailureDiagnostic(
            code="attachment_conflict",
            message="Result was preserved but run attachment conflicted",
        )
        latest = _record_failure(
            workspace_root,
            project_id,
            action=action,
            code=failure.code,
            message=failure.message,
            recorded_at=completed_at,
        )
        receipt = _receipt(
            attempt_id=attempt_id,
            project_id=project_id,
            action=action,
            started_at=started_at,
            completed_at=completed_at,
            run_revision=run.revision,
            request_fingerprint=request_fingerprint,
            status="artifact_created_unattached",
            reference=reference,
            failure=failure,
            attachment_outcome="conflict",
        )
        save_action_attempt_receipt(receipt, workspace_root)
        return ProductionActionResult(
            receipt=receipt,
            reference=reference,
            stored_run_revision=latest.revision,
        )

    completed_at = now()
    receipt = _receipt(
        attempt_id=attempt_id,
        project_id=project_id,
        action=action,
        started_at=started_at,
        completed_at=completed_at,
        run_revision=run.revision,
        request_fingerprint=request_fingerprint,
        status="succeeded_and_attached",
        reference=reference,
        failure=None,
        attachment_outcome="attached",
    )
    save_action_attempt_receipt(receipt, workspace_root)
    return ProductionActionResult(
        receipt=receipt,
        reference=reference,
        stored_run_revision=stored.revision,
    )


def execute_research_product(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    product_url: str,
    researcher=research_product,
    now: Callable[[], datetime] = _utc_now,
    attempt_id: str,
) -> ProductionActionResult:
    request_fingerprint = _fingerprint(
        {"action": "research_product", "product_url": product_url}
    )

    def operation(run: AdProductionRun):
        project = _project_root(workspace_root, project_id)
        _safe_directory(project, "documents")
        if (
            run.product_input is None
            or run.product_input.kind != "product_url"
            or run.product_input.value != product_url
        ):
            raise ProductionActionError("research URL must match current Product input")
        product = Product.model_validate(researcher(product_url).model_dump())
        path = _document_path(project, "product", product)
        _persist_document(product, path, save_product)
        reference = _reference(workspace_root, path, product.id)
        return reference, attach_run_reference(run, "product", reference)

    return _execute_nonpaid(
        workspace_root,
        project_id,
        action="research_product",
        request_fingerprint=request_fingerprint,
        attempt_id=attempt_id,
        now=now,
        operation=operation,
    )


def _required_product(run: AdProductionRun, workspace_root) -> Product:
    if run.product is None:
        raise ProductionActionError("Product reference is required")
    product = _load_reference_model(workspace_root, run.product, Product)
    if product.id != run.product.id:
        raise ProductionActionError("Product reference identity changed")
    return product


def execute_download_assets(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    image_ids: Sequence[str],
    downloader=download_product_image,
    now: Callable[[], datetime] = _utc_now,
    attempt_id: str,
) -> ProductionActionResult:
    fingerprint_ids = (
        image_ids
        if isinstance(image_ids, (str, bytes)) or image_ids is None
        else tuple(image_ids)
    )
    request_fingerprint = _fingerprint(
        {"action": "download_assets", "image_ids": fingerprint_ids}
    )

    def operation(run: AdProductionRun):
        if isinstance(image_ids, (str, bytes)) or not image_ids:
            raise ProductionActionError("explicit ProductImage IDs are required")
        selected_ids = tuple(image_ids)
        if any(
            not isinstance(item, str) or not item.strip() for item in selected_ids
        ):
            raise ProductionActionError("ProductImage IDs must be non-empty strings")
        if len(selected_ids) != len(set(selected_ids)):
            raise ProductionActionError("ProductImage IDs must be unique")
        product = _required_product(run, workspace_root)
        images_by_id = {item.id: item for item in product.images}
        if len(images_by_id) != len(product.images):
            raise ProductionActionError("ProductImage IDs are ambiguous")
        unknown = [item for item in selected_ids if item not in images_by_id]
        if unknown:
            raise ProductionActionError(
                f"unknown explicit ProductImage IDs: {', '.join(unknown)}"
            )
        existing = []
        if run.original_asset_manifest is not None:
            current = _load_reference_model(
                workspace_root,
                run.original_asset_manifest,
                ProductAssetManifest,
            )
            existing = [
                item.model_copy(deep=True)
                for item in current.images
                if item.source_image_id not in selected_ids
            ]
        project = _project_root(workspace_root, project_id)
        storage_root = _safe_directory(project, "artifacts", "product-images")
        downloaded = [
            DownloadedProductImage.model_validate(
                downloader(
                    product.id,
                    images_by_id[image_id],
                    storage_root=storage_root,
                ).model_dump()
            )
            for image_id in selected_ids
        ]
        manifest = ProductAssetManifest(
            product_id=product.id,
            images=[*existing, *downloaded],
        )
        path = _document_path(project, "product-assets", manifest)
        _persist_document(manifest, path, save_product_asset_manifest)
        reference = _reference(workspace_root, path, manifest.product_id)
        return reference, attach_run_reference(
            run, "original_asset_manifest", reference
        )

    return _execute_nonpaid(
        workspace_root,
        project_id,
        action="download_assets",
        request_fingerprint=request_fingerprint,
        attempt_id=attempt_id,
        now=now,
        operation=operation,
    )


def execute_prepare_reference_assets(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    now: Callable[[], datetime] = _utc_now,
    attempt_id: str,
) -> ProductionActionResult:
    run = load_ad_production_run(workspace_root, project_id)
    request_fingerprint = _fingerprint(
        {
            "action": "prepare_reference_assets",
            "product": None if run.product is None else run.product.model_dump(),
            "originals": (
                None
                if run.original_asset_manifest is None
                else run.original_asset_manifest.model_dump()
            ),
        }
    )

    def operation(current: AdProductionRun):
        product = _required_product(current, workspace_root)
        if current.original_asset_manifest is None:
            raise ProductionActionError("original asset manifest is required")
        originals = _load_reference_model(
            workspace_root,
            current.original_asset_manifest,
            ProductAssetManifest,
        )
        project = _project_root(workspace_root, project_id)
        storage_root = _safe_directory(project, "artifacts", "reference-sheets")
        sheets = build_product_reference_sheet_set(
            product,
            originals,
            storage_root=storage_root,
        )
        manifest = build_product_derived_asset_manifest(originals, sheets)
        path = _document_path(project, "derived-assets", manifest)
        _persist_document(
            manifest, path, save_product_derived_asset_manifest
        )
        reference = _reference(workspace_root, path, manifest.product_id)
        return reference, attach_run_reference(
            current, "derived_asset_manifest", reference
        )

    return _execute_nonpaid(
        workspace_root,
        project_id,
        action="prepare_reference_assets",
        request_fingerprint=request_fingerprint,
        attempt_id=attempt_id,
        now=now,
        operation=operation,
    )


def _run_shot(run: AdProductionRun, shot_id: str):
    matches = [item for item in run.shots if item.shot_id == shot_id]
    if len(matches) != 1:
        raise ProductionActionError(f"run shot must resolve exactly once: {shot_id}")
    return matches[0]


def _required_run_model(
    run: AdProductionRun,
    workspace_root,
    field_name: str,
    model_type,
    identity_reader,
):
    reference = getattr(run, field_name)
    if reference is None:
        raise ProductionActionError(f"{field_name} reference is required")
    value = _load_reference_model(workspace_root, reference, model_type)
    if identity_reader(value) != reference.id:
        raise ProductionActionError(f"{field_name} reference identity changed")
    return value


def execute_record_hero_review(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    shot_id: str,
    review: HeroFrameReview,
    now: Callable[[], datetime] = _utc_now,
    attempt_id: str,
) -> ProductionActionResult:
    try:
        current_review = HeroFrameReview.model_validate(review.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ProductionActionError("Hero review record is invalid") from error
    request_fingerprint = _fingerprint(
        {
            "action": "record_hero_review",
            "shot_id": shot_id,
            "review": current_review.model_dump(mode="json"),
        }
    )

    def operation(run: AdProductionRun):
        shot = _run_shot(run, shot_id)
        if shot.hero_frame_artifact is None:
            raise ProductionActionError("Hero Frame artifact reference is required")
        artifact = _load_reference_model(
            workspace_root, shot.hero_frame_artifact, HeroFrameArtifact
        )
        if artifact.id != shot.hero_frame_artifact.id or artifact.shot_id != shot_id:
            raise ProductionActionError("Hero Frame artifact binding changed")
        project = _project_root(workspace_root, project_id)
        storage = _safe_directory(project, "artifacts", "domain")
        history = load_hero_frame_review_history(artifact, storage)
        updated = append_hero_frame_review(history, current_review, artifact)
        path = save_hero_frame_review_history(updated, storage)
        reference = _reference(
            workspace_root, path, updated.review_context.id
        )
        return reference, attach_shot_reference(
            run, shot_id, "hero_frame_review_history", reference
        )

    return _execute_nonpaid(
        workspace_root,
        project_id,
        action="record_hero_review",
        request_fingerprint=request_fingerprint,
        attempt_id=attempt_id,
        now=now,
        operation=operation,
    )


def execute_record_video_review(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    shot_id: str,
    review: VideoShotReview,
    now: Callable[[], datetime] = _utc_now,
    attempt_id: str,
) -> ProductionActionResult:
    try:
        current_review = VideoShotReview.model_validate(review.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ProductionActionError("Video review record is invalid") from error
    request_fingerprint = _fingerprint(
        {
            "action": "record_video_review",
            "shot_id": shot_id,
            "review": current_review.model_dump(mode="json"),
        }
    )

    def operation(run: AdProductionRun):
        storyboard = _required_run_model(
            run, workspace_root, "storyboard", Storyboard, lambda item: item.id
        )
        shot = _run_shot(run, shot_id)
        if shot.video_artifact is None:
            raise ProductionActionError("Video artifact reference is required")
        artifact = _load_reference_model(
            workspace_root, shot.video_artifact, VideoShotArtifact
        )
        if artifact.id != shot.video_artifact.id or artifact.shot_id != shot_id:
            raise ProductionActionError("Video artifact binding changed")
        project = _project_root(workspace_root, project_id)
        storage = _safe_directory(project, "artifacts", "domain")
        history = load_video_shot_review_history(artifact, storyboard, storage)
        updated = append_video_shot_review(
            history, current_review, artifact, storyboard
        )
        path = save_video_shot_review_history(updated, storage)
        reference = _reference(
            workspace_root, path, updated.review_context.id
        )
        return reference, attach_shot_reference(
            run, shot_id, "video_review_history", reference
        )

    return _execute_nonpaid(
        workspace_root,
        project_id,
        action="record_video_review",
        request_fingerprint=request_fingerprint,
        attempt_id=attempt_id,
        now=now,
        operation=operation,
    )


def _editing_context(run: AdProductionRun, workspace_root):
    product = _required_run_model(
        run, workspace_root, "product", Product, lambda item: item.id
    )
    campaign = _required_run_model(
        run, workspace_root, "campaign", AdCampaign, lambda item: item.id
    )
    storyboard = _required_run_model(
        run, workspace_root, "storyboard", Storyboard, lambda item: item.id
    )
    artifacts = []
    histories = []
    for storyboard_shot in storyboard.shots:
        shot = _run_shot(run, storyboard_shot.id)
        if shot.video_artifact is None or shot.video_review_history is None:
            raise ProductionActionError(
                f"approved Video artifact and review are required: {shot.shot_id}"
            )
        artifact = _load_reference_model(
            workspace_root, shot.video_artifact, VideoShotArtifact
        )
        history = _load_reference_model(
            workspace_root, shot.video_review_history, VideoShotReviewHistory
        )
        if artifact.id != shot.video_artifact.id:
            raise ProductionActionError("Video artifact reference identity changed")
        if history.review_context.id != shot.video_review_history.id:
            raise ProductionActionError("Video review reference identity changed")
        artifacts.append(artifact)
        histories.append(history)
    return product, campaign, storyboard, tuple(artifacts), tuple(histories)


def execute_build_editing_plan(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    authored_input: EditingPlanActionInput,
    now: Callable[[], datetime] = _utc_now,
    attempt_id: str,
) -> ProductionActionResult:
    try:
        current_input = EditingPlanActionInput.model_validate(
            authored_input.model_dump()
        )
    except (AttributeError, ValidationError) as error:
        raise ProductionActionError("EditingPlan authored input is invalid") from error
    request_fingerprint = _fingerprint(
        {
            "action": "build_editing_plan",
            "input": current_input.model_dump(mode="json"),
        }
    )

    def operation(run: AdProductionRun):
        product, campaign, storyboard, artifacts, histories = _editing_context(
            run, workspace_root
        )
        handoffs = tuple(
            build_editing_source_handoff(artifact, storyboard, history)
            for artifact, history in zip(artifacts, histories)
        )
        audio_manifest = None
        if run.editing_audio_manifest is not None:
            audio_manifest = _load_reference_model(
                workspace_root,
                run.editing_audio_manifest,
                EditingAudioManifest,
            )
            if audio_manifest.product_id != run.editing_audio_manifest.id:
                raise ProductionActionError("editing audio reference identity changed")
        plan = build_editing_plan(
            product,
            campaign,
            storyboard,
            handoffs,
            current_input.segment_selections,
            current_input.overlay_timings,
            audio_manifest=audio_manifest,
            music_selection=current_input.music_selection,
            sfx_selections=current_input.sfx_selections,
        )
        project = _project_root(workspace_root, project_id)
        storage = _safe_directory(project, "artifacts", "domain")
        path = save_editing_plan(plan, storage)
        reference = _reference(workspace_root, path, plan.id)
        return reference, attach_run_reference(run, "editing_plan", reference)

    return _execute_nonpaid(
        workspace_root,
        project_id,
        action="build_editing_plan",
        request_fingerprint=request_fingerprint,
        attempt_id=attempt_id,
        now=now,
        operation=operation,
    )


def execute_record_final_review(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    review: FinalAdReview,
    now: Callable[[], datetime] = _utc_now,
    attempt_id: str,
) -> ProductionActionResult:
    try:
        current_review = FinalAdReview.model_validate(review.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ProductionActionError("Final Ad review record is invalid") from error
    request_fingerprint = _fingerprint(
        {
            "action": "record_final_review",
            "review": current_review.model_dump(mode="json"),
        }
    )

    def operation(run: AdProductionRun):
        product, campaign, storyboard, artifacts, histories = _editing_context(
            run, workspace_root
        )
        plan = _required_run_model(
            run, workspace_root, "editing_plan", EditingPlan, lambda item: item.id
        )
        artifact = _required_run_model(
            run,
            workspace_root,
            "editing_artifact",
            EditingArtifact,
            lambda item: item.id,
        )
        project = _project_root(workspace_root, project_id)
        storage = _safe_directory(project, "artifacts", "domain")
        history = load_final_ad_review_history(
            artifact,
            plan,
            product,
            campaign,
            storyboard,
            artifacts,
            histories,
            storage,
        )
        updated = append_final_ad_review(
            history,
            current_review,
            artifact,
            plan,
            product,
            campaign,
            storyboard,
            artifacts,
            histories,
        )
        path = save_final_ad_review_history(updated, storage)
        reference = _reference(
            workspace_root, path, updated.review_context.id
        )
        return reference, attach_run_reference(
            run, "final_review_history", reference
        )

    return _execute_nonpaid(
        workspace_root,
        project_id,
        action="record_final_review",
        request_fingerprint=request_fingerprint,
        attempt_id=attempt_id,
        now=now,
        operation=operation,
    )
