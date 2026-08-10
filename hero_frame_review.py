"""Manual, context-bound fidelity reviews for generated Hero Frames."""

import hashlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Tuple, Union

from PIL import Image, UnidentifiedImageError
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from hero_frame_artifact import HeroFrameArtifact
from product import NonEmptyStr, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name


HERO_FRAME_REVIEW_CONTEXT_PREFIX = "hero-frame-review-context:"

ChecklistResult = Literal["pass", "fail", "not_applicable"]
HeroFrameReviewDecision = Literal[
    "approved_for_video_start_image",
    "rejected_for_video_start_image",
]
HeroFrameReviewState = Literal[
    "pending_review",
    "approved_for_video_start_image",
    "rejected_for_video_start_image",
]
HeroFrameRejectionReason = Literal[
    "geometry_mismatch",
    "color_or_material_mismatch",
    "controls_or_ports_mismatch",
    "panel_or_seam_mismatch",
    "logo_or_marking_mismatch",
    "invented_or_missing_product_detail",
    "added_text",
    "scene_or_framing_unusable",
    "other",
]


class FrozenReviewModel(ProductModel):
    """Strict immutable base for durable append-only review values."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )


class HeroFrameFidelityChecklist(FrozenReviewModel):
    """Complete manual checklist; no item is decided automatically."""

    geometry_and_proportions: ChecklistResult
    color_variant_and_materials: ChecklistResult
    controls_buttons_and_ports: ChecklistResult
    seams_and_panel_boundaries: ChecklistResult
    logos_markings_and_labels: ChecklistResult
    no_invented_or_missing_construction: ChecklistResult
    no_added_advertising_text: ChecklistResult
    scene_and_framing_usable: ChecklistResult


def _context_payload_from_values(values: dict) -> dict:
    return {
        "artifact_id": values["artifact_id"],
        "artifact_sha256": values["artifact_sha256"],
        "source_request_fingerprint": values["source_request_fingerprint"],
        "product_id": values["product_id"],
        "campaign_id": values["campaign_id"],
        "storyboard_id": values["storyboard_id"],
        "asset_plan_id": values["asset_plan_id"],
        "shot_id": values["shot_id"],
        "width": values["width"],
        "height": values["height"],
        "format": values["format"],
    }


def _context_id(values: dict) -> str:
    canonical = json.dumps(
        _context_payload_from_values(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return HERO_FRAME_REVIEW_CONTEXT_PREFIX + hashlib.sha256(canonical).hexdigest()


class HeroFrameReviewContext(FrozenReviewModel):
    """Path-free immutable snapshot defining the exact review authority scope."""

    id: NonEmptyStr
    artifact_id: NonEmptyStr
    artifact_sha256: Sha256Hex
    source_request_fingerprint: Sha256Hex
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    format: Literal["PNG"] = "PNG"

    @model_validator(mode="after")
    def require_content_and_context_addressed_identity(
        self,
    ) -> "HeroFrameReviewContext":
        if self.artifact_id != f"hero-frame:{self.artifact_sha256}":
            raise ValueError("review context artifact ID must contain its SHA-256")
        if self.id != _context_id(self.model_dump(exclude={"id"})):
            raise ValueError("review context ID must match its canonical snapshot")
        return self


class HeroFrameReview(FrozenReviewModel):
    """One immutable-attribution manual decision for an exact review context."""

    review_id: NonEmptyStr
    review_context_id: NonEmptyStr
    reviewer_actor_id: NonEmptyStr
    decision: HeroFrameReviewDecision
    reviewed_at: datetime
    checklist: HeroFrameFidelityChecklist
    rejection_reasons: Tuple[HeroFrameRejectionReason, ...] = Field(
        default_factory=tuple
    )
    notes: Optional[NonEmptyStr] = None

    @field_validator("reviewed_at")
    @classmethod
    def require_utc_aware_reviewed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_decision_consistency(self) -> "HeroFrameReview":
        if len(self.rejection_reasons) != len(set(self.rejection_reasons)):
            raise ValueError("rejection reasons must not contain duplicates")
        checklist_values = list(self.checklist.model_dump().values())
        has_failure = "fail" in checklist_values
        if self.decision == "approved_for_video_start_image":
            if has_failure:
                raise ValueError("approved review cannot contain a failed checklist item")
            if self.rejection_reasons:
                raise ValueError("approved review cannot contain rejection reasons")
            return self

        if not self.rejection_reasons:
            raise ValueError("rejected review requires at least one rejection reason")
        has_scene_rejection = (
            "scene_or_framing_unusable" in self.rejection_reasons
        )
        if not has_failure and not has_scene_rejection:
            raise ValueError(
                "rejected review requires a failed checklist item or a "
                "scene/framing usability reason"
            )
        if (
            has_scene_rejection
            and self.checklist.scene_and_framing_usable == "pass"
        ):
            raise ValueError(
                "scene/framing rejection contradicts a passed scene checklist"
            )
        if "other" in self.rejection_reasons and self.notes is None:
            raise ValueError("other rejection reason requires explanatory notes")
        return self


class HeroFrameReviewHistory(FrozenReviewModel):
    """Ordered append-only decisions for one exact review context."""

    schema_version: Literal[1] = 1
    review_context: HeroFrameReviewContext
    reviews: Tuple[HeroFrameReview, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def require_context_binding_and_unique_review_ids(
        self,
    ) -> "HeroFrameReviewHistory":
        review_ids = [review.review_id for review in self.reviews]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("review history must not contain duplicate review IDs")
        for review in self.reviews:
            if review.review_context_id != self.review_context.id:
                raise ValueError(
                    f"review does not belong to history context: {review.review_id}"
                )
        return self


class HeroFrameReviewError(ValueError):
    """Base error for controlled manual review failures."""


class HeroFrameReviewArtifactError(HeroFrameReviewError):
    """Raised when current artifact metadata or bytes are stale."""


class HeroFrameReviewPersistenceError(HeroFrameReviewError):
    """Raised when append-only local review persistence cannot be preserved."""


def _validate_current_artifact(artifact: HeroFrameArtifact) -> HeroFrameArtifact:
    try:
        validated = HeroFrameArtifact.model_validate(artifact.model_dump())
    except ValidationError as error:
        raise HeroFrameReviewArtifactError(
            f"Hero Frame artifact must be structurally valid: {error}"
        ) from error
    try:
        data = Path(validated.local_path).read_bytes()
    except OSError as error:
        raise HeroFrameReviewArtifactError(
            f"Hero Frame artifact could not be read: {validated.id}"
        ) from error
    if hashlib.sha256(data).hexdigest() != validated.sha256:
        raise HeroFrameReviewArtifactError(
            f"Hero Frame artifact SHA-256 changed: {validated.id}"
        )
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            decoded_format = image.format
            decoded_size = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise HeroFrameReviewArtifactError(
            f"Hero Frame artifact could not be decoded: {validated.id}"
        ) from error
    if decoded_format != validated.format:
        raise HeroFrameReviewArtifactError(
            f"Hero Frame artifact format changed: {validated.id}"
        )
    if decoded_size != (validated.width, validated.height):
        raise HeroFrameReviewArtifactError(
            f"Hero Frame artifact dimensions changed: {validated.id}"
        )
    return validated


def create_hero_frame_review_context(
    artifact: HeroFrameArtifact,
) -> HeroFrameReviewContext:
    """Create a deterministic path-free context from a current valid artifact."""

    validated = _validate_current_artifact(artifact)
    values = {
        "artifact_id": validated.id,
        "artifact_sha256": validated.sha256,
        "source_request_fingerprint": validated.source_request_fingerprint,
        "product_id": validated.product_id,
        "campaign_id": validated.campaign_id,
        "storyboard_id": validated.storyboard_id,
        "asset_plan_id": validated.asset_plan_id,
        "shot_id": validated.shot_id,
        "width": validated.width,
        "height": validated.height,
        "format": validated.format,
    }
    return HeroFrameReviewContext(id=_context_id(values), **values)


def create_hero_frame_review_history(
    artifact: HeroFrameArtifact,
) -> HeroFrameReviewHistory:
    """Create an empty pending history for an exact current artifact context."""

    return HeroFrameReviewHistory(
        review_context=create_hero_frame_review_context(artifact),
    )


def append_hero_frame_review(
    history: HeroFrameReviewHistory,
    review: HeroFrameReview,
    current_artifact: HeroFrameArtifact,
) -> HeroFrameReviewHistory:
    """Revalidate and append one record without mutating any caller input."""

    try:
        validated_history = HeroFrameReviewHistory.model_validate(
            history.model_dump()
        )
        validated_review = HeroFrameReview.model_validate(review.model_dump())
    except ValidationError as error:
        raise HeroFrameReviewError(
            f"review history and record must be structurally valid: {error}"
        ) from error
    current_context = create_hero_frame_review_context(current_artifact)
    if validated_history.review_context != current_context:
        raise HeroFrameReviewError(
            "review history does not match the current artifact context"
        )
    if validated_review.review_context_id != current_context.id:
        raise HeroFrameReviewError(
            "review record does not match the current artifact context"
        )
    if any(
        existing.review_id == validated_review.review_id
        for existing in validated_history.reviews
    ):
        raise HeroFrameReviewError(
            f"duplicate Hero Frame review ID: {validated_review.review_id}"
        )
    return HeroFrameReviewHistory(
        review_context=current_context.model_copy(deep=True),
        reviews=(
            *(item.model_copy(deep=True) for item in validated_history.reviews),
            validated_review.model_copy(deep=True),
        ),
    )


def resolve_hero_frame_review_state(
    history: HeroFrameReviewHistory,
    current_artifact: HeroFrameArtifact,
) -> HeroFrameReviewState:
    """Resolve only the latest review for the exact current artifact context."""

    try:
        validated_history = HeroFrameReviewHistory.model_validate(
            history.model_dump()
        )
    except ValidationError as error:
        raise HeroFrameReviewError(
            f"review history must be structurally valid: {error}"
        ) from error
    current_context = create_hero_frame_review_context(current_artifact)
    if validated_history.review_context != current_context:
        return "pending_review"
    if not validated_history.reviews:
        return "pending_review"
    return validated_history.reviews[-1].decision


def _validated_history(history: HeroFrameReviewHistory) -> HeroFrameReviewHistory:
    try:
        return HeroFrameReviewHistory.model_validate(history.model_dump())
    except ValidationError as error:
        raise HeroFrameReviewPersistenceError(
            f"review history must be structurally valid: {error}"
        ) from error


def review_history_path(
    history: HeroFrameReviewHistory,
    storage_root: Union[str, Path] = "products",
) -> Path:
    """Return the product-scoped hash-only path for one review history."""

    validated = _validated_history(history)
    context_id = validated.review_context.id
    if not context_id.startswith(HERO_FRAME_REVIEW_CONTEXT_PREFIX):
        raise HeroFrameReviewPersistenceError("invalid review context prefix")
    context_hash = context_id[len(HERO_FRAME_REVIEW_CONTEXT_PREFIX) :]
    if len(context_hash) != 64 or any(
        character not in "0123456789abcdef" for character in context_hash
    ):
        raise HeroFrameReviewPersistenceError("invalid review context hash")
    return (
        Path(storage_root)
        / product_asset_directory_name(validated.review_context.product_id)
        / "reviews"
        / "hero-frames"
        / f"{context_hash}.json"
    )


def _read_persisted_history(path: Path) -> HeroFrameReviewHistory:
    try:
        if path.is_symlink():
            raise HeroFrameReviewPersistenceError(
                "review history path must not be a symbolic link"
            )
        raw = path.read_text(encoding="utf-8")
        return HeroFrameReviewHistory.model_validate_json(raw)
    except HeroFrameReviewPersistenceError:
        raise
    except (OSError, UnicodeError, ValidationError) as error:
        raise HeroFrameReviewPersistenceError(
            f"review history could not be loaded: {path}"
        ) from error


def save_hero_frame_review_history(
    history: HeroFrameReviewHistory,
    storage_root: Union[str, Path] = "products",
) -> Path:
    """Atomically write a complete history while preventing prior-record edits."""

    validated = _validated_history(history)
    root = Path(storage_root)
    target = review_history_path(validated, root)
    directory = target.parent
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise HeroFrameReviewPersistenceError(
                "review history directory resolves outside its storage root"
            ) from error
        if target.is_symlink():
            raise HeroFrameReviewPersistenceError(
                "review history path must not be a symbolic link"
            )
        if target.exists():
            if not target.is_file():
                raise HeroFrameReviewPersistenceError(
                    "review history path is not a regular file"
                )
            existing = _read_persisted_history(target)
            if existing.review_context != validated.review_context:
                raise HeroFrameReviewPersistenceError(
                    "persisted review context cannot be replaced"
                )
            existing_count = len(existing.reviews)
            if (
                existing_count > len(validated.reviews)
                or existing.reviews != validated.reviews[:existing_count]
            ):
                raise HeroFrameReviewPersistenceError(
                    "persisted review records are append-only"
                )

        json_text = validated.model_dump_json(
            indent=2,
            ensure_ascii=False,
            exclude_none=True,
        ) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".hero-frame-review-",
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
    except HeroFrameReviewPersistenceError:
        raise
    except OSError as error:
        raise HeroFrameReviewPersistenceError(
            f"review history could not be saved: {target}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_hero_frame_review_history(
    current_artifact: HeroFrameArtifact,
    storage_root: Union[str, Path] = "products",
) -> HeroFrameReviewHistory:
    """Load exact-context history or return a new empty pending history."""

    empty_history = create_hero_frame_review_history(current_artifact)
    path = review_history_path(empty_history, storage_root)
    if not path.exists():
        return empty_history
    loaded = _read_persisted_history(path)
    if loaded.review_context != empty_history.review_context:
        raise HeroFrameReviewPersistenceError(
            "persisted review history does not match current artifact context"
        )
    return loaded
