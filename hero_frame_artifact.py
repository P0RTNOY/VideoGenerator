"""Validated local Hero Frame artifacts and provider execution receipts."""

import hashlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional, Union

from PIL import Image, UnidentifiedImageError
from pydantic import Field, ValidationError, field_validator, model_validator

from hero_frame_request import HeroFrameRequest
from product import NonEmptyStr, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name


HERO_FRAME_ASPECT_RATIO = 9 / 16
HERO_FRAME_ASPECT_RATIO_TOLERANCE = 0.01


class HeroFrameArtifact(ProductModel):
    """One technically validated generated PNG linked to its domain request."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    source_request_fingerprint: Sha256Hex
    local_path: NonEmptyStr
    sha256: Sha256Hex
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    format: Literal["PNG"] = "PNG"
    created_at: datetime
    manual_fidelity_review_required: Literal[True] = True

    @field_validator("created_at")
    @classmethod
    def require_utc_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_content_addressed_identity(self) -> "HeroFrameArtifact":
        if self.id != f"hero-frame:{self.sha256}":
            raise ValueError("Hero Frame artifact ID must contain its SHA-256")
        return self


class HeroFrameGenerationUsage(ProductModel):
    input_tokens: Optional[int] = Field(default=None, ge=0, strict=True)
    output_tokens: Optional[int] = Field(default=None, ge=0, strict=True)
    total_tokens: Optional[int] = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def require_at_least_one_returned_value(self) -> "HeroFrameGenerationUsage":
        if all(
            value is None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
            )
        ):
            raise ValueError("usage requires at least one provider-returned value")
        return self


class HeroFrameGenerationCost(ProductModel):
    amount: float = Field(ge=0, strict=True)
    currency: NonEmptyStr


class HeroFrameGenerationReceipt(ProductModel):
    """Sanitized metadata for exactly one actual provider attempt."""

    schema_version: Literal[1] = 1
    attempt_id: NonEmptyStr
    outcome: Literal["succeeded", "failed"]
    artifact_id: Optional[NonEmptyStr] = None
    provider: NonEmptyStr
    model: NonEmptyStr
    started_at: datetime
    completed_at: datetime
    usage: Optional[HeroFrameGenerationUsage] = None
    cost: Optional[HeroFrameGenerationCost] = None
    provider_request_id: Optional[NonEmptyStr] = None
    attempt_count: Literal[1] = 1

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_utc_aware_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generation receipt timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_outcome_artifact_consistency(self) -> "HeroFrameGenerationReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.outcome == "succeeded" and self.artifact_id is None:
            raise ValueError("successful receipt requires an artifact ID")
        if self.outcome == "failed" and self.artifact_id is not None:
            raise ValueError("failed receipt cannot reference an artifact")
        return self


HeroFrameFailureCode = Literal[
    "provider_error",
    "provider_rejected",
    "missing_output",
    "malformed_output",
    "invalid_output",
]


class HeroFrameGenerationFailure(ProductModel):
    code: HeroFrameFailureCode
    message: NonEmptyStr


class HeroFrameGenerationResult(ProductModel):
    """Terminal result for one provider attempt, never a fidelity approval."""

    status: Literal["succeeded", "failed"]
    artifact: Optional[HeroFrameArtifact] = None
    receipt: HeroFrameGenerationReceipt
    failure: Optional[HeroFrameGenerationFailure] = None

    @model_validator(mode="after")
    def require_terminal_result_consistency(self) -> "HeroFrameGenerationResult":
        if self.status != self.receipt.outcome:
            raise ValueError("result status must match receipt outcome")
        if self.status == "succeeded":
            if self.artifact is None or self.failure is not None:
                raise ValueError(
                    "successful result requires an artifact and no failure"
                )
            if self.receipt.artifact_id != self.artifact.id:
                raise ValueError("receipt artifact ID must match result artifact")
        elif self.artifact is not None or self.failure is None:
            raise ValueError("failed result requires failure details and no artifact")
        return self


class HeroFrameArtifactError(RuntimeError):
    """Base error for controlled Hero Frame artifact failures."""


class HeroFrameArtifactValidationError(HeroFrameArtifactError):
    """Raised when provider bytes cannot be admitted as a Hero Frame."""


class HeroFrameArtifactFilesystemError(HeroFrameArtifactError):
    """Raised when canonical local storage cannot be used safely."""


def _request_reference_payload(asset) -> dict:
    return {
        "asset_id": asset.asset_id,
        "asset_kind": asset.asset_kind,
        "sha256": asset.sha256,
        "width": asset.width,
        "height": asset.height,
        "format": asset.format,
    }


def hero_frame_request_fingerprint(request: HeroFrameRequest) -> str:
    """Hash meaningful intent while excluding runtime paths and object identity."""

    try:
        validated = HeroFrameRequest.model_validate(request.model_dump())
    except ValidationError as error:
        raise HeroFrameArtifactValidationError(
            f"Hero Frame request must be structurally valid: {error}"
        ) from error
    payload = {
        "product": validated.product.model_dump(mode="json", exclude_none=True),
        "campaign_id": validated.campaign_id,
        "storyboard_id": validated.storyboard_id,
        "asset_plan_id": validated.asset_plan_id,
        "shot_id": validated.shot_id,
        "factual_claims": [
            claim.model_dump(mode="json", exclude_none=True)
            for claim in validated.factual_claims
        ],
        "creative_direction": validated.creative_direction.model_dump(
            mode="json",
            exclude_none=True,
        ),
        "aspect_ratio": validated.aspect_ratio,
        "product_fidelity": validated.product_fidelity,
        "added_text_policy": validated.added_text_policy,
        "factual_grounding": validated.factual_grounding,
        "primary_reference": _request_reference_payload(
            validated.primary_reference
        ),
        "supporting_references": [
            _request_reference_payload(asset)
            for asset in validated.supporting_references
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _decode_png(data: bytes) -> tuple:
    if not isinstance(data, bytes) or not data:
        raise HeroFrameArtifactValidationError(
            "provider output must contain non-empty bytes"
        )
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image_format = image.format
            width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise HeroFrameArtifactValidationError(
            "provider output is not a fully decodable image"
        ) from error
    if image_format != "PNG":
        raise HeroFrameArtifactValidationError(
            f"provider output format must be PNG, not {image_format or 'unknown'}"
        )
    if width <= 0 or height <= 0:
        raise HeroFrameArtifactValidationError(
            "provider output dimensions must be positive"
        )
    if abs((width / height) - HERO_FRAME_ASPECT_RATIO) > (
        HERO_FRAME_ASPECT_RATIO_TOLERANCE
    ):
        raise HeroFrameArtifactValidationError(
            f"provider output dimensions {width}x{height} are not consistent "
            "with requested 9:16"
        )
    return width, height


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as existing:
        for chunk in iter(lambda: existing.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_exact_png(
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
        / "hero-frames"
    )
    target = directory / f"{sha256}.png"
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve()
        try:
            directory.resolve().relative_to(resolved_root)
        except ValueError as error:
            raise HeroFrameArtifactFilesystemError(
                "Hero Frame directory resolves outside its storage root"
            ) from error
        if target.is_symlink():
            raise HeroFrameArtifactFilesystemError(
                "canonical Hero Frame path must not be a symbolic link"
            )
        if target.exists():
            if not target.is_file() or _hash_file(target) != sha256:
                raise HeroFrameArtifactFilesystemError(
                    "existing canonical Hero Frame failed its SHA-256 check"
                )
            return target.resolve()

        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".hero-frame-",
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
                raise HeroFrameArtifactFilesystemError(
                    "concurrent canonical Hero Frame failed its SHA-256 check"
                )
        return target.resolve()
    except HeroFrameArtifactError:
        raise
    except OSError as error:
        raise HeroFrameArtifactFilesystemError(
            f"Hero Frame could not be stored: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def store_validated_hero_frame_artifact(
    output_bytes: bytes,
    request: HeroFrameRequest,
    storage_root: Union[str, Path] = "products",
    *,
    created_at: Optional[datetime] = None,
) -> HeroFrameArtifact:
    """Validate and store exact provider bytes without media transformation."""

    width, height = _decode_png(output_bytes)
    sha256 = hashlib.sha256(output_bytes).hexdigest()
    try:
        validated_request = HeroFrameRequest.model_validate(request.model_dump())
    except ValidationError as error:
        raise HeroFrameArtifactValidationError(
            f"Hero Frame request must be structurally valid: {error}"
        ) from error
    stored_path = _store_exact_png(
        output_bytes,
        validated_request.product.product_id,
        sha256,
        storage_root,
    )
    return HeroFrameArtifact(
        id=f"hero-frame:{sha256}",
        product_id=validated_request.product.product_id,
        campaign_id=validated_request.campaign_id,
        storyboard_id=validated_request.storyboard_id,
        asset_plan_id=validated_request.asset_plan_id,
        shot_id=validated_request.shot_id,
        source_request_fingerprint=hero_frame_request_fingerprint(
            validated_request
        ),
        local_path=str(stored_path),
        sha256=sha256,
        width=width,
        height=height,
        format="PNG",
        created_at=created_at or datetime.now(timezone.utc),
    )
