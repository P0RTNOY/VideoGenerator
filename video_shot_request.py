"""Provider-neutral ephemeral requests for animating an approved Hero Frame."""

import hashlib
import io
import math
from pathlib import Path
from typing import List, Literal, Optional, Sequence

from PIL import Image, UnidentifiedImageError
from pydantic import ConfigDict, Field, ValidationError, model_validator

from campaign import AdCampaign, AspectRatio, CampaignClaimSnapshot
from hero_frame_artifact import HeroFrameArtifact
from hero_frame_review import (
    HeroFrameReviewArtifactError,
    HeroFrameReviewError,
    HeroFrameReviewHistory,
    create_hero_frame_review_context,
    resolve_hero_frame_review_state,
)
from product import NonEmptyStr, Product, ProductModel
from product_image_downloads import Sha256Hex
from storyboard import Storyboard, StoryboardContextError, validate_storyboard
from storyboard_asset_plan import StoryboardAssetPlan


AnimationFidelity = Literal["animate_approved_hero_frame"]
AddedTextPolicy = Literal["no_added_text"]
ProviderAudioDisposition = Literal["discard_before_editing"]


class VideoMotionIntent(ProductModel):
    """Explicit authored motion channels; never a source of Product facts."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    subject_motion: Optional[NonEmptyStr] = None
    environment_motion: Optional[NonEmptyStr] = None
    camera_motion: Optional[NonEmptyStr] = None
    motion_constraints: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def require_at_least_one_motion_channel(self) -> "VideoMotionIntent":
        if all(
            value is None
            for value in (
                self.subject_motion,
                self.environment_motion,
                self.camera_motion,
                self.motion_constraints,
            )
        ):
            raise ValueError("motion intent requires at least one authored channel")
        return self


class VideoStartImageHandle(ProductModel):
    """Current local handle for the sole authoritative provider image input."""

    artifact_id: NonEmptyStr
    local_path: Path
    sha256: Sha256Hex
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    format: Literal["PNG"] = "PNG"

    @model_validator(mode="after")
    def require_content_addressed_artifact_id(self) -> "VideoStartImageHandle":
        if self.artifact_id != f"hero-frame:{self.sha256}":
            raise ValueError("start-image artifact ID must contain its SHA-256")
        return self


class VideoShotRequest(ProductModel):
    """Ephemeral generation intent authorized by one exact manual review."""

    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    hero_frame_artifact_id: NonEmptyStr
    hero_frame_sha256: Sha256Hex
    hero_frame_request_fingerprint: Sha256Hex
    authorizing_review_context_id: NonEmptyStr
    authorizing_review_id: NonEmptyStr
    start_image: VideoStartImageHandle
    intended_edit_duration_seconds: float = Field(gt=0, strict=True)
    requested_generation_duration_seconds: float = Field(gt=0, strict=True)
    motion: VideoMotionIntent
    motion_relevant_claims: List[CampaignClaimSnapshot] = Field(
        default_factory=list
    )
    animation_fidelity: AnimationFidelity = "animate_approved_hero_frame"
    aspect_ratio: AspectRatio
    added_text_policy: AddedTextPolicy = "no_added_text"
    provider_audio_disposition: ProviderAudioDisposition = (
        "discard_before_editing"
    )

    @model_validator(mode="after")
    def require_consistent_start_state(self) -> "VideoShotRequest":
        if self.hero_frame_artifact_id != self.start_image.artifact_id:
            raise ValueError("start image must match the authorized Hero Frame")
        if self.hero_frame_sha256 != self.start_image.sha256:
            raise ValueError("start-image SHA must match the authorized Hero Frame")
        claim_ids = [claim.claim_id for claim in self.motion_relevant_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("motion-relevant claims must have unique IDs")
        for field_name in (
            "intended_edit_duration_seconds",
            "requested_generation_duration_seconds",
        ):
            if not math.isfinite(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be finite")
        return self


class VideoShotRequestError(ValueError):
    """Base error for controlled VideoShotRequest preparation failures."""


class VideoShotRequestContextError(VideoShotRequestError):
    """Raised when durable inputs do not authorize one exact video request."""


class VideoStartImageIntegrityError(VideoShotRequestError):
    """Raised when the admitted Hero Frame changes before provider upload."""


def _validated_inputs(
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    asset_plan: StoryboardAssetPlan,
    artifact: HeroFrameArtifact,
    history: HeroFrameReviewHistory,
    motion: VideoMotionIntent,
):
    try:
        return (
            Product.model_validate(product.model_dump()),
            AdCampaign.model_validate(campaign.model_dump()),
            Storyboard.model_validate(storyboard.model_dump()),
            StoryboardAssetPlan.model_validate(asset_plan.model_dump()),
            HeroFrameArtifact.model_validate(artifact.model_dump()),
            HeroFrameReviewHistory.model_validate(history.model_dump()),
            VideoMotionIntent.model_validate(motion.model_dump()),
        )
    except ValidationError as error:
        raise VideoShotRequestContextError(
            f"Video Shot request inputs must be structurally valid: {error}"
        ) from error


def _validate_generation_duration(value: object) -> float:
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        raise VideoShotRequestContextError(
            "requested generation duration must be a finite positive float"
        )
    return value


def _validate_claim_ids(value: Sequence[str]) -> List[str]:
    if isinstance(value, (str, bytes)):
        raise VideoShotRequestContextError(
            "motion-relevant claim IDs must be an ordered collection"
        )
    claim_ids = list(value)
    if any(type(claim_id) is not str or not claim_id.strip() for claim_id in claim_ids):
        raise VideoShotRequestContextError(
            "motion-relevant claim IDs must be non-empty strings"
        )
    if len(claim_ids) != len(set(claim_ids)):
        raise VideoShotRequestContextError(
            "motion-relevant claim IDs must not contain duplicates"
        )
    return claim_ids


def build_video_shot_request(
    *,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    asset_plan: StoryboardAssetPlan,
    hero_frame_artifact: HeroFrameArtifact,
    review_history: HeroFrameReviewHistory,
    shot_id: str,
    requested_generation_duration_seconds: float,
    motion: VideoMotionIntent,
    motion_relevant_claim_ids: Sequence[str] = (),
) -> VideoShotRequest:
    """Build one request only from an exact approved Hero Frame context."""

    (
        validated_product,
        validated_campaign,
        validated_storyboard,
        validated_plan,
        validated_artifact,
        validated_history,
        validated_motion,
    ) = _validated_inputs(
        product,
        campaign,
        storyboard,
        asset_plan,
        hero_frame_artifact,
        review_history,
        motion,
    )
    generation_duration = _validate_generation_duration(
        requested_generation_duration_seconds
    )
    claim_ids = _validate_claim_ids(motion_relevant_claim_ids)

    try:
        validated_storyboard = validate_storyboard(
            validated_storyboard,
            validated_campaign,
        )
    except StoryboardContextError as error:
        raise VideoShotRequestContextError(str(error)) from error

    if validated_campaign.claim_selection.product_id != validated_product.id:
        raise VideoShotRequestContextError(
            "Campaign does not belong to the supplied Product"
        )
    if validated_plan.product_id != validated_product.id:
        raise VideoShotRequestContextError(
            "asset plan does not belong to the supplied Product"
        )
    if validated_plan.campaign_id != validated_campaign.id:
        raise VideoShotRequestContextError(
            "asset plan does not belong to the supplied Campaign"
        )
    if validated_plan.storyboard_id != validated_storyboard.id:
        raise VideoShotRequestContextError(
            "asset plan does not belong to the supplied Storyboard"
        )

    shots = [item for item in validated_storyboard.shots if item.id == shot_id]
    if len(shots) != 1:
        raise VideoShotRequestContextError(
            f"Storyboard shot must resolve exactly once: {shot_id}"
        )
    shot = shots[0]
    selections = [
        item
        for item in validated_plan.shot_selections
        if item.shot_id == shot_id
    ]
    if len(selections) != 1:
        raise VideoShotRequestContextError(
            f"Storyboard shot must have one prepared asset selection: {shot_id}"
        )
    storyboard_shot_ids = {item.id for item in validated_storyboard.shots}
    if any(
        selection.shot_id not in storyboard_shot_ids
        for selection in validated_plan.shot_selections
    ):
        raise VideoShotRequestContextError(
            "asset plan references an unknown Storyboard shot"
        )

    expected_bindings = {
        "product_id": validated_product.id,
        "campaign_id": validated_campaign.id,
        "storyboard_id": validated_storyboard.id,
        "asset_plan_id": validated_plan.id,
        "shot_id": shot.id,
    }
    for field_name, expected in expected_bindings.items():
        if getattr(validated_artifact, field_name) != expected:
            raise VideoShotRequestContextError(
                f"Hero Frame artifact {field_name} does not match request context"
            )

    try:
        current_context = create_hero_frame_review_context(validated_artifact)
        review_state = resolve_hero_frame_review_state(
            validated_history,
            validated_artifact,
        )
    except (HeroFrameReviewArtifactError, HeroFrameReviewError) as error:
        raise VideoShotRequestContextError(
            f"Hero Frame approval could not be verified: {error}"
        ) from error
    if validated_history.review_context != current_context:
        review_state = "pending_review"
    if review_state != "approved_for_video_start_image":
        raise VideoShotRequestContextError(
            f"Hero Frame review state is {review_state}; exact approval is required"
        )
    authorizing_review = validated_history.reviews[-1]
    if (
        authorizing_review.review_context_id != current_context.id
        or authorizing_review.decision != "approved_for_video_start_image"
    ):
        raise VideoShotRequestContextError(
            "latest review does not authorize the current Hero Frame context"
        )

    shot_claim_ids = set(shot.selected_claim_ids)
    invalid_shot_claims = [
        claim_id for claim_id in claim_ids if claim_id not in shot_claim_ids
    ]
    if invalid_shot_claims:
        raise VideoShotRequestContextError(
            "motion-relevant claims must be selected by the matching shot: "
            + ", ".join(invalid_shot_claims)
        )
    snapshots_by_id = {
        snapshot.claim_id: snapshot
        for snapshot in validated_campaign.claim_snapshots
    }
    try:
        selected_snapshots = [
            snapshots_by_id[claim_id].model_copy(deep=True)
            for claim_id in claim_ids
        ]
    except KeyError as error:
        raise VideoShotRequestContextError(
            f"missing selected Campaign claim snapshot: {error.args[0]}"
        ) from error

    return VideoShotRequest(
        product_id=validated_product.id,
        campaign_id=validated_campaign.id,
        storyboard_id=validated_storyboard.id,
        asset_plan_id=validated_plan.id,
        shot_id=shot.id,
        hero_frame_artifact_id=validated_artifact.id,
        hero_frame_sha256=validated_artifact.sha256,
        hero_frame_request_fingerprint=(
            validated_artifact.source_request_fingerprint
        ),
        authorizing_review_context_id=current_context.id,
        authorizing_review_id=authorizing_review.review_id,
        start_image=VideoStartImageHandle(
            artifact_id=validated_artifact.id,
            local_path=Path(validated_artifact.local_path),
            sha256=validated_artifact.sha256,
            width=validated_artifact.width,
            height=validated_artifact.height,
            format=validated_artifact.format,
        ),
        intended_edit_duration_seconds=shot.duration_seconds,
        requested_generation_duration_seconds=generation_duration,
        motion=validated_motion.model_copy(deep=True),
        motion_relevant_claims=selected_snapshots,
        aspect_ratio=validated_campaign.aspect_ratio,
    )


def read_verified_video_start_image_bytes(
    handle: VideoStartImageHandle,
) -> bytes:
    """Read once and reverify the exact approved image before provider upload."""

    try:
        validated = VideoStartImageHandle.model_validate(handle.model_dump())
    except ValidationError as error:
        raise VideoStartImageIntegrityError(
            f"start-image metadata is invalid: {error}"
        ) from error
    try:
        data = validated.local_path.read_bytes()
    except OSError as error:
        raise VideoStartImageIntegrityError(
            f"start image could not be read: {validated.artifact_id}"
        ) from error
    if hashlib.sha256(data).hexdigest() != validated.sha256:
        raise VideoStartImageIntegrityError(
            f"start-image SHA-256 changed: {validated.artifact_id}"
        )
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            decoded_format = image.format
            decoded_size = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise VideoStartImageIntegrityError(
            f"start image could not be decoded: {validated.artifact_id}"
        ) from error
    if decoded_format != validated.format:
        raise VideoStartImageIntegrityError(
            f"start-image format changed: {validated.artifact_id}"
        )
    if decoded_size != (validated.width, validated.height):
        raise VideoStartImageIntegrityError(
            f"start-image dimensions changed: {validated.artifact_id}"
        )
    return data
