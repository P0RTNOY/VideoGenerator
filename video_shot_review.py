"""Manual visual-fidelity reviews and approved Editing source handoffs."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Literal, Optional, Sequence, Tuple, Union

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from product import NonEmptyStr, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name
from storyboard import OverlayKind, Storyboard, StoryboardTextOverlay
from video_shot_artifact import (
    VideoShotArtifact,
    VideoShotArtifactValidationError,
    read_verified_video_shot_bytes,
)


VIDEO_SHOT_REVIEW_CONTEXT_PREFIX = "video-shot-review-context:"

ChecklistResult = Literal["pass", "fail", "not_applicable"]
VideoShotReviewDecision = Literal[
    "approved_for_editing",
    "rejected_for_editing",
]
VideoShotReviewState = Literal[
    "pending_review",
    "approved_for_editing",
    "rejected_for_editing",
]
VideoShotRejectionReason = Literal[
    "geometry_instability",
    "color_or_material_instability",
    "controls_or_ports_instability",
    "panel_or_seam_instability",
    "logo_or_marking_instability",
    "unintended_surface_reconstruction",
    "morphing_or_flicker",
    "subject_motion_unusable",
    "environment_motion_unusable",
    "camera_motion_unusable",
    "scene_inconsistent",
    "added_text",
    "insufficient_usable_range",
    "other",
]


class FrozenVideoReviewModel(ProductModel):
    """Strict immutable base for review authority values."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )


class UsableVideoRange(FrozenVideoReviewModel):
    """One manually authorized chronological range in integer milliseconds."""

    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_positive_span(self) -> "UsableVideoRange":
        if self.end_ms <= self.start_ms:
            raise ValueError("usable video range end_ms must exceed start_ms")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _require_ordered_non_overlapping_ranges(
    ranges: Sequence[UsableVideoRange],
) -> None:
    previous_end = -1
    for usable_range in ranges:
        if usable_range.start_ms < previous_end:
            raise ValueError("usable ranges must be sorted and non-overlapping")
        previous_end = usable_range.end_ms


class VideoFidelityChecklist(FrozenVideoReviewModel):
    """Complete visual product, motion, and editability checklist."""

    geometry_and_proportions_stable: ChecklistResult
    color_variant_and_materials_stable: ChecklistResult
    controls_buttons_and_ports: ChecklistResult
    seams_and_panel_boundaries: ChecklistResult
    logos_markings_and_labels: ChecklistResult
    no_unintended_surface_reconstruction: ChecklistResult
    no_morphing_or_flicker: ChecklistResult
    requested_subject_motion_usable: ChecklistResult
    environment_motion_usable: ChecklistResult
    camera_motion_usable: ChecklistResult
    scene_consistency: ChecklistResult
    no_added_advertising_text: ChecklistResult
    sufficient_contiguous_editable_range: ChecklistResult

    @model_validator(mode="after")
    def restrict_not_applicable_to_visibility_checks(
        self,
    ) -> "VideoFidelityChecklist":
        visibility_dependent = {
            "controls_buttons_and_ports",
            "seams_and_panel_boundaries",
            "logos_markings_and_labels",
        }
        for field_name, value in self.model_dump().items():
            if value == "not_applicable" and field_name not in visibility_dependent:
                raise ValueError(
                    f"{field_name} cannot be not_applicable for video review"
                )
        return self


def _floor_milliseconds(seconds: float) -> int:
    return int(
        (Decimal(str(seconds)) * Decimal("1000")).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


def _ceil_milliseconds(seconds: float) -> int:
    return int(
        (Decimal(str(seconds)) * Decimal("1000")).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def _validated_storyboard(storyboard: Storyboard) -> Storyboard:
    try:
        return Storyboard.model_validate(storyboard.model_dump())
    except ValidationError as error:
        raise VideoShotReviewError(
            f"Storyboard must be structurally valid: {error}"
        ) from error


def _resolve_storyboard_shot(storyboard: Storyboard, shot_id: str):
    matches = [shot for shot in storyboard.shots if shot.id == shot_id]
    if len(matches) != 1:
        raise VideoShotReviewError(
            f"Storyboard shot must resolve exactly once: {shot_id}"
        )
    return matches[0]


def editing_context_fingerprint(storyboard: Storyboard, shot_id: str) -> str:
    """Hash only the current shot inputs that shape downstream Editing intent."""

    validated = _validated_storyboard(storyboard)
    shot = _resolve_storyboard_shot(validated, shot_id)
    payload = {
        "shot_id": shot.id,
        "selected_claim_ids": list(shot.selected_claim_ids),
        "intended_edit_duration_seconds": shot.duration_seconds,
        "text_overlays": [
            overlay.model_dump(mode="json", exclude_none=True)
            for overlay in shot.text_overlays
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _context_payload(values: dict) -> dict:
    return {
        "video_artifact_id": values["video_artifact_id"],
        "video_artifact_sha256": values["video_artifact_sha256"],
        "video_shot_request_fingerprint": values[
            "video_shot_request_fingerprint"
        ],
        "product_id": values["product_id"],
        "campaign_id": values["campaign_id"],
        "storyboard_id": values["storyboard_id"],
        "asset_plan_id": values["asset_plan_id"],
        "shot_id": values["shot_id"],
        "hero_frame_artifact_id": values["hero_frame_artifact_id"],
        "hero_frame_artifact_sha256": values["hero_frame_artifact_sha256"],
        "authorizing_hero_frame_review_context_id": values[
            "authorizing_hero_frame_review_context_id"
        ],
        "authorizing_hero_frame_review_id": values[
            "authorizing_hero_frame_review_id"
        ],
        "width": values["width"],
        "height": values["height"],
        "raw_duration_seconds": values["raw_duration_seconds"],
        "raw_duration_ms": values["raw_duration_ms"],
        "container": values["container"],
        "video_codec": values["video_codec"],
        "has_audio": values["has_audio"],
        "provider_audio_disposition": values["provider_audio_disposition"],
        "editing_context_fingerprint": values["editing_context_fingerprint"],
        "intended_edit_duration_ms": values["intended_edit_duration_ms"],
    }


def _review_context_id(values: dict) -> str:
    canonical = json.dumps(
        _context_payload(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return VIDEO_SHOT_REVIEW_CONTEXT_PREFIX + hashlib.sha256(canonical).hexdigest()


class VideoShotReviewContext(FrozenVideoReviewModel):
    """Path-free exact artifact and Storyboard Editing-intent snapshot."""

    id: NonEmptyStr
    video_artifact_id: NonEmptyStr
    video_artifact_sha256: Sha256Hex
    video_shot_request_fingerprint: Sha256Hex
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    hero_frame_artifact_id: NonEmptyStr
    hero_frame_artifact_sha256: Sha256Hex
    authorizing_hero_frame_review_context_id: NonEmptyStr
    authorizing_hero_frame_review_id: NonEmptyStr
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    raw_duration_seconds: float = Field(gt=0, strict=True)
    raw_duration_ms: int = Field(gt=0, strict=True)
    container: NonEmptyStr
    video_codec: NonEmptyStr
    has_audio: bool
    provider_audio_disposition: Literal["discard_before_editing"] = (
        "discard_before_editing"
    )
    editing_context_fingerprint: Sha256Hex
    intended_edit_duration_ms: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_canonical_identity(self) -> "VideoShotReviewContext":
        if self.video_artifact_id != f"video-shot:{self.video_artifact_sha256}":
            raise ValueError("reviewed Video Shot ID must contain its SHA-256")
        if (
            self.hero_frame_artifact_id
            != f"hero-frame:{self.hero_frame_artifact_sha256}"
        ):
            raise ValueError("reviewed Hero Frame ID must contain its SHA-256")
        if self.raw_duration_ms != _floor_milliseconds(
            self.raw_duration_seconds
        ):
            raise ValueError("raw_duration_ms must conservatively match raw duration")
        if self.id != _review_context_id(self.model_dump(exclude={"id"})):
            raise ValueError("video review context ID must match its snapshot")
        return self


_REASON_CHECKLIST_FIELDS = {
    "geometry_instability": "geometry_and_proportions_stable",
    "color_or_material_instability": "color_variant_and_materials_stable",
    "controls_or_ports_instability": "controls_buttons_and_ports",
    "panel_or_seam_instability": "seams_and_panel_boundaries",
    "logo_or_marking_instability": "logos_markings_and_labels",
    "unintended_surface_reconstruction": (
        "no_unintended_surface_reconstruction"
    ),
    "morphing_or_flicker": "no_morphing_or_flicker",
    "subject_motion_unusable": "requested_subject_motion_usable",
    "environment_motion_unusable": "environment_motion_usable",
    "camera_motion_unusable": "camera_motion_usable",
    "scene_inconsistent": "scene_consistency",
    "added_text": "no_added_advertising_text",
    "insufficient_usable_range": "sufficient_contiguous_editable_range",
}


class VideoShotReview(FrozenVideoReviewModel):
    """One immutable manual decision for one exact video review context."""

    review_id: NonEmptyStr
    review_context_id: NonEmptyStr
    reviewer_actor_id: NonEmptyStr
    decision: VideoShotReviewDecision
    reviewed_at: datetime
    checklist: VideoFidelityChecklist
    usable_ranges: Tuple[UsableVideoRange, ...] = Field(default_factory=tuple)
    rejection_reasons: Tuple[VideoShotRejectionReason, ...] = Field(
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
    def require_decision_consistency(self) -> "VideoShotReview":
        if len(self.rejection_reasons) != len(set(self.rejection_reasons)):
            raise ValueError("rejection reasons must not contain duplicates")
        _require_ordered_non_overlapping_ranges(self.usable_ranges)

        checklist_values = self.checklist.model_dump()
        failed_fields = {
            field_name
            for field_name, value in checklist_values.items()
            if value == "fail"
        }
        if self.decision == "approved_for_editing":
            if failed_fields:
                raise ValueError("approved review cannot contain checklist failures")
            if not self.usable_ranges:
                raise ValueError("approved review requires a manual usable range")
            if self.rejection_reasons:
                raise ValueError("approved review cannot contain rejection reasons")
            return self

        if self.usable_ranges:
            raise ValueError("rejected review cannot authorize usable ranges")
        if not self.rejection_reasons:
            raise ValueError("rejected review requires a rejection reason")
        if "other" in self.rejection_reasons and self.notes is None:
            raise ValueError("other rejection reason requires explanatory notes")
        for reason in self.rejection_reasons:
            field_name = _REASON_CHECKLIST_FIELDS.get(reason)
            if field_name is not None and field_name not in failed_fields:
                raise ValueError(
                    f"rejection reason {reason} requires {field_name}=fail"
                )
        if not failed_fields and "other" not in self.rejection_reasons:
            raise ValueError(
                "rejected review requires a checklist failure or explained other reason"
            )
        return self


def _validate_review_for_context(
    review: VideoShotReview,
    context: VideoShotReviewContext,
) -> None:
    if review.review_context_id != context.id:
        raise VideoShotReviewError(
            "video review does not match the current exact context"
        )
    for usable_range in review.usable_ranges:
        if usable_range.end_ms > context.raw_duration_ms:
            raise VideoShotReviewError(
                "usable video range exceeds current raw artifact duration"
            )
    if review.decision == "approved_for_editing" and not any(
        usable_range.duration_ms >= context.intended_edit_duration_ms
        for usable_range in review.usable_ranges
    ):
        raise VideoShotReviewError(
            "approved review requires one contiguous range long enough for Editing"
        )


class VideoShotReviewHistory(FrozenVideoReviewModel):
    """Ordered append-only manual decisions for one exact context."""

    schema_version: Literal[1] = 1
    review_context: VideoShotReviewContext
    reviews: Tuple[VideoShotReview, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def require_context_and_unique_ids(self) -> "VideoShotReviewHistory":
        review_ids = [review.review_id for review in self.reviews]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("video review IDs must not contain duplicates")
        for review in self.reviews:
            if review.review_context_id != self.review_context.id:
                raise ValueError(
                    f"review does not belong to history context: {review.review_id}"
                )
            try:
                _validate_review_for_context(review, self.review_context)
            except VideoShotReviewError as error:
                raise ValueError(str(error)) from error
        return self


class VideoShotReviewError(ValueError):
    """Base error for controlled Video Shot review failures."""


class VideoShotReviewArtifactError(VideoShotReviewError):
    """Raised when current raw video bytes or metadata are stale."""


class VideoShotReviewPersistenceError(VideoShotReviewError):
    """Raised when append-only review persistence cannot be preserved."""


def _validated_artifact(artifact: VideoShotArtifact) -> VideoShotArtifact:
    try:
        validated = VideoShotArtifact.model_validate(artifact.model_dump())
        read_verified_video_shot_bytes(validated)
        return validated
    except (ValidationError, VideoShotArtifactValidationError) as error:
        raise VideoShotReviewArtifactError(
            f"Video Shot artifact is not current and valid: {error}"
        ) from error


def create_video_shot_review_context(
    artifact: VideoShotArtifact,
    storyboard: Storyboard,
) -> VideoShotReviewContext:
    """Create a deterministic path-free snapshot after verifying current bytes."""

    validated_artifact = _validated_artifact(artifact)
    validated_storyboard = _validated_storyboard(storyboard)
    if validated_artifact.storyboard_id != validated_storyboard.id:
        raise VideoShotReviewError(
            "Video Shot artifact does not belong to the supplied Storyboard"
        )
    if validated_artifact.campaign_id != validated_storyboard.campaign_id:
        raise VideoShotReviewError(
            "Video Shot artifact and Storyboard Campaign IDs do not match"
        )
    shot = _resolve_storyboard_shot(
        validated_storyboard,
        validated_artifact.shot_id,
    )
    raw_duration_ms = _floor_milliseconds(validated_artifact.duration_seconds)
    intended_edit_duration_ms = _ceil_milliseconds(shot.duration_seconds)
    if raw_duration_ms <= 0 or intended_edit_duration_ms <= 0:
        raise VideoShotReviewError("video and intended durations must be at least 1ms")
    values = {
        "video_artifact_id": validated_artifact.id,
        "video_artifact_sha256": validated_artifact.sha256,
        "video_shot_request_fingerprint": (
            validated_artifact.source_request_fingerprint
        ),
        "product_id": validated_artifact.product_id,
        "campaign_id": validated_artifact.campaign_id,
        "storyboard_id": validated_artifact.storyboard_id,
        "asset_plan_id": validated_artifact.asset_plan_id,
        "shot_id": validated_artifact.shot_id,
        "hero_frame_artifact_id": validated_artifact.hero_frame_artifact_id,
        "hero_frame_artifact_sha256": validated_artifact.hero_frame_sha256,
        "authorizing_hero_frame_review_context_id": (
            validated_artifact.authorizing_review_context_id
        ),
        "authorizing_hero_frame_review_id": (
            validated_artifact.authorizing_review_id
        ),
        "width": validated_artifact.width,
        "height": validated_artifact.height,
        "raw_duration_seconds": validated_artifact.duration_seconds,
        "raw_duration_ms": raw_duration_ms,
        "container": validated_artifact.container,
        "video_codec": validated_artifact.video_codec,
        "has_audio": validated_artifact.has_audio,
        "provider_audio_disposition": (
            validated_artifact.provider_audio_disposition
        ),
        "editing_context_fingerprint": editing_context_fingerprint(
            validated_storyboard,
            shot.id,
        ),
        "intended_edit_duration_ms": intended_edit_duration_ms,
    }
    return VideoShotReviewContext(id=_review_context_id(values), **values)


def create_video_shot_review_history(
    artifact: VideoShotArtifact,
    storyboard: Storyboard,
) -> VideoShotReviewHistory:
    return VideoShotReviewHistory(
        review_context=create_video_shot_review_context(artifact, storyboard)
    )


def append_video_shot_review(
    history: VideoShotReviewHistory,
    review: VideoShotReview,
    current_artifact: VideoShotArtifact,
    current_storyboard: Storyboard,
) -> VideoShotReviewHistory:
    """Revalidate current state and append without mutating caller inputs."""

    try:
        validated_history = VideoShotReviewHistory.model_validate(
            history.model_dump()
        )
        validated_review = VideoShotReview.model_validate(review.model_dump())
    except ValidationError as error:
        raise VideoShotReviewError(
            f"video review history and record must be structurally valid: {error}"
        ) from error
    current_context = create_video_shot_review_context(
        current_artifact,
        current_storyboard,
    )
    if validated_history.review_context != current_context:
        raise VideoShotReviewError(
            "video review history does not match the current exact context"
        )
    _validate_review_for_context(validated_review, current_context)
    if any(
        existing.review_id == validated_review.review_id
        for existing in validated_history.reviews
    ):
        raise VideoShotReviewError(
            f"duplicate Video Shot review ID: {validated_review.review_id}"
        )
    return VideoShotReviewHistory(
        review_context=current_context.model_copy(deep=True),
        reviews=(
            *(item.model_copy(deep=True) for item in validated_history.reviews),
            validated_review.model_copy(deep=True),
        ),
    )


def resolve_video_shot_review_state(
    history: VideoShotReviewHistory,
    current_context: VideoShotReviewContext,
) -> VideoShotReviewState:
    """Purely resolve the latest decision for one exact supplied context."""

    try:
        validated_history = VideoShotReviewHistory.model_validate(
            history.model_dump()
        )
        validated_context = VideoShotReviewContext.model_validate(
            current_context.model_dump()
        )
    except ValidationError as error:
        raise VideoShotReviewError(
            f"video review state inputs must be structurally valid: {error}"
        ) from error
    if validated_history.review_context != validated_context:
        return "pending_review"
    if not validated_history.reviews:
        return "pending_review"
    return validated_history.reviews[-1].decision


def _validated_history(
    history: VideoShotReviewHistory,
) -> VideoShotReviewHistory:
    try:
        return VideoShotReviewHistory.model_validate(history.model_dump())
    except ValidationError as error:
        raise VideoShotReviewPersistenceError(
            f"video review history must be structurally valid: {error}"
        ) from error


def video_shot_review_history_path(
    history: VideoShotReviewHistory,
    storage_root: Union[str, Path] = "products",
) -> Path:
    validated = _validated_history(history)
    context_id = validated.review_context.id
    if not context_id.startswith(VIDEO_SHOT_REVIEW_CONTEXT_PREFIX):
        raise VideoShotReviewPersistenceError("invalid video review context prefix")
    context_hash = context_id[len(VIDEO_SHOT_REVIEW_CONTEXT_PREFIX) :]
    if len(context_hash) != 64 or any(
        character not in "0123456789abcdef" for character in context_hash
    ):
        raise VideoShotReviewPersistenceError("invalid video review context hash")
    return (
        Path(storage_root)
        / product_asset_directory_name(validated.review_context.product_id)
        / "reviews"
        / "video-shots"
        / f"{context_hash}.json"
    )


def _read_persisted_history(path: Path) -> VideoShotReviewHistory:
    try:
        if path.is_symlink():
            raise VideoShotReviewPersistenceError(
                "video review history path must not be a symbolic link"
            )
        return VideoShotReviewHistory.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except VideoShotReviewPersistenceError:
        raise
    except (OSError, UnicodeError, ValidationError) as error:
        raise VideoShotReviewPersistenceError(
            f"video review history could not be loaded: {path}"
        ) from error


def save_video_shot_review_history(
    history: VideoShotReviewHistory,
    storage_root: Union[str, Path] = "products",
) -> Path:
    """Atomically persist a history while enforcing an append-only prefix."""

    validated = _validated_history(history)
    root = Path(storage_root)
    target = video_shot_review_history_path(validated, root)
    directory = target.parent
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise VideoShotReviewPersistenceError(
                "video review directory resolves outside its storage root"
            ) from error
        if target.is_symlink():
            raise VideoShotReviewPersistenceError(
                "video review history path must not be a symbolic link"
            )
        if target.exists():
            if not target.is_file():
                raise VideoShotReviewPersistenceError(
                    "video review history path is not a regular file"
                )
            existing = _read_persisted_history(target)
            if existing.review_context != validated.review_context:
                raise VideoShotReviewPersistenceError(
                    "persisted video review context cannot be replaced"
                )
            existing_count = len(existing.reviews)
            if (
                existing_count > len(validated.reviews)
                or existing.reviews != validated.reviews[:existing_count]
            ):
                raise VideoShotReviewPersistenceError(
                    "persisted video review records are append-only"
                )

        json_text = validated.model_dump_json(
            indent=2,
            ensure_ascii=False,
            exclude_none=True,
        ) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".video-shot-review-",
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
    except VideoShotReviewPersistenceError:
        raise
    except OSError as error:
        raise VideoShotReviewPersistenceError(
            f"video review history could not be saved: {target}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_video_shot_review_history(
    current_artifact: VideoShotArtifact,
    current_storyboard: Storyboard,
    storage_root: Union[str, Path] = "products",
) -> VideoShotReviewHistory:
    empty = create_video_shot_review_history(
        current_artifact,
        current_storyboard,
    )
    path = video_shot_review_history_path(empty, storage_root)
    if not path.exists():
        return empty
    loaded = _read_persisted_history(path)
    if loaded.review_context != empty.review_context:
        raise VideoShotReviewPersistenceError(
            "persisted video review does not match the current exact context"
        )
    return loaded


class EditingTextOverlay(FrozenVideoReviewModel):
    """Immutable handoff snapshot of one current Storyboard overlay value."""

    kind: OverlayKind
    text: Optional[NonEmptyStr] = None
    claim_id: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def require_storyboard_overlay_contract(self) -> "EditingTextOverlay":
        StoryboardTextOverlay.model_validate(self.model_dump())
        return self


class EditingRuntimeVideo(FrozenVideoReviewModel):
    """Verified local handle to the exact raw source bytes."""

    artifact_id: NonEmptyStr
    local_path: Path
    sha256: Sha256Hex
    raw_duration_ms: int = Field(gt=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    container: NonEmptyStr
    video_codec: NonEmptyStr
    has_audio: bool

    @model_validator(mode="after")
    def require_content_addressed_identity(self) -> "EditingRuntimeVideo":
        if self.artifact_id != f"video-shot:{self.sha256}":
            raise ValueError("runtime video ID must contain its SHA-256")
        return self


class EditingSourceHandoff(FrozenVideoReviewModel):
    """Ephemeral authorization for future Editing to use exact safe ranges."""

    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    video_artifact_id: NonEmptyStr
    video_artifact_sha256: Sha256Hex
    authorizing_video_review_context_id: NonEmptyStr
    authorizing_video_review_id: NonEmptyStr
    runtime_video: EditingRuntimeVideo
    approved_usable_ranges: Tuple[UsableVideoRange, ...]
    intended_edit_duration_ms: int = Field(gt=0, strict=True)
    text_overlays: Tuple[EditingTextOverlay, ...] = Field(default_factory=tuple)
    provider_audio_disposition: Literal["discard_before_editing"] = (
        "discard_before_editing"
    )

    @model_validator(mode="after")
    def require_authorized_source_consistency(self) -> "EditingSourceHandoff":
        if self.video_artifact_id != self.runtime_video.artifact_id:
            raise ValueError("runtime video must match the authorized artifact")
        if self.video_artifact_sha256 != self.runtime_video.sha256:
            raise ValueError("runtime video SHA must match the authorized artifact")
        if not self.approved_usable_ranges:
            raise ValueError("Editing handoff requires an approved usable range")
        _require_ordered_non_overlapping_ranges(self.approved_usable_ranges)
        if any(
            usable_range.end_ms > self.runtime_video.raw_duration_ms
            for usable_range in self.approved_usable_ranges
        ):
            raise ValueError("Editing handoff range exceeds raw video duration")
        if not any(
            usable_range.duration_ms >= self.intended_edit_duration_ms
            for usable_range in self.approved_usable_ranges
        ):
            raise ValueError(
                "Editing handoff requires one sufficiently long contiguous range"
            )
        return self


def build_editing_source_handoff(
    artifact: VideoShotArtifact,
    storyboard: Storyboard,
    history: VideoShotReviewHistory,
) -> EditingSourceHandoff:
    """Return an ephemeral source only for the latest exact approved review."""

    current_context = create_video_shot_review_context(artifact, storyboard)
    try:
        validated_history = VideoShotReviewHistory.model_validate(
            history.model_dump()
        )
        validated_artifact = VideoShotArtifact.model_validate(artifact.model_dump())
        validated_storyboard = Storyboard.model_validate(storyboard.model_dump())
    except ValidationError as error:
        raise VideoShotReviewError(
            f"Editing handoff inputs must be structurally valid: {error}"
        ) from error
    state = resolve_video_shot_review_state(validated_history, current_context)
    if state != "approved_for_editing":
        raise VideoShotReviewError(
            f"Video Shot review state is {state}; exact approval is required"
        )
    authorizing_review = validated_history.reviews[-1]
    _validate_review_for_context(authorizing_review, current_context)
    shot = _resolve_storyboard_shot(
        validated_storyboard,
        validated_artifact.shot_id,
    )

    # Recheck exact bytes immediately before exposing the runtime handle.
    try:
        read_verified_video_shot_bytes(validated_artifact)
    except VideoShotArtifactValidationError as error:
        raise VideoShotReviewArtifactError(
            f"Video Shot artifact changed before Editing handoff: {error}"
        ) from error
    return EditingSourceHandoff(
        product_id=current_context.product_id,
        campaign_id=current_context.campaign_id,
        storyboard_id=current_context.storyboard_id,
        asset_plan_id=current_context.asset_plan_id,
        shot_id=current_context.shot_id,
        video_artifact_id=current_context.video_artifact_id,
        video_artifact_sha256=current_context.video_artifact_sha256,
        authorizing_video_review_context_id=current_context.id,
        authorizing_video_review_id=authorizing_review.review_id,
        runtime_video=EditingRuntimeVideo(
            artifact_id=validated_artifact.id,
            local_path=Path(validated_artifact.local_path),
            sha256=validated_artifact.sha256,
            raw_duration_ms=current_context.raw_duration_ms,
            width=validated_artifact.width,
            height=validated_artifact.height,
            container=validated_artifact.container,
            video_codec=validated_artifact.video_codec,
            has_audio=validated_artifact.has_audio,
        ),
        approved_usable_ranges=tuple(
            item.model_copy(deep=True) for item in authorizing_review.usable_ranges
        ),
        intended_edit_duration_ms=current_context.intended_edit_duration_ms,
        text_overlays=tuple(
            EditingTextOverlay.model_validate(overlay.model_dump())
            for overlay in shot.text_overlays
        ),
        provider_audio_disposition=current_context.provider_audio_disposition,
    )
