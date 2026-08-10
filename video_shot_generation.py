"""Provider-neutral video instruction compilation and capability preflight."""

from typing import Literal, Optional, Protocol, Tuple

from pydantic import ConfigDict, ValidationError

from campaign import CampaignClaimSnapshot
from product import NonEmptyStr, ProductModel
from video_shot_request import (
    VideoShotRequest,
    VideoStartImageIntegrityError,
    read_verified_video_start_image_bytes,
)


MAX_CREATIVE_MOTION_LENGTH = 4000


class VideoProviderCapabilities(ProductModel):
    """Small immutable declaration used to reject incompatible providers."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )

    provider: NonEmptyStr
    model: NonEmptyStr
    supports_start_image: bool
    supported_aspect_ratios: Tuple[Literal["9:16"], ...]
    supported_resolutions: Tuple[Literal["720p"], ...]
    supported_durations_seconds: Tuple[int, ...]
    videos_per_request: int
    audio_generation: Literal["always_on"]


class VideoGenerationOptions(ProductModel):
    """One-attempt v1 options with no retry, fallback, or prompt rewriting."""

    resolution: Literal["720p"] = "720p"
    number_of_videos: Literal[1] = 1


class VideoShotGenerationInstruction(ProductModel):
    """Structured, provider-neutral visual instruction channels."""

    authoritative_start_image_policy: Literal[
        "approved_hero_frame_is_sole_authoritative_visual_start"
    ] = "approved_hero_frame_is_sole_authoritative_visual_start"
    motion_relevant_facts: list[CampaignClaimSnapshot]
    subject_motion: Optional[NonEmptyStr] = None
    environment_motion: Optional[NonEmptyStr] = None
    camera_motion: Optional[NonEmptyStr] = None
    motion_constraints: Optional[NonEmptyStr] = None
    animation_fidelity: Literal["animate_approved_hero_frame"] = (
        "animate_approved_hero_frame"
    )
    added_text_policy: Literal["no_added_text"] = "no_added_text"
    provider_audio_disposition: Literal["discard_before_editing"] = (
        "discard_before_editing"
    )


class VideoProvider(Protocol):
    """Minimal provider boundary; concrete adapters own their SDK contracts."""

    def capabilities(self) -> VideoProviderCapabilities:
        ...

    def generate(self, request: VideoShotRequest):
        ...


class VideoGenerationError(ValueError):
    """Base error for video instruction and provider preflight failures."""


class VideoProviderCompatibilityError(VideoGenerationError):
    """Raised before a provider call when the exact request is unsupported."""


class CreativeMotionTooLongError(VideoGenerationError):
    """Raised rather than truncating authored motion at the trust boundary."""


def _revalidated_request(request: VideoShotRequest) -> VideoShotRequest:
    try:
        return VideoShotRequest.model_validate(request.model_dump())
    except ValidationError as error:
        raise VideoProviderCompatibilityError(
            f"Video Shot request must be structurally valid: {error}"
        ) from error


def compile_video_shot_instruction(
    request: VideoShotRequest,
) -> VideoShotGenerationInstruction:
    """Compile separated factual, creative, and fixed-policy channels."""

    validated = _revalidated_request(request)
    for field_name in (
        "subject_motion",
        "environment_motion",
        "camera_motion",
        "motion_constraints",
    ):
        value = getattr(validated.motion, field_name)
        if value is not None and len(value) > MAX_CREATIVE_MOTION_LENGTH:
            raise CreativeMotionTooLongError(
                f"motion field exceeds {MAX_CREATIVE_MOTION_LENGTH} characters: "
                f"{field_name}"
            )
    return VideoShotGenerationInstruction(
        motion_relevant_facts=[
            item.model_copy(deep=True)
            for item in validated.motion_relevant_claims
        ],
        subject_motion=validated.motion.subject_motion,
        environment_motion=validated.motion.environment_motion,
        camera_motion=validated.motion.camera_motion,
        motion_constraints=validated.motion.motion_constraints,
    )


def preflight_video_generation(
    request: VideoShotRequest,
    options: VideoGenerationOptions,
    capabilities: VideoProviderCapabilities,
) -> bytes:
    """Reject incompatible intent and return immediately reverified image bytes."""

    validated_request = _revalidated_request(request)
    try:
        validated_options = VideoGenerationOptions.model_validate(
            options.model_dump()
        )
        validated_capabilities = VideoProviderCapabilities.model_validate(
            capabilities.model_dump()
        )
    except ValidationError as error:
        raise VideoProviderCompatibilityError(
            f"video provider options and capabilities must be valid: {error}"
        ) from error
    if not validated_capabilities.supports_start_image:
        raise VideoProviderCompatibilityError(
            "provider does not support the required start image"
        )
    if validated_request.aspect_ratio not in (
        validated_capabilities.supported_aspect_ratios
    ):
        raise VideoProviderCompatibilityError(
            f"provider does not support aspect ratio {validated_request.aspect_ratio}"
        )
    if validated_options.resolution not in (
        validated_capabilities.supported_resolutions
    ):
        raise VideoProviderCompatibilityError(
            f"provider does not support resolution {validated_options.resolution}"
        )
    duration = validated_request.requested_generation_duration_seconds
    if duration not in validated_capabilities.supported_durations_seconds:
        raise VideoProviderCompatibilityError(
            f"provider does not support requested duration {duration:g}s"
        )
    if (
        validated_options.number_of_videos != 1
        or validated_capabilities.videos_per_request != 1
    ):
        raise VideoProviderCompatibilityError(
            "v1 requires exactly one generated video"
        )
    try:
        return read_verified_video_start_image_bytes(validated_request.start_image)
    except VideoStartImageIntegrityError as error:
        raise VideoProviderCompatibilityError(
            f"approved start-image integrity failed: {error}"
        ) from error
