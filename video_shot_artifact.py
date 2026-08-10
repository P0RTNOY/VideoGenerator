"""Exact raw video artifacts and sanitized one-attempt generation receipts."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import Field, ValidationError, field_validator, model_validator

from product import NonEmptyStr, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name
from video_shot_request import VideoShotRequest


VIDEO_ASPECT_RATIO = 9 / 16
VIDEO_ASPECT_RATIO_TOLERANCE = 0.02
VIDEO_DURATION_TOLERANCE_SECONDS = 0.5


class VideoShotArtifact(ProductModel):
    """One technically admitted raw provider MP4 awaiting manual review."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    hero_frame_artifact_id: NonEmptyStr
    hero_frame_sha256: Sha256Hex
    authorizing_review_context_id: NonEmptyStr
    authorizing_review_id: NonEmptyStr
    source_request_fingerprint: Sha256Hex
    sha256: Sha256Hex
    local_path: NonEmptyStr
    duration_seconds: float = Field(gt=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    container: NonEmptyStr
    video_codec: NonEmptyStr
    has_audio: bool
    created_at: datetime
    provider_audio_disposition: Literal["discard_before_editing"] = (
        "discard_before_editing"
    )
    manual_fidelity_review_required: Literal[True] = True

    @field_validator("created_at")
    @classmethod
    def require_utc_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_content_addressed_identity(self) -> "VideoShotArtifact":
        if self.id != f"video-shot:{self.sha256}":
            raise ValueError("Video Shot artifact ID must contain its SHA-256")
        return self


class VideoGenerationReceipt(ProductModel):
    """Sanitized metadata for exactly one actual provider attempt."""

    schema_version: Literal[1] = 1
    attempt_id: NonEmptyStr
    outcome: Literal["succeeded", "failed"]
    artifact_id: Optional[NonEmptyStr] = None
    provider: NonEmptyStr
    model: NonEmptyStr
    started_at: datetime
    completed_at: datetime
    provider_operation_id: Optional[NonEmptyStr] = None
    attempt_count: Literal[1] = 1

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_utc_aware_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generation receipt timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_outcome_consistency(self) -> "VideoGenerationReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.outcome == "succeeded" and self.artifact_id is None:
            raise ValueError("successful receipt requires an artifact ID")
        if self.outcome == "failed" and self.artifact_id is not None:
            raise ValueError("failed receipt cannot reference an artifact")
        return self


VideoGenerationFailureCode = Literal[
    "provider_error",
    "provider_rejected",
    "provider_audio_filter_error",
    "polling_exhausted",
    "malformed_output",
    "invalid_output",
]


class VideoGenerationFailure(ProductModel):
    code: VideoGenerationFailureCode
    message: NonEmptyStr


class VideoGenerationResult(ProductModel):
    """Terminal raw-generation result that never grants editing approval."""

    status: Literal["succeeded", "failed"]
    artifact: Optional[VideoShotArtifact] = None
    receipt: VideoGenerationReceipt
    failure: Optional[VideoGenerationFailure] = None

    @model_validator(mode="after")
    def require_terminal_consistency(self) -> "VideoGenerationResult":
        if self.status != self.receipt.outcome:
            raise ValueError("result status must match receipt outcome")
        if self.status == "succeeded":
            if self.artifact is None or self.failure is not None:
                raise ValueError("success requires an artifact and no failure")
            if self.receipt.artifact_id != self.artifact.id:
                raise ValueError("receipt artifact ID must match result artifact")
        elif self.artifact is not None or self.failure is None:
            raise ValueError("failure requires details and no artifact")
        return self


class VideoShotArtifactError(RuntimeError):
    """Base error for controlled raw-video artifact failures."""


class VideoShotArtifactValidationError(VideoShotArtifactError):
    """Raised when exact provider bytes cannot be technically admitted."""


class VideoShotArtifactFilesystemError(VideoShotArtifactError):
    """Raised when canonical content-addressed storage is unsafe or unavailable."""


def video_shot_request_fingerprint(request: VideoShotRequest) -> str:
    """Hash meaningful generation intent without paths or execution metadata."""

    try:
        validated = VideoShotRequest.model_validate(request.model_dump())
    except ValidationError as error:
        raise VideoShotArtifactValidationError(
            f"Video Shot request must be structurally valid: {error}"
        ) from error
    payload = {
        "product_id": validated.product_id,
        "campaign_id": validated.campaign_id,
        "storyboard_id": validated.storyboard_id,
        "asset_plan_id": validated.asset_plan_id,
        "shot_id": validated.shot_id,
        "hero_frame_artifact_id": validated.hero_frame_artifact_id,
        "hero_frame_sha256": validated.hero_frame_sha256,
        "hero_frame_request_fingerprint": (
            validated.hero_frame_request_fingerprint
        ),
        "authorizing_review_context_id": (
            validated.authorizing_review_context_id
        ),
        "authorizing_review_id": validated.authorizing_review_id,
        "intended_edit_duration_seconds": (
            validated.intended_edit_duration_seconds
        ),
        "requested_generation_duration_seconds": (
            validated.requested_generation_duration_seconds
        ),
        "motion": validated.motion.model_dump(mode="json", exclude_none=True),
        "motion_relevant_claims": [
            item.model_dump(mode="json", exclude_none=True)
            for item in validated.motion_relevant_claims
        ],
        "animation_fidelity": validated.animation_fidelity,
        "aspect_ratio": validated.aspect_ratio,
        "added_text_policy": validated.added_text_policy,
        "provider_audio_disposition": validated.provider_audio_disposition,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _run_media_command(command: list[str], failure_message: str) -> bytes:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise VideoShotArtifactValidationError(failure_message) from error
    return completed.stdout


def _inspect_mp4(data: bytes, requested_duration: float) -> dict:
    if not isinstance(data, bytes) or not data:
        raise VideoShotArtifactValidationError(
            "provider output must contain non-empty bytes"
        )
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".video-probe-",
            suffix=".mp4",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
        probe_output = _run_media_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(temporary_path),
            ],
            "provider output is not an inspectable MP4",
        )
        _run_media_command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(temporary_path),
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ],
            "provider video stream is not decodable",
        )
        try:
            metadata = json.loads(probe_output.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise VideoShotArtifactValidationError(
                "ffprobe returned malformed video metadata"
            ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    format_data = metadata.get("format") or {}
    container = format_data.get("format_name")
    if not isinstance(container, str) or "mp4" not in container.split(","):
        raise VideoShotArtifactValidationError(
            "provider output container must include MP4"
        )
    streams = metadata.get("streams")
    if not isinstance(streams, list):
        raise VideoShotArtifactValidationError("provider output has no stream list")
    video_streams = [
        stream for stream in streams if stream.get("codec_type") == "video"
    ]
    if len(video_streams) != 1:
        raise VideoShotArtifactValidationError(
            "provider output must contain exactly one video stream"
        )
    video = video_streams[0]
    width = video.get("width")
    height = video.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise VideoShotArtifactValidationError(
            "provider output dimensions must be positive integers"
        )
    if abs((width / height) - VIDEO_ASPECT_RATIO) > VIDEO_ASPECT_RATIO_TOLERANCE:
        raise VideoShotArtifactValidationError(
            f"provider output dimensions {width}x{height} are not plausible 9:16"
        )
    codec = video.get("codec_name")
    if not isinstance(codec, str) or not codec.strip():
        raise VideoShotArtifactValidationError(
            "provider output video codec could not be identified"
        )
    try:
        duration = float(format_data["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise VideoShotArtifactValidationError(
            "provider output duration could not be identified"
        ) from error
    if duration <= 0 or abs(duration - requested_duration) > (
        VIDEO_DURATION_TOLERANCE_SECONDS
    ):
        raise VideoShotArtifactValidationError(
            f"provider output duration {duration:g}s does not match requested "
            f"{requested_duration:g}s"
        )
    return {
        "container": container,
        "video_codec": codec,
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "has_audio": any(
            stream.get("codec_type") == "audio" for stream in streams
        ),
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as existing:
        for chunk in iter(lambda: existing.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_exact_mp4(
    data: bytes,
    product_id: str,
    sha256: str,
    storage_root: Union[str, Path],
) -> Path:
    root = Path(storage_root)
    directory = (
        root
        / product_asset_directory_name(product_id)
        / "assets"
        / "derived"
        / "video-shots"
    )
    target = directory / f"{sha256}.mp4"
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve()
        try:
            directory.resolve().relative_to(resolved_root)
        except ValueError as error:
            raise VideoShotArtifactFilesystemError(
                "Video Shot directory resolves outside its storage root"
            ) from error
        if target.is_symlink():
            raise VideoShotArtifactFilesystemError(
                "canonical Video Shot path must not be a symbolic link"
            )
        if target.exists():
            if not target.is_file() or _hash_file(target) != sha256:
                raise VideoShotArtifactFilesystemError(
                    "existing canonical Video Shot failed its SHA-256 check"
                )
            return target.resolve()
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".video-shot-",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(str(temporary_path), str(target))
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or _hash_file(target) != sha256:
                raise VideoShotArtifactFilesystemError(
                    "concurrent canonical Video Shot failed its SHA-256 check"
                )
        return target.resolve()
    except VideoShotArtifactError:
        raise
    except OSError as error:
        raise VideoShotArtifactFilesystemError(
            f"Video Shot could not be stored: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def store_validated_video_shot_artifact(
    output_bytes: bytes,
    request: VideoShotRequest,
    storage_root: Union[str, Path] = "products",
    *,
    created_at: Optional[datetime] = None,
) -> VideoShotArtifact:
    """Inspect and store exact raw provider bytes without transformation."""

    try:
        validated = VideoShotRequest.model_validate(request.model_dump())
    except ValidationError as error:
        raise VideoShotArtifactValidationError(
            f"Video Shot request must be structurally valid: {error}"
        ) from error
    metadata = _inspect_mp4(
        output_bytes,
        validated.requested_generation_duration_seconds,
    )
    sha256 = hashlib.sha256(output_bytes).hexdigest()
    stored_path = _store_exact_mp4(
        output_bytes,
        validated.product_id,
        sha256,
        storage_root,
    )
    return VideoShotArtifact(
        id=f"video-shot:{sha256}",
        product_id=validated.product_id,
        campaign_id=validated.campaign_id,
        storyboard_id=validated.storyboard_id,
        asset_plan_id=validated.asset_plan_id,
        shot_id=validated.shot_id,
        hero_frame_artifact_id=validated.hero_frame_artifact_id,
        hero_frame_sha256=validated.hero_frame_sha256,
        authorizing_review_context_id=validated.authorizing_review_context_id,
        authorizing_review_id=validated.authorizing_review_id,
        source_request_fingerprint=video_shot_request_fingerprint(validated),
        sha256=sha256,
        local_path=str(stored_path),
        created_at=created_at or datetime.now(timezone.utc),
        **metadata,
    )
