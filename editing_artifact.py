"""Technical admission and metadata for exact final-ad editing output."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from editing_plan import EditingPlan
from product import NonEmptyStr, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name


FINAL_DURATION_TOLERANCE_MS = 100


class FrozenEditingArtifactModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class EditingArtifact(FrozenEditingArtifactModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    editing_plan_id: NonEmptyStr
    sha256: Sha256Hex
    local_path: NonEmptyStr
    width: Literal[720]
    height: Literal[1280]
    duration_ms: int = Field(gt=0, strict=True)
    container: Literal["mp4"]
    video_codec: Literal["h264"]
    audio_codec: Literal["aac"]
    audio_stream_count: Literal[1] = 1
    created_at: datetime
    source_video_audio_discarded: Literal[True] = True
    manual_final_review_required: Literal[True] = True

    @field_validator("created_at")
    @classmethod
    def require_utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Editing artifact created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_content_identity(self) -> "EditingArtifact":
        if self.id != f"edited-ad:{self.sha256}":
            raise ValueError("Editing artifact ID must contain its SHA-256")
        return self


class EditingReceipt(FrozenEditingArtifactModel):
    schema_version: Literal[1] = 1
    attempt_id: NonEmptyStr
    editing_plan_id: NonEmptyStr
    artifact_id: NonEmptyStr
    tool: Literal["ffmpeg"] = "ffmpeg"
    tool_version: Optional[NonEmptyStr] = None
    started_at: datetime
    completed_at: datetime
    source_video_audio_discarded: Literal[True] = True

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Editing receipt timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_chronology(self) -> "EditingReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("Editing receipt completion must not precede start")
        return self


class EditingArtifactError(RuntimeError):
    """Base error for final editing artifact handling."""


class EditingArtifactValidationError(EditingArtifactError):
    """Raised when rendered bytes do not satisfy the fixed output contract."""


class EditingArtifactFilesystemError(EditingArtifactError):
    """Raised when exact rendered bytes cannot be stored safely."""


def _run(command: list[str], message: str) -> bytes:
    try:
        return subprocess.run(command, check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise EditingArtifactValidationError(message) from error


def _duration_ms(raw: object) -> int:
    try:
        value = Decimal(str(raw)) * Decimal("1000")
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise EditingArtifactValidationError("final duration is invalid") from error


def inspect_edited_ad(path: Union[str, Path], target_duration_ms: int) -> dict:
    """Probe and fully decode one output without modifying it."""

    source = Path(path)
    try:
        if not source.is_file() or source.stat().st_size <= 0:
            raise EditingArtifactValidationError("edited output must be a non-empty file")
    except OSError as error:
        raise EditingArtifactValidationError("edited output cannot be inspected") from error
    probe = _run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
        "edited output is not an inspectable MP4",
    )
    try:
        metadata = json.loads(probe.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EditingArtifactValidationError("ffprobe returned malformed output metadata") from error
    streams = metadata.get("streams")
    if not isinstance(streams, list):
        raise EditingArtifactValidationError("edited output has no streams")
    video = [item for item in streams if item.get("codec_type") == "video"]
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise EditingArtifactValidationError("edited output requires exactly one video and one controlled audio stream")
    format_name = str(metadata.get("format", {}).get("format_name", ""))
    if "mp4" not in format_name:
        raise EditingArtifactValidationError("edited output container must be MP4")
    if (video[0].get("width"), video[0].get("height")) != (720, 1280):
        raise EditingArtifactValidationError("edited output must be exactly 720x1280")
    if video[0].get("codec_name") != "h264":
        raise EditingArtifactValidationError("edited output video codec must be H.264")
    if audio[0].get("codec_name") != "aac":
        raise EditingArtifactValidationError("edited output audio codec must be AAC")
    duration_ms = _duration_ms(metadata.get("format", {}).get("duration"))
    if abs(duration_ms - target_duration_ms) > FINAL_DURATION_TOLERANCE_MS:
        raise EditingArtifactValidationError("edited output duration does not match Campaign target")
    _run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
        "edited output is not fully decodable",
    )
    return {
        "width": 720,
        "height": 1280,
        "duration_ms": duration_ms,
        "container": "mp4",
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_stream_count": 1,
    }


def admit_editing_artifact(
    rendered_path: Union[str, Path],
    plan: EditingPlan,
    storage_root: Union[str, Path] = "products",
    *,
    created_at: Optional[datetime] = None,
) -> EditingArtifact:
    """Validate canonical FFmpeg output, then store those exact bytes by SHA."""

    try:
        current_plan = EditingPlan.model_validate(plan.model_dump())
    except ValidationError as error:
        raise EditingArtifactValidationError(f"Editing Plan is invalid: {error}") from error
    source = Path(rendered_path)
    metadata = inspect_edited_ad(source, current_plan.target_duration_ms)
    try:
        data = source.read_bytes()
    except OSError as error:
        raise EditingArtifactFilesystemError("rendered output could not be read") from error
    sha256 = hashlib.sha256(data).hexdigest()
    root = Path(storage_root)
    target = (
        root
        / product_asset_directory_name(current_plan.product_id)
        / "assets"
        / "derived"
        / "edited-ads"
        / f"{sha256}.mp4"
    )
    temporary_path = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.resolve().relative_to(root.resolve())
        if target.is_symlink():
            raise EditingArtifactFilesystemError("edited artifact path must not be a symbolic link")
        if target.exists():
            if target.read_bytes() != data:
                raise EditingArtifactFilesystemError("content-addressed edited artifact conflicts with existing bytes")
        else:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".edited-ad-", suffix=".tmp", dir=target.parent, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(str(temporary_path), str(target))
            temporary_path = None
    except EditingArtifactFilesystemError:
        raise
    except (OSError, ValueError) as error:
        raise EditingArtifactFilesystemError("edited artifact could not be stored") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return EditingArtifact(
        id=f"edited-ad:{sha256}",
        product_id=current_plan.product_id,
        campaign_id=current_plan.campaign_id,
        storyboard_id=current_plan.storyboard_id,
        editing_plan_id=current_plan.id,
        sha256=sha256,
        local_path=str(target.resolve()),
        created_at=created_at or datetime.now(timezone.utc),
        **metadata,
    )


def read_verified_editing_artifact_bytes(artifact: EditingArtifact) -> bytes:
    """Re-read and technically revalidate one exact final Editing artifact."""

    try:
        validated = EditingArtifact.model_validate(artifact.model_dump())
    except ValidationError as error:
        raise EditingArtifactValidationError(
            f"Editing artifact must be structurally valid: {error}"
        ) from error
    source = Path(validated.local_path)
    try:
        data = source.read_bytes()
    except OSError as error:
        raise EditingArtifactValidationError(
            f"Editing artifact could not be read: {validated.id}"
        ) from error
    if hashlib.sha256(data).hexdigest() != validated.sha256:
        raise EditingArtifactValidationError(
            f"Editing artifact SHA-256 changed: {validated.id}"
        )
    metadata = inspect_edited_ad(source, validated.duration_ms)
    expected = {
        "width": validated.width,
        "height": validated.height,
        "duration_ms": validated.duration_ms,
        "container": validated.container,
        "video_codec": validated.video_codec,
        "audio_codec": validated.audio_codec,
        "audio_stream_count": validated.audio_stream_count,
    }
    if metadata != expected:
        raise EditingArtifactValidationError(
            f"Editing artifact technical metadata changed: {validated.id}"
        )
    try:
        if source.read_bytes() != data:
            raise EditingArtifactValidationError(
                f"Editing artifact changed during verification: {validated.id}"
            )
    except OSError as error:
        raise EditingArtifactValidationError(
            f"Editing artifact could not be reread: {validated.id}"
        ) from error
    return data


def save_editing_artifact(artifact: EditingArtifact, path: Union[str, Path]) -> Path:
    try:
        validated = EditingArtifact.model_validate(artifact.model_dump())
    except ValidationError as error:
        raise EditingArtifactValidationError(f"Editing artifact is invalid: {error}") from error
    output = Path(path)
    try:
        output.write_text(
            validated.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise EditingArtifactFilesystemError(f"Editing artifact JSON could not be saved: {output}") from error
    return output


def load_editing_artifact(path: Union[str, Path]) -> EditingArtifact:
    source = Path(path)
    try:
        return EditingArtifact.model_validate_json(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        raise EditingArtifactValidationError(f"Editing artifact JSON could not be loaded: {source}") from error
