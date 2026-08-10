"""Ephemeral runtime authorization for one exact approved Campaign deliverable."""

import hashlib
from pathlib import Path
from typing import Literal, Sequence

from pydantic import ConfigDict, Field, ValidationError, model_validator

from campaign import AdCampaign
from editing_artifact import (
    EditingArtifact,
    EditingArtifactValidationError,
    inspect_edited_ad,
    read_verified_editing_artifact_bytes,
)
from editing_plan import EditingPlan
from final_ad_review import (
    FINAL_AD_REVIEW_CONTEXT_PREFIX,
    FinalAdReviewHistory,
    FinalAdReviewError,
    create_final_ad_review_context,
)
from product import NonEmptyStr, Product, ProductModel
from product_image_downloads import Sha256Hex
from storyboard import Storyboard
from video_shot_artifact import VideoShotArtifact
from video_shot_review import VideoShotReviewHistory


class FrozenCampaignDeliverableModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class CampaignDeliverableRuntimeArtifact(FrozenCampaignDeliverableModel):
    artifact_id: NonEmptyStr
    sha256: Sha256Hex
    local_path: Path
    width: Literal[720]
    height: Literal[1280]
    duration_ms: int = Field(gt=0, strict=True)
    container: Literal["mp4"]
    video_codec: Literal["h264"]
    audio_codec: Literal["aac"]
    controlled_audio_stream_count: Literal[1] = 1

    @model_validator(mode="after")
    def require_content_identity(self) -> "CampaignDeliverableRuntimeArtifact":
        if self.artifact_id != f"edited-ad:{self.sha256}":
            raise ValueError("deliverable runtime artifact ID must contain its SHA-256")
        return self


class CampaignDeliverableHandoff(FrozenCampaignDeliverableModel):
    """Current runtime authority; durable truth remains artifact, Plan, and review."""

    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    editing_plan_id: NonEmptyStr
    editing_artifact_id: NonEmptyStr
    editing_artifact_sha256: Sha256Hex
    final_review_context_id: NonEmptyStr
    authorizing_final_review_id: NonEmptyStr
    runtime_artifact: CampaignDeliverableRuntimeArtifact
    approval_scope: Literal["exact_campaign_deliverable"] = (
        "exact_campaign_deliverable"
    )

    @model_validator(mode="after")
    def require_exact_runtime_authority(self) -> "CampaignDeliverableHandoff":
        if self.editing_artifact_id != self.runtime_artifact.artifact_id:
            raise ValueError("deliverable runtime artifact ID must match authorization")
        if self.editing_artifact_sha256 != self.runtime_artifact.sha256:
            raise ValueError("deliverable runtime SHA must match authorization")
        if not self.final_review_context_id.startswith(
            FINAL_AD_REVIEW_CONTEXT_PREFIX
        ):
            raise ValueError("deliverable requires a Final Ad Review context")
        return self


class CampaignDeliverableError(ValueError):
    """Raised when exact current approval cannot authorize deliverable use."""


def build_campaign_deliverable_handoff(
    *,
    history: FinalAdReviewHistory,
    artifact: EditingArtifact,
    plan: EditingPlan,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    video_artifacts: Sequence[VideoShotArtifact],
    video_review_histories: Sequence[VideoShotReviewHistory],
) -> CampaignDeliverableHandoff:
    """Expose exact final bytes only while all review authority remains current."""

    try:
        current_history = FinalAdReviewHistory.model_validate(history.model_dump())
        current_artifact = EditingArtifact.model_validate(artifact.model_dump())
    except ValidationError as error:
        raise CampaignDeliverableError(
            f"deliverable authorization inputs are invalid: {error}"
        ) from error
    try:
        current_context = create_final_ad_review_context(
            artifact=current_artifact,
            plan=plan,
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            video_artifacts=video_artifacts,
            video_review_histories=video_review_histories,
        )
    except FinalAdReviewError as error:
        raise CampaignDeliverableError(
            f"current Final Ad context is not deliverable-authorized: {error}"
        ) from error
    if current_history.review_context != current_context:
        raise CampaignDeliverableError(
            "Final Ad review history does not match the exact current context"
        )
    if not current_history.reviews:
        raise CampaignDeliverableError("Final Ad review is pending")
    authorizing_review = current_history.reviews[-1]
    if authorizing_review.decision != "approved_final_campaign_deliverable":
        raise CampaignDeliverableError(
            "latest exact Final Ad review does not approve deliverable use"
        )

    # Recheck the exact final bytes at the last boundary before returning a path.
    try:
        read_verified_editing_artifact_bytes(current_artifact)
    except EditingArtifactValidationError as error:
        raise CampaignDeliverableError(
            "final Editing artifact changed before deliverable handoff"
        ) from error
    return CampaignDeliverableHandoff(
        product_id=current_context.product_id,
        campaign_id=current_context.campaign_id,
        storyboard_id=current_context.storyboard_id,
        editing_plan_id=current_context.editing_plan_id,
        editing_artifact_id=current_context.editing_artifact_id,
        editing_artifact_sha256=current_context.editing_artifact_sha256,
        final_review_context_id=current_context.id,
        authorizing_final_review_id=authorizing_review.review_id,
        runtime_artifact=CampaignDeliverableRuntimeArtifact(
            artifact_id=current_artifact.id,
            sha256=current_artifact.sha256,
            local_path=Path(current_artifact.local_path).resolve(),
            width=current_artifact.width,
            height=current_artifact.height,
            duration_ms=current_artifact.duration_ms,
            container=current_artifact.container,
            video_codec=current_artifact.video_codec,
            audio_codec=current_artifact.audio_codec,
            controlled_audio_stream_count=current_artifact.audio_stream_count,
        ),
    )


def read_verified_campaign_deliverable_bytes(
    handoff: CampaignDeliverableHandoff,
) -> bytes:
    """Consumer-time recheck of the handoff's exact final artifact bytes."""

    try:
        current = CampaignDeliverableHandoff.model_validate(handoff.model_dump())
    except ValidationError as error:
        raise CampaignDeliverableError(
            f"Campaign deliverable handoff is invalid: {error}"
        ) from error
    runtime = current.runtime_artifact
    try:
        data = runtime.local_path.read_bytes()
    except OSError as error:
        raise CampaignDeliverableError(
            f"Campaign deliverable bytes could not be read: {runtime.artifact_id}"
        ) from error
    if hashlib.sha256(data).hexdigest() != runtime.sha256:
        raise CampaignDeliverableError(
            f"Campaign deliverable SHA-256 changed: {runtime.artifact_id}"
        )
    try:
        metadata = inspect_edited_ad(runtime.local_path, runtime.duration_ms)
    except EditingArtifactValidationError as error:
        raise CampaignDeliverableError(
            f"Campaign deliverable is no longer technically valid: {runtime.artifact_id}"
        ) from error
    expected = {
        "width": runtime.width,
        "height": runtime.height,
        "duration_ms": runtime.duration_ms,
        "container": runtime.container,
        "video_codec": runtime.video_codec,
        "audio_codec": runtime.audio_codec,
        "audio_stream_count": runtime.controlled_audio_stream_count,
    }
    if metadata != expected:
        raise CampaignDeliverableError(
            f"Campaign deliverable metadata changed: {runtime.artifact_id}"
        )
    try:
        if runtime.local_path.read_bytes() != data:
            raise CampaignDeliverableError(
                f"Campaign deliverable changed during verification: {runtime.artifact_id}"
            )
    except OSError as error:
        raise CampaignDeliverableError(
            f"Campaign deliverable could not be reread: {runtime.artifact_id}"
        ) from error
    return data
