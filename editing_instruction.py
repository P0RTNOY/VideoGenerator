"""Ephemeral provider-neutral render instructions compiled from current authority."""

from pathlib import Path
from typing import Literal, Optional, Sequence, Tuple

from pydantic import ConfigDict, Field, ValidationError

from campaign import AdCampaign
from editing_audio_assets import (
    EditingAudioManifest,
    EditingAudioManifestError,
    resolve_verified_editing_audio_asset,
)
from editing_plan import (
    AudioMixPolicy,
    EditingMusicSelection,
    EditingOutputSpec,
    EditingOverlayCue,
    EditingPlan,
    EditingPlanError,
    EditingSegmentSelection,
    EditingSfxSelection,
    build_editing_plan,
)
from product import NonEmptyStr, Product, ProductModel
from product_image_downloads import Sha256Hex
from storyboard import Storyboard
from video_shot_artifact import VideoShotArtifact
from video_shot_review import (
    EditingSourceHandoff,
    VideoShotReviewError,
    VideoShotReviewHistory,
    build_editing_source_handoff,
)


DEFAULT_SOCIAL_BOLD_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/System/Library/Fonts/Helvetica.ttc"),
    Path("/Library/Fonts/Arial Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
)


class FrozenEditingInstructionModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class RuntimeVideoSegment(FrozenEditingInstructionModel):
    shot_id: NonEmptyStr
    artifact_id: NonEmptyStr
    sha256: Sha256Hex
    local_path: Path
    source_start_ms: int = Field(ge=0, strict=True)
    source_end_ms: int = Field(gt=0, strict=True)
    timeline_start_ms: int = Field(ge=0, strict=True)
    timeline_end_ms: int = Field(gt=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    resize_mode: Literal["none", "downscale"]
    transition: Literal["cut"] = "cut"
    source_audio: Literal["discard"] = "discard"


class RuntimeMusic(FrozenEditingInstructionModel):
    asset_id: NonEmptyStr
    sha256: Sha256Hex
    local_path: Path
    source_start_ms: int = Field(ge=0, strict=True)
    source_end_ms: int = Field(gt=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)
    loop: Literal[False] = False


class RuntimeSfx(FrozenEditingInstructionModel):
    asset_id: NonEmptyStr
    sha256: Sha256Hex
    local_path: Path
    source_start_ms: Literal[0] = 0
    source_end_ms: int = Field(gt=0, strict=True)
    timeline_start_ms: int = Field(ge=0, strict=True)
    timeline_end_ms: int = Field(gt=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)


class EditingInstruction(FrozenEditingInstructionModel):
    """Current verified runtime handles plus unchanged durable plan intent."""

    editing_plan_id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    target_duration_ms: int = Field(gt=0, strict=True)
    video_segments: Tuple[RuntimeVideoSegment, ...] = Field(min_length=1)
    overlays: Tuple[EditingOverlayCue, ...] = Field(default_factory=tuple)
    runtime_font_path: Path
    music: Optional[RuntimeMusic] = None
    sfx: Tuple[RuntimeSfx, ...] = Field(default_factory=tuple)
    source_video_audio: Literal["discard"] = "discard"
    audio_mix: AudioMixPolicy
    output: EditingOutputSpec


class EditingInstructionError(ValueError):
    """Raised when current runtime state no longer authorizes the Plan."""


def resolve_social_bold_font(
    candidates: Optional[Sequence[Path]] = None,
) -> Path:
    """Resolve the sole v1 style preset without placing a path in EditingPlan."""

    selected_candidates = (
        DEFAULT_SOCIAL_BOLD_FONT_CANDIDATES
        if candidates is None
        else candidates
    )
    for candidate in selected_candidates:
        path = Path(candidate)
        if path.is_file() and not path.is_symlink():
            return path.resolve()
    raise EditingInstructionError(
        "social_bold_v1 requires a supported installed bold font"
    )


def _rebuild_handoffs(
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
        raise EditingInstructionError(f"current video review inputs are invalid: {error}") from error
    if len(current_artifacts) != len(current_histories):
        raise EditingInstructionError("each current video artifact requires one review history")
    artifacts_by_shot = {}
    histories_by_shot = {}
    for artifact in current_artifacts:
        if artifact.shot_id in artifacts_by_shot:
            raise EditingInstructionError(f"duplicate current video artifact for shot {artifact.shot_id}")
        artifacts_by_shot[artifact.shot_id] = artifact
    for history in current_histories:
        shot_id = history.review_context.shot_id
        if shot_id in histories_by_shot:
            raise EditingInstructionError(f"duplicate current video review history for shot {shot_id}")
        histories_by_shot[shot_id] = history
    expected = [shot.id for shot in storyboard.shots]
    if set(artifacts_by_shot) != set(expected) or set(histories_by_shot) != set(expected):
        raise EditingInstructionError("current video state must cover every Storyboard shot exactly")
    handoffs = []
    for shot_id in expected:
        try:
            handoffs.append(
                build_editing_source_handoff(
                    artifacts_by_shot[shot_id], storyboard, histories_by_shot[shot_id]
                )
            )
        except VideoShotReviewError as error:
            raise EditingInstructionError(
                f"current source for shot {shot_id} is not authorized: {error}"
            ) from error
    return tuple(handoffs)


def compile_editing_instruction(
    *,
    plan: EditingPlan,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    video_artifacts: Sequence[VideoShotArtifact],
    video_review_histories: Sequence[VideoShotReviewHistory],
    audio_manifest: Optional[EditingAudioManifest],
    font_candidates: Optional[Sequence[Path]] = None,
) -> EditingInstruction:
    """Rebuild current approvals and compile an ephemeral exact render instruction."""

    try:
        current_plan = EditingPlan.model_validate(plan.model_dump())
    except ValidationError as error:
        raise EditingInstructionError(f"Editing Plan is structurally invalid: {error}") from error
    handoffs = _rebuild_handoffs(storyboard, video_artifacts, video_review_histories)

    segment_selections = tuple(
        EditingSegmentSelection(
            shot_id=item.shot_id,
            source_start_ms=item.source_start_ms,
        )
        for item in current_plan.segments
    )
    overlay_timings = tuple(
        {
            "shot_id": item.shot_id,
            "overlay_index": item.overlay_index,
            "timeline_start_ms": item.timeline_start_ms,
            "timeline_end_ms": item.timeline_end_ms,
            "placement": item.placement,
        }
        for item in current_plan.overlays
    )
    music_selection = (
        None
        if current_plan.music is None
        else EditingMusicSelection(
            asset_id=current_plan.music.asset.asset_id,
            source_start_ms=current_plan.music.source_start_ms,
            gain_db=current_plan.music.gain_db,
        )
    )
    sfx_selections = tuple(
        EditingSfxSelection(
            asset_id=item.asset.asset_id,
            timeline_start_ms=item.timeline_start_ms,
            gain_db=item.gain_db,
        )
        for item in current_plan.sfx
    )
    try:
        rebuilt_plan = build_editing_plan(
            product,
            campaign,
            storyboard,
            handoffs,
            segment_selections,
            overlay_timings,
            audio_manifest=audio_manifest,
            music_selection=music_selection,
            sfx_selections=sfx_selections,
        )
    except EditingPlanError as error:
        raise EditingInstructionError(f"current context no longer authorizes Editing Plan: {error}") from error
    if rebuilt_plan != current_plan:
        raise EditingInstructionError("Editing Plan is stale for the current exact authorization context")

    handoffs_by_shot = {item.shot_id: item for item in handoffs}
    runtime_segments = []
    for segment in current_plan.segments:
        handoff = handoffs_by_shot[segment.shot_id]
        width = handoff.runtime_video.width
        height = handoff.runtime_video.height
        if width * 1280 != height * 720:
            raise EditingInstructionError(f"source {segment.shot_id} is not exact 9:16 and cannot be cropped or stretched")
        if width < 720 or height < 1280:
            raise EditingInstructionError(f"source {segment.shot_id} would require forbidden upscaling")
        runtime_segments.append(
            RuntimeVideoSegment(
                shot_id=segment.shot_id,
                artifact_id=handoff.runtime_video.artifact_id,
                sha256=handoff.runtime_video.sha256,
                local_path=handoff.runtime_video.local_path,
                source_start_ms=segment.source_start_ms,
                source_end_ms=segment.source_end_ms,
                timeline_start_ms=segment.timeline_start_ms,
                timeline_end_ms=segment.timeline_end_ms,
                width=width,
                height=height,
                resize_mode="none" if (width, height) == (720, 1280) else "downscale",
            )
        )

    runtime_music = None
    runtime_sfx = []
    if current_plan.music is not None:
        try:
            asset = resolve_verified_editing_audio_asset(
                audio_manifest, current_plan.music.asset.asset_id, expected_kind="music"
            )
        except EditingAudioManifestError as error:
            raise EditingInstructionError(f"music is no longer verified: {error}") from error
        runtime_music = RuntimeMusic(
            asset_id=asset.id,
            sha256=asset.sha256,
            local_path=Path(asset.local_path),
            source_start_ms=current_plan.music.source_start_ms,
            source_end_ms=current_plan.music.source_end_ms,
            gain_db=current_plan.music.gain_db,
        )
    for cue in current_plan.sfx:
        try:
            asset = resolve_verified_editing_audio_asset(
                audio_manifest, cue.asset.asset_id, expected_kind="sfx"
            )
        except EditingAudioManifestError as error:
            raise EditingInstructionError(f"SFX is no longer verified: {error}") from error
        runtime_sfx.append(
            RuntimeSfx(
                asset_id=asset.id,
                sha256=asset.sha256,
                local_path=Path(asset.local_path),
                source_end_ms=cue.source_end_ms,
                timeline_start_ms=cue.timeline_start_ms,
                timeline_end_ms=cue.timeline_end_ms,
                gain_db=cue.gain_db,
            )
        )

    return EditingInstruction(
        editing_plan_id=current_plan.id,
        product_id=current_plan.product_id,
        campaign_id=current_plan.campaign_id,
        storyboard_id=current_plan.storyboard_id,
        target_duration_ms=current_plan.target_duration_ms,
        video_segments=tuple(runtime_segments),
        overlays=tuple(item.model_copy(deep=True) for item in current_plan.overlays),
        runtime_font_path=resolve_social_bold_font(font_candidates),
        music=runtime_music,
        sfx=tuple(runtime_sfx),
        source_video_audio="discard",
        audio_mix=current_plan.audio_mix.model_copy(deep=True),
        output=current_plan.output.model_copy(deep=True),
    )
