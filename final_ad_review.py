"""Manual, exact-context review authority for final Campaign deliverables."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Sequence, Tuple, Union

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from campaign import AdCampaign
from editing_artifact import (
    EditingArtifact,
    EditingArtifactValidationError,
    read_verified_editing_artifact_bytes,
)
from editing_plan import AudioMixPolicy, EditingOutputSpec, EditingPlan
from product import NonEmptyStr, Product, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name
from storyboard import Storyboard, StoryboardContextError, validate_storyboard
from video_shot_artifact import VideoShotArtifact
from video_shot_review import (
    EditingSourceHandoff,
    VideoShotReviewError,
    VideoShotReviewHistory,
    build_editing_source_handoff,
)


FINAL_AD_REVIEW_CONTEXT_PREFIX = "final-ad-review-context:"

ChecklistResult = Literal["pass", "fail", "not_applicable"]
FinalAdReviewDecision = Literal[
    "approved_final_campaign_deliverable",
    "rejected_final_campaign_deliverable",
]
FinalAdReviewState = Literal[
    "pending_review",
    "approved_final_campaign_deliverable",
    "rejected_final_campaign_deliverable",
]
FinalAdRejectionReason = Literal[
    "source_authorization_invalid",
    "shot_order_or_continuity_issue",
    "product_fidelity_issue",
    "overlay_missing_or_duplicate",
    "overlay_readability_issue",
    "factual_claim_copy_mismatch",
    "creative_or_cta_copy_issue",
    "duration_or_format_issue",
    "audio_mix_issue",
    "provider_audio_present",
    "music_sfx_sync_issue",
    "unexpected_visual_or_text_artifact",
    "other",
]


class FrozenFinalAdReviewModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class FinalAdSourceSnapshot(FrozenFinalAdReviewModel):
    shot_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    video_artifact_id: NonEmptyStr
    video_artifact_sha256: Sha256Hex
    video_review_context_id: NonEmptyStr
    authorizing_video_review_id: NonEmptyStr
    source_start_ms: int = Field(ge=0, strict=True)
    source_end_ms: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_exact_source_identity(self) -> "FinalAdSourceSnapshot":
        if self.video_artifact_id != f"video-shot:{self.video_artifact_sha256}":
            raise ValueError("Final Ad source artifact ID must contain its SHA-256")
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("Final Ad source range must be positive")
        return self


class FinalAdOverlaySnapshot(FrozenFinalAdReviewModel):
    shot_id: NonEmptyStr
    overlay_index: int = Field(ge=0, strict=True)
    kind: Literal["claim", "creative", "product_name", "cta"]
    resolved_text: NonEmptyStr
    timeline_start_ms: int = Field(ge=0, strict=True)
    timeline_end_ms: int = Field(gt=0, strict=True)
    placement: Literal["top_center", "center", "bottom_center"]
    style_preset_id: Literal["social_bold_v1"]


class FinalAdMusicSnapshot(FrozenFinalAdReviewModel):
    asset_id: NonEmptyStr
    asset_sha256: Sha256Hex
    source_start_ms: int = Field(ge=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)


class FinalAdSfxSnapshot(FrozenFinalAdReviewModel):
    asset_id: NonEmptyStr
    asset_sha256: Sha256Hex
    timeline_start_ms: int = Field(ge=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)


def _canonical_context_payload(values: dict) -> bytes:
    payload = dict(values)
    payload.pop("id", None)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _context_id(values: dict) -> str:
    return FINAL_AD_REVIEW_CONTEXT_PREFIX + hashlib.sha256(
        _canonical_context_payload(values)
    ).hexdigest()


class FinalAdReviewContext(FrozenFinalAdReviewModel):
    """Path-free snapshot of exact final bytes, intent, and source authority."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    editing_artifact_id: NonEmptyStr
    editing_artifact_sha256: Sha256Hex
    editing_plan_id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    duration_ms: int = Field(gt=0, strict=True)
    width: Literal[720]
    height: Literal[1280]
    container: Literal["mp4"]
    video_codec: Literal["h264"]
    audio_codec: Literal["aac"]
    controlled_audio_stream_count: Literal[1] = 1
    sources: Tuple[FinalAdSourceSnapshot, ...] = Field(min_length=1)
    overlays: Tuple[FinalAdOverlaySnapshot, ...] = Field(default_factory=tuple)
    music: Optional[FinalAdMusicSnapshot] = None
    sfx: Tuple[FinalAdSfxSnapshot, ...] = Field(default_factory=tuple)
    source_video_audio: Literal["discard"] = "discard"
    audio_mix: AudioMixPolicy
    output: EditingOutputSpec

    @model_validator(mode="after")
    def require_canonical_identity(self) -> "FinalAdReviewContext":
        if self.editing_artifact_id != f"edited-ad:{self.editing_artifact_sha256}":
            raise ValueError("Final Ad Review artifact ID must contain its SHA-256")
        source_shots = [item.shot_id for item in self.sources]
        if len(source_shots) != len(set(source_shots)):
            raise ValueError("Final Ad Review sources must have unique shot IDs")
        overlay_keys = [(item.shot_id, item.overlay_index) for item in self.overlays]
        if len(overlay_keys) != len(set(overlay_keys)):
            raise ValueError("Final Ad Review overlays must have unique identities")
        expected = _context_id(self.model_dump(mode="json", exclude={"id"}))
        if self.id != expected:
            raise ValueError("Final Ad Review context ID must match its canonical snapshot")
        return self


class FinalAdReviewChecklist(FrozenFinalAdReviewModel):
    artifact_and_plan_binding_correct: Literal["pass", "fail"]
    shot_order_and_visual_continuity: Literal["pass", "fail"]
    product_fidelity_across_shots: Literal["pass", "fail"]
    overlay_completeness: Literal["pass", "fail"]
    overlay_readability: Literal["pass", "fail"]
    factual_claim_overlay_copy: ChecklistResult
    creative_or_cta_copy: ChecklistResult
    final_duration_and_technical_format: Literal["pass", "fail"]
    audio_mix_quality: Literal["pass", "fail"]
    provider_audio_absent: Literal["pass", "fail"]
    music_sfx_synchronization: ChecklistResult
    no_unexpected_visual_or_text_artifacts: Literal["pass", "fail"]


class FinalAdReview(FrozenFinalAdReviewModel):
    review_id: NonEmptyStr
    review_context_id: NonEmptyStr
    reviewer_actor_id: NonEmptyStr
    reviewed_at: datetime
    decision: FinalAdReviewDecision
    checklist: FinalAdReviewChecklist
    rejection_reasons: Tuple[FinalAdRejectionReason, ...] = Field(default_factory=tuple)
    notes: Optional[NonEmptyStr] = None

    @field_validator("reviewed_at")
    @classmethod
    def require_utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Final Ad review timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_decision_consistency(self) -> "FinalAdReview":
        if len(self.rejection_reasons) != len(set(self.rejection_reasons)):
            raise ValueError("Final Ad rejection reasons must not contain duplicates")
        checklist_values = self.checklist.model_dump().values()
        if self.decision == "approved_final_campaign_deliverable":
            if "fail" in checklist_values:
                raise ValueError("approved Final Ad review cannot contain checklist failures")
            if self.rejection_reasons:
                raise ValueError("approved Final Ad review cannot contain rejection reasons")
            return self
        if not self.rejection_reasons:
            raise ValueError("rejected Final Ad review requires a rejection reason")
        if "other" in self.rejection_reasons and self.notes is None:
            raise ValueError("other Final Ad rejection reason requires notes")
        return self


def _validate_review_for_context(
    review: FinalAdReview,
    context: FinalAdReviewContext,
) -> None:
    if review.review_context_id != context.id:
        raise FinalAdReviewError("Final Ad review does not match exact current context")
    has_claim = any(item.kind == "claim" for item in context.overlays)
    has_creative_or_cta = any(
        item.kind in ("creative", "cta") for item in context.overlays
    )
    has_music_or_sfx = context.music is not None or bool(context.sfx)
    optional_checks = (
        (
            "factual_claim_overlay_copy",
            review.checklist.factual_claim_overlay_copy,
            has_claim,
        ),
        (
            "creative_or_cta_copy",
            review.checklist.creative_or_cta_copy,
            has_creative_or_cta,
        ),
        (
            "music_sfx_synchronization",
            review.checklist.music_sfx_synchronization,
            has_music_or_sfx,
        ),
    )
    for field_name, value, content_exists in optional_checks:
        if value == "not_applicable" and content_exists:
            raise FinalAdReviewError(
                f"{field_name} cannot be not_applicable when matching content exists"
            )
    if review.decision == "approved_final_campaign_deliverable":
        if any(value == "fail" for value in review.checklist.model_dump().values()):
            raise FinalAdReviewError("Final Ad approval requires a passing checklist")


class FinalAdReviewHistory(FrozenFinalAdReviewModel):
    schema_version: Literal[1] = 1
    review_context: FinalAdReviewContext
    reviews: Tuple[FinalAdReview, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def require_append_order_and_context(self) -> "FinalAdReviewHistory":
        review_ids = [item.review_id for item in self.reviews]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("Final Ad review IDs must be unique")
        previous_time = None
        for review in self.reviews:
            try:
                _validate_review_for_context(review, self.review_context)
            except FinalAdReviewError as error:
                raise ValueError(str(error)) from error
            if previous_time is not None and review.reviewed_at < previous_time:
                raise ValueError("Final Ad review timestamps must preserve append order")
            previous_time = review.reviewed_at
        return self


class FinalAdReviewError(ValueError):
    """Base error for controlled Final Ad review failures."""


class FinalAdReviewArtifactError(FinalAdReviewError):
    """Raised when current final Editing bytes or metadata are stale."""


class FinalAdReviewAuthorizationError(FinalAdReviewError):
    """Raised when Plan bindings or transitive source authority are stale."""


class FinalAdReviewPersistenceError(FinalAdReviewError):
    """Raised when append-only local Final Ad review persistence fails."""


def _validated_domain_inputs(
    artifact: EditingArtifact,
    plan: EditingPlan,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
):
    try:
        current_artifact = EditingArtifact.model_validate(artifact.model_dump())
        current_plan = EditingPlan.model_validate(plan.model_dump())
        current_product = Product.model_validate(product.model_dump())
        current_campaign = AdCampaign.model_validate(campaign.model_dump())
        current_storyboard = Storyboard.model_validate(storyboard.model_dump())
    except ValidationError as error:
        raise FinalAdReviewAuthorizationError(
            f"Final Ad review inputs are structurally invalid: {error}"
        ) from error
    try:
        current_storyboard = validate_storyboard(current_storyboard, current_campaign)
    except StoryboardContextError as error:
        raise FinalAdReviewAuthorizationError(str(error)) from error
    expected_product_id = current_campaign.claim_selection.product_id
    if not (
        current_product.id
        == expected_product_id
        == current_plan.product_id
        == current_artifact.product_id
    ):
        raise FinalAdReviewAuthorizationError("Final Ad Product bindings do not match")
    if not (
        current_campaign.id
        == current_storyboard.campaign_id
        == current_plan.campaign_id
        == current_artifact.campaign_id
    ):
        raise FinalAdReviewAuthorizationError("Final Ad Campaign bindings do not match")
    if not (
        current_storyboard.id
        == current_plan.storyboard_id
        == current_artifact.storyboard_id
    ):
        raise FinalAdReviewAuthorizationError("Final Ad Storyboard bindings do not match")
    if current_artifact.editing_plan_id != current_plan.id:
        raise FinalAdReviewAuthorizationError("Editing artifact does not match Editing Plan")
    if current_artifact.duration_ms != current_plan.target_duration_ms:
        raise FinalAdReviewAuthorizationError("Editing artifact duration does not match Plan")
    if current_artifact.source_video_audio_discarded is not True:
        raise FinalAdReviewAuthorizationError("Editing artifact lacks source-audio discard evidence")
    try:
        read_verified_editing_artifact_bytes(current_artifact)
    except EditingArtifactValidationError as error:
        raise FinalAdReviewArtifactError(str(error)) from error
    return (
        current_artifact,
        current_plan,
        current_product,
        current_campaign,
        current_storyboard,
    )


def _current_handoffs(
    storyboard: Storyboard,
    artifacts: Sequence[VideoShotArtifact],
    histories: Sequence[VideoShotReviewHistory],
) -> Tuple[EditingSourceHandoff, ...]:
    try:
        current_artifacts = tuple(
            VideoShotArtifact.model_validate(item.model_dump()) for item in artifacts
        )
        current_histories = tuple(
            VideoShotReviewHistory.model_validate(item.model_dump()) for item in histories
        )
    except (TypeError, ValidationError) as error:
        raise FinalAdReviewAuthorizationError(
            f"source authorization inputs are invalid: {error}"
        ) from error
    artifacts_by_shot = {}
    histories_by_shot = {}
    for artifact in current_artifacts:
        if artifact.shot_id in artifacts_by_shot:
            raise FinalAdReviewAuthorizationError(
                f"duplicate current source artifact for shot {artifact.shot_id}"
            )
        artifacts_by_shot[artifact.shot_id] = artifact
    for history in current_histories:
        shot_id = history.review_context.shot_id
        if shot_id in histories_by_shot:
            raise FinalAdReviewAuthorizationError(
                f"duplicate current source review history for shot {shot_id}"
            )
        histories_by_shot[shot_id] = history
    expected_shots = [shot.id for shot in storyboard.shots]
    if set(artifacts_by_shot) != set(expected_shots) or set(histories_by_shot) != set(expected_shots):
        raise FinalAdReviewAuthorizationError(
            "current source artifacts and reviews must cover every Storyboard shot"
        )
    handoffs = []
    for shot_id in expected_shots:
        try:
            handoffs.append(
                build_editing_source_handoff(
                    artifacts_by_shot[shot_id],
                    storyboard,
                    histories_by_shot[shot_id],
                )
            )
        except VideoShotReviewError as error:
            raise FinalAdReviewAuthorizationError(
                f"source authorization is unusable for shot {shot_id}: {error}"
            ) from error
    return tuple(handoffs)


def create_final_ad_review_context(
    *,
    artifact: EditingArtifact,
    plan: EditingPlan,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    video_artifacts: Sequence[VideoShotArtifact],
    video_review_histories: Sequence[VideoShotReviewHistory],
) -> FinalAdReviewContext:
    """Build exact path-free review context after transitive reauthorization."""

    (
        current_artifact,
        current_plan,
        _,
        _,
        current_storyboard,
    ) = _validated_domain_inputs(artifact, plan, product, campaign, storyboard)
    handoffs = _current_handoffs(
        current_storyboard, video_artifacts, video_review_histories
    )
    handoffs_by_shot = {item.shot_id: item for item in handoffs}
    sources = []
    for segment in current_plan.segments:
        handoff = handoffs_by_shot.get(segment.shot_id)
        if handoff is None:
            raise FinalAdReviewAuthorizationError(
                f"Editing Plan references missing current shot {segment.shot_id}"
            )
        expected_source = segment.source
        if (
            expected_source.asset_plan_id != handoff.asset_plan_id
            or expected_source.video_artifact_id != handoff.video_artifact_id
            or expected_source.video_artifact_sha256 != handoff.video_artifact_sha256
            or expected_source.authorizing_video_review_context_id
            != handoff.authorizing_video_review_context_id
            or expected_source.authorizing_video_review_id
            != handoff.authorizing_video_review_id
            or expected_source.approved_usable_ranges != handoff.approved_usable_ranges
        ):
            raise FinalAdReviewAuthorizationError(
                f"Editing Plan source authorization is stale for shot {segment.shot_id}"
            )
        if not any(
            item.start_ms <= segment.source_start_ms
            and segment.source_end_ms <= item.end_ms
            for item in handoff.approved_usable_ranges
        ):
            raise FinalAdReviewAuthorizationError(
                f"Editing Plan source range is no longer approved for shot {segment.shot_id}"
            )
        sources.append(
            FinalAdSourceSnapshot(
                shot_id=segment.shot_id,
                asset_plan_id=handoff.asset_plan_id,
                video_artifact_id=handoff.video_artifact_id,
                video_artifact_sha256=handoff.video_artifact_sha256,
                video_review_context_id=handoff.authorizing_video_review_context_id,
                authorizing_video_review_id=handoff.authorizing_video_review_id,
                source_start_ms=segment.source_start_ms,
                source_end_ms=segment.source_end_ms,
            )
        )
    if [item.shot_id for item in sources] != [shot.id for shot in current_storyboard.shots]:
        raise FinalAdReviewAuthorizationError(
            "Editing Plan source order does not match Storyboard"
        )

    overlays = tuple(
        FinalAdOverlaySnapshot(
            shot_id=item.shot_id,
            overlay_index=item.overlay_index,
            kind=item.kind,
            resolved_text=item.resolved_text,
            timeline_start_ms=item.timeline_start_ms,
            timeline_end_ms=item.timeline_end_ms,
            placement=item.placement,
            style_preset_id=item.style_preset_id,
        )
        for item in current_plan.overlays
    )
    music = (
        None
        if current_plan.music is None
        else FinalAdMusicSnapshot(
            asset_id=current_plan.music.asset.asset_id,
            asset_sha256=current_plan.music.asset.sha256,
            source_start_ms=current_plan.music.source_start_ms,
            gain_db=current_plan.music.gain_db,
        )
    )
    sfx = tuple(
        FinalAdSfxSnapshot(
            asset_id=item.asset.asset_id,
            asset_sha256=item.asset.sha256,
            timeline_start_ms=item.timeline_start_ms,
            gain_db=item.gain_db,
        )
        for item in current_plan.sfx
    )
    values = {
        "schema_version": 1,
        "editing_artifact_id": current_artifact.id,
        "editing_artifact_sha256": current_artifact.sha256,
        "editing_plan_id": current_plan.id,
        "product_id": current_artifact.product_id,
        "campaign_id": current_artifact.campaign_id,
        "storyboard_id": current_artifact.storyboard_id,
        "duration_ms": current_artifact.duration_ms,
        "width": current_artifact.width,
        "height": current_artifact.height,
        "container": current_artifact.container,
        "video_codec": current_artifact.video_codec,
        "audio_codec": current_artifact.audio_codec,
        "controlled_audio_stream_count": current_artifact.audio_stream_count,
        "sources": tuple(sources),
        "overlays": overlays,
        "music": music,
        "sfx": sfx,
        "source_video_audio": current_plan.source_video_audio,
        "audio_mix": current_plan.audio_mix.model_copy(deep=True),
        "output": current_plan.output.model_copy(deep=True),
    }
    json_values = {
        key: (
            value.model_dump(mode="json")
            if hasattr(value, "model_dump")
            else [item.model_dump(mode="json") for item in value]
            if isinstance(value, tuple)
            else value
        )
        for key, value in values.items()
    }
    return FinalAdReviewContext(id=_context_id(json_values), **values)


def create_final_ad_review_history(
    artifact: EditingArtifact,
    plan: EditingPlan,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    video_artifacts: Sequence[VideoShotArtifact],
    video_review_histories: Sequence[VideoShotReviewHistory],
) -> FinalAdReviewHistory:
    return FinalAdReviewHistory(
        review_context=create_final_ad_review_context(
            artifact=artifact,
            plan=plan,
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            video_artifacts=video_artifacts,
            video_review_histories=video_review_histories,
        )
    )


def append_final_ad_review(
    history: FinalAdReviewHistory,
    review: FinalAdReview,
    current_artifact: EditingArtifact,
    current_plan: EditingPlan,
    current_product: Product,
    current_campaign: AdCampaign,
    current_storyboard: Storyboard,
    current_video_artifacts: Sequence[VideoShotArtifact],
    current_video_review_histories: Sequence[VideoShotReviewHistory],
) -> FinalAdReviewHistory:
    """Append one exact-context human decision without rewriting history."""

    try:
        validated_history = FinalAdReviewHistory.model_validate(history.model_dump())
        validated_review = FinalAdReview.model_validate(review.model_dump())
    except ValidationError as error:
        raise FinalAdReviewError(
            f"Final Ad review history and record are invalid: {error}"
        ) from error
    current_context = create_final_ad_review_context(
        artifact=current_artifact,
        plan=current_plan,
        product=current_product,
        campaign=current_campaign,
        storyboard=current_storyboard,
        video_artifacts=current_video_artifacts,
        video_review_histories=current_video_review_histories,
    )
    if validated_history.review_context != current_context:
        raise FinalAdReviewError("Final Ad review history is stale for current context")
    _validate_review_for_context(validated_review, current_context)
    if any(item.review_id == validated_review.review_id for item in validated_history.reviews):
        raise FinalAdReviewError(f"duplicate Final Ad review ID: {validated_review.review_id}")
    if (
        validated_history.reviews
        and validated_review.reviewed_at < validated_history.reviews[-1].reviewed_at
    ):
        raise FinalAdReviewError("Final Ad review timestamp precedes existing history")
    return FinalAdReviewHistory(
        review_context=current_context.model_copy(deep=True),
        reviews=(
            *(item.model_copy(deep=True) for item in validated_history.reviews),
            validated_review.model_copy(deep=True),
        ),
    )


def resolve_final_ad_review_state(
    history: FinalAdReviewHistory,
    current_artifact: EditingArtifact,
    current_plan: EditingPlan,
    current_product: Product,
    current_campaign: AdCampaign,
    current_storyboard: Storyboard,
    current_video_artifacts: Sequence[VideoShotArtifact],
    current_video_review_histories: Sequence[VideoShotReviewHistory],
) -> FinalAdReviewState:
    """Resolve latest state only while artifact and all sources remain current."""

    try:
        validated_history = FinalAdReviewHistory.model_validate(history.model_dump())
    except ValidationError as error:
        raise FinalAdReviewError(f"Final Ad review history is invalid: {error}") from error
    try:
        current_context = create_final_ad_review_context(
            artifact=current_artifact,
            plan=current_plan,
            product=current_product,
            campaign=current_campaign,
            storyboard=current_storyboard,
            video_artifacts=current_video_artifacts,
            video_review_histories=current_video_review_histories,
        )
    except (FinalAdReviewArtifactError, FinalAdReviewAuthorizationError):
        return "pending_review"
    if validated_history.review_context != current_context or not validated_history.reviews:
        return "pending_review"
    return validated_history.reviews[-1].decision


def _validated_history(history: FinalAdReviewHistory) -> FinalAdReviewHistory:
    try:
        return FinalAdReviewHistory.model_validate(history.model_dump())
    except ValidationError as error:
        raise FinalAdReviewPersistenceError(
            f"Final Ad review history is invalid: {error}"
        ) from error


def final_ad_review_history_path(
    history: FinalAdReviewHistory,
    storage_root: Union[str, Path] = "products",
) -> Path:
    validated = _validated_history(history)
    context_id = validated.review_context.id
    if not context_id.startswith(FINAL_AD_REVIEW_CONTEXT_PREFIX):
        raise FinalAdReviewPersistenceError("invalid Final Ad review context prefix")
    context_hash = context_id[len(FINAL_AD_REVIEW_CONTEXT_PREFIX) :]
    if len(context_hash) != 64 or any(
        character not in "0123456789abcdef" for character in context_hash
    ):
        raise FinalAdReviewPersistenceError("invalid Final Ad review context hash")
    return (
        Path(storage_root)
        / product_asset_directory_name(validated.review_context.product_id)
        / "reviews"
        / "final-ads"
        / f"{context_hash}.json"
    )


def _read_persisted_history(path: Path) -> FinalAdReviewHistory:
    try:
        if path.is_symlink():
            raise FinalAdReviewPersistenceError(
                "Final Ad review history path must not be a symbolic link"
            )
        return FinalAdReviewHistory.model_validate_json(path.read_text(encoding="utf-8"))
    except FinalAdReviewPersistenceError:
        raise
    except (OSError, UnicodeError, ValidationError) as error:
        raise FinalAdReviewPersistenceError(
            f"Final Ad review history could not be loaded: {path}"
        ) from error


def save_final_ad_review_history(
    history: FinalAdReviewHistory,
    storage_root: Union[str, Path] = "products",
) -> Path:
    """Atomically persist a path-free history without rewriting prior records."""

    validated = _validated_history(history)
    root = Path(storage_root)
    target = final_ad_review_history_path(validated, root)
    directory = target.parent
    temporary_path = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        directory.resolve().relative_to(root.resolve())
        if target.is_symlink():
            raise FinalAdReviewPersistenceError(
                "Final Ad review history path must not be a symbolic link"
            )
        if target.exists():
            if not target.is_file():
                raise FinalAdReviewPersistenceError(
                    "Final Ad review history path is not a regular file"
                )
            existing = _read_persisted_history(target)
            existing_count = len(existing.reviews)
            if (
                existing.review_context != validated.review_context
                or existing_count > len(validated.reviews)
                or existing.reviews != validated.reviews[:existing_count]
            ):
                raise FinalAdReviewPersistenceError(
                    "persisted Final Ad review history is append-only"
                )
        json_text = validated.model_dump_json(
            indent=2,
            ensure_ascii=False,
            exclude_none=True,
        ) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".final-ad-review-",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(json_text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(str(temporary_path), str(target))
        temporary_path = None
        return target.resolve()
    except FinalAdReviewPersistenceError:
        raise
    except (OSError, ValueError) as error:
        raise FinalAdReviewPersistenceError(
            f"Final Ad review history could not be saved: {target}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_final_ad_review_history(
    current_artifact: EditingArtifact,
    current_plan: EditingPlan,
    current_product: Product,
    current_campaign: AdCampaign,
    current_storyboard: Storyboard,
    current_video_artifacts: Sequence[VideoShotArtifact],
    current_video_review_histories: Sequence[VideoShotReviewHistory],
    storage_root: Union[str, Path] = "products",
) -> FinalAdReviewHistory:
    empty = create_final_ad_review_history(
        current_artifact,
        current_plan,
        current_product,
        current_campaign,
        current_storyboard,
        current_video_artifacts,
        current_video_review_histories,
    )
    path = final_ad_review_history_path(empty, storage_root)
    if not path.exists():
        return empty
    loaded = _read_persisted_history(path)
    if loaded.review_context != empty.review_context:
        raise FinalAdReviewPersistenceError(
            "persisted Final Ad review does not match current context"
        )
    return loaded
