"""Durable, path-free editing intent for one complete final advertisement."""

import hashlib
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Optional, Sequence, Tuple, Union

from pydantic import ConfigDict, Field, ValidationError, model_validator

from campaign import AdCampaign
from editing_audio_assets import (
    EditingAudioAsset,
    EditingAudioManifest,
    EditingAudioManifestError,
    resolve_verified_editing_audio_asset,
)
from product import NonEmptyStr, Product, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name
from storyboard import Storyboard, StoryboardContextError, validate_storyboard
from video_shot_review import EditingSourceHandoff, UsableVideoRange


OverlayPlacement = Literal["top_center", "center", "bottom_center"]


class FrozenEditingPlanModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class EditingSegmentSelection(FrozenEditingPlanModel):
    """Caller's explicit source offset for one Storyboard shot."""

    shot_id: NonEmptyStr
    source_start_ms: int = Field(ge=0, strict=True)


class EditingOverlayTiming(FrozenEditingPlanModel):
    """Caller's explicit timing and coarse placement for one overlay."""

    shot_id: NonEmptyStr
    overlay_index: int = Field(ge=0, strict=True)
    timeline_start_ms: int = Field(ge=0, strict=True)
    timeline_end_ms: int = Field(gt=0, strict=True)
    placement: OverlayPlacement

    @model_validator(mode="after")
    def require_positive_interval(self) -> "EditingOverlayTiming":
        if self.timeline_end_ms <= self.timeline_start_ms:
            raise ValueError("overlay timing must have positive duration")
        return self


class EditingMusicSelection(FrozenEditingPlanModel):
    asset_id: NonEmptyStr
    source_start_ms: int = Field(ge=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)


class EditingSfxSelection(FrozenEditingPlanModel):
    asset_id: NonEmptyStr
    timeline_start_ms: int = Field(ge=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)


class EditingSourceSnapshot(FrozenEditingPlanModel):
    """Minimal durable authorization snapshot; deliberately contains no path."""

    shot_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    video_artifact_id: NonEmptyStr
    video_artifact_sha256: Sha256Hex
    authorizing_video_review_context_id: NonEmptyStr
    authorizing_video_review_id: NonEmptyStr
    approved_usable_ranges: Tuple[UsableVideoRange, ...] = Field(min_length=1)


class EditingSegment(FrozenEditingPlanModel):
    shot_id: NonEmptyStr
    source: EditingSourceSnapshot
    source_start_ms: int = Field(ge=0, strict=True)
    source_end_ms: int = Field(gt=0, strict=True)
    timeline_start_ms: int = Field(ge=0, strict=True)
    timeline_end_ms: int = Field(gt=0, strict=True)
    transition: Literal["cut"] = "cut"

    @model_validator(mode="after")
    def require_equal_authorized_intervals(self) -> "EditingSegment":
        if self.source.shot_id != self.shot_id:
            raise ValueError("segment source shot must match segment shot")
        source_duration = self.source_end_ms - self.source_start_ms
        timeline_duration = self.timeline_end_ms - self.timeline_start_ms
        if source_duration <= 0 or source_duration != timeline_duration:
            raise ValueError("source and timeline durations must be equal and positive")
        if not any(
            item.start_ms <= self.source_start_ms
            and self.source_end_ms <= item.end_ms
            for item in self.source.approved_usable_ranges
        ):
            raise ValueError("segment must fit wholly inside one approved range")
        return self


class EditingOverlayCue(FrozenEditingPlanModel):
    shot_id: NonEmptyStr
    overlay_index: int = Field(ge=0, strict=True)
    kind: Literal["claim", "creative", "product_name", "cta"]
    claim_id: Optional[NonEmptyStr] = None
    resolved_text: NonEmptyStr
    timeline_start_ms: int = Field(ge=0, strict=True)
    timeline_end_ms: int = Field(gt=0, strict=True)
    placement: OverlayPlacement
    style_preset_id: Literal["social_bold_v1"] = "social_bold_v1"

    @model_validator(mode="after")
    def require_kind_and_timing(self) -> "EditingOverlayCue":
        if self.timeline_end_ms <= self.timeline_start_ms:
            raise ValueError("overlay cue must have positive duration")
        if self.kind == "claim" and self.claim_id is None:
            raise ValueError("claim cue requires a claim ID")
        if self.kind != "claim" and self.claim_id is not None:
            raise ValueError("only claim cues may contain a claim ID")
        return self


class EditingAudioSnapshot(FrozenEditingPlanModel):
    asset_id: NonEmptyStr
    kind: Literal["music", "sfx"]
    sha256: Sha256Hex
    duration_ms: int = Field(gt=0, strict=True)


class EditingMusicBed(FrozenEditingPlanModel):
    asset: EditingAudioSnapshot
    source_start_ms: int = Field(ge=0, strict=True)
    source_end_ms: int = Field(gt=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)
    loop: Literal[False] = False

    @model_validator(mode="after")
    def require_music_source(self) -> "EditingMusicBed":
        if self.asset.kind != "music":
            raise ValueError("music bed requires a music asset")
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("music source interval must be positive")
        if self.source_end_ms > self.asset.duration_ms:
            raise ValueError("music source interval exceeds the verified asset")
        return self


class EditingSfxCue(FrozenEditingPlanModel):
    asset: EditingAudioSnapshot
    source_start_ms: Literal[0] = 0
    source_end_ms: int = Field(gt=0, strict=True)
    timeline_start_ms: int = Field(ge=0, strict=True)
    timeline_end_ms: int = Field(gt=0, strict=True)
    gain_db: float = Field(ge=-60, le=12, strict=True)

    @model_validator(mode="after")
    def require_whole_sfx_asset(self) -> "EditingSfxCue":
        if self.asset.kind != "sfx":
            raise ValueError("SFX cue requires an SFX asset")
        if self.source_end_ms != self.asset.duration_ms:
            raise ValueError("SFX v1 must use the entire asset from its beginning")
        if self.timeline_end_ms - self.timeline_start_ms != self.asset.duration_ms:
            raise ValueError("SFX timeline duration must equal the verified asset")
        return self


class AudioMixPolicy(FrozenEditingPlanModel):
    integrated_loudness_lufs: Literal[-14.0] = -14.0
    true_peak_ceiling_dbtp: Literal[-1.0] = -1.0
    cue_gains_applied_before_normalization: Literal[True] = True
    automatic_ducking: Literal[False] = False
    silence_when_no_selected_audio: Literal[True] = True


class EditingOutputSpec(FrozenEditingPlanModel):
    width: Literal[720] = 720
    height: Literal[1280] = 1280
    aspect_ratio: Literal["9:16"] = "9:16"
    container: Literal["mp4"] = "mp4"
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    resize_policy: Literal["downscale_only_no_crop_no_stretch_no_upscale"] = (
        "downscale_only_no_crop_no_stretch_no_upscale"
    )


def _canonical_plan_payload(values: dict) -> bytes:
    payload = dict(values)
    payload.pop("id", None)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class EditingPlan(FrozenEditingPlanModel):
    """Complete immutable editing intent for every Storyboard shot and overlay."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    target_duration_ms: int = Field(gt=0, strict=True)
    segments: Tuple[EditingSegment, ...] = Field(min_length=1)
    overlays: Tuple[EditingOverlayCue, ...] = Field(default_factory=tuple)
    music: Optional[EditingMusicBed] = None
    sfx: Tuple[EditingSfxCue, ...] = Field(default_factory=tuple)
    source_video_audio: Literal["discard"] = "discard"
    audio_mix: AudioMixPolicy = Field(default_factory=AudioMixPolicy)
    output: EditingOutputSpec = Field(default_factory=EditingOutputSpec)

    @model_validator(mode="after")
    def require_complete_timeline_and_identity(self) -> "EditingPlan":
        expected_start = 0
        shot_ids = []
        for segment in self.segments:
            if segment.timeline_start_ms != expected_start:
                raise ValueError("editing segments must form a gap-free timeline")
            expected_start = segment.timeline_end_ms
            shot_ids.append(segment.shot_id)
        if expected_start != self.target_duration_ms:
            raise ValueError("editing timeline must exactly equal target duration")
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("editing segments must contain one segment per shot")

        segments_by_shot = {item.shot_id: item for item in self.segments}
        overlay_keys = set()
        for cue in self.overlays:
            key = (cue.shot_id, cue.overlay_index)
            if key in overlay_keys:
                raise ValueError("editing overlay identity must be unique")
            overlay_keys.add(key)
            segment = segments_by_shot.get(cue.shot_id)
            if segment is None:
                raise ValueError("editing overlay references an unknown shot")
            if not (
                segment.timeline_start_ms <= cue.timeline_start_ms
                and cue.timeline_end_ms <= segment.timeline_end_ms
            ):
                raise ValueError("editing overlay must fit inside its shot timeline")

        if self.music is not None:
            if self.music.source_end_ms - self.music.source_start_ms != self.target_duration_ms:
                raise ValueError("music span must exactly equal target duration")
        for cue in self.sfx:
            if cue.timeline_end_ms > self.target_duration_ms:
                raise ValueError("SFX cue exceeds target duration")

        expected_id = "editing-plan:" + hashlib.sha256(
            _canonical_plan_payload(self.model_dump(mode="json"))
        ).hexdigest()
        if self.id != expected_id:
            raise ValueError("editing plan ID must match canonical path-free content")
        return self


class EditingPlanError(ValueError):
    """Raised when current domain context cannot authorize a durable plan."""


class EditingPlanPersistenceError(EditingPlanError):
    """Raised when immutable plan persistence fails."""


def _milliseconds(seconds: object, label: str) -> int:
    try:
        value = Decimal(str(seconds)) * Decimal("1000")
        integral = value.to_integral_value()
    except (InvalidOperation, TypeError, ValueError) as error:
        raise EditingPlanError(f"{label} cannot be represented in milliseconds") from error
    if value != integral or integral <= 0:
        raise EditingPlanError(f"{label} must be an exact positive millisecond duration")
    return int(integral)


def _validated_sequence(model, values: Sequence, label: str):
    try:
        return tuple(
            model.model_validate(item.model_dump() if hasattr(item, "model_dump") else item)
            for item in values
        )
    except (TypeError, ValidationError) as error:
        raise EditingPlanError(f"{label} are structurally invalid: {error}") from error


def _audio_snapshot(asset: EditingAudioAsset) -> EditingAudioSnapshot:
    return EditingAudioSnapshot(
        asset_id=asset.id,
        kind=asset.kind,
        sha256=asset.sha256,
        duration_ms=asset.duration_ms,
    )


def _product_display_name(product: Product) -> str:
    brand = product.brand.strip()
    name = product.name.strip()
    folded_name = name.casefold()
    folded_brand = brand.casefold()
    if folded_name == folded_brand or folded_name.startswith(folded_brand + " "):
        return name
    return f"{brand} {name}"


def build_editing_plan(
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    source_handoffs: Sequence[EditingSourceHandoff],
    segment_selections: Sequence[EditingSegmentSelection],
    overlay_timings: Sequence[EditingOverlayTiming],
    *,
    audio_manifest: Optional[EditingAudioManifest] = None,
    music_selection: Optional[EditingMusicSelection] = None,
    sfx_selections: Sequence[EditingSfxSelection] = (),
) -> EditingPlan:
    """Validate current authorization context and create path-free editing intent."""

    try:
        current_product = Product.model_validate(product.model_dump())
        current_campaign = AdCampaign.model_validate(campaign.model_dump())
        current_storyboard = Storyboard.model_validate(storyboard.model_dump())
    except ValidationError as error:
        raise EditingPlanError(f"editing plan context is structurally invalid: {error}") from error

    if current_product.id != current_campaign.claim_selection.product_id:
        raise EditingPlanError("Product does not match Campaign Product binding")
    try:
        current_storyboard = validate_storyboard(current_storyboard, current_campaign)
    except StoryboardContextError as error:
        raise EditingPlanError(str(error)) from error

    target_duration_ms = current_campaign.target_duration_seconds * 1000
    shot_durations = [
        _milliseconds(shot.duration_seconds, f"shot {shot.id} duration")
        for shot in current_storyboard.shots
    ]
    if sum(shot_durations) != target_duration_ms:
        raise EditingPlanError("Storyboard timeline must exactly equal Campaign target")

    handoffs = _validated_sequence(EditingSourceHandoff, source_handoffs, "source handoffs")
    selections = _validated_sequence(EditingSegmentSelection, segment_selections, "segment selections")
    timings = _validated_sequence(EditingOverlayTiming, overlay_timings, "overlay timings")
    sfx_inputs = _validated_sequence(EditingSfxSelection, sfx_selections, "SFX selections")
    try:
        selected_music = (
            None
            if music_selection is None
            else EditingMusicSelection.model_validate(
                music_selection.model_dump() if hasattr(music_selection, "model_dump") else music_selection
            )
        )
    except ValidationError as error:
        raise EditingPlanError(f"music selection is structurally invalid: {error}") from error

    expected_shot_ids = [shot.id for shot in current_storyboard.shots]
    if [item.shot_id for item in handoffs] != expected_shot_ids:
        raise EditingPlanError("source handoffs must cover every Storyboard shot exactly in order")
    if [item.shot_id for item in selections] != expected_shot_ids:
        raise EditingPlanError("segment selections must cover every Storyboard shot exactly in order")

    segments = []
    timeline_start = 0
    asset_plan_ids = set()
    for shot, duration_ms, handoff, selection in zip(
        current_storyboard.shots, shot_durations, handoffs, selections
    ):
        if (
            handoff.product_id != current_product.id
            or handoff.campaign_id != current_campaign.id
            or handoff.storyboard_id != current_storyboard.id
        ):
            raise EditingPlanError(f"source handoff for {shot.id} has stale bindings")
        if handoff.intended_edit_duration_ms != duration_ms:
            raise EditingPlanError(f"source handoff for {shot.id} has stale duration intent")
        expected_overlays = tuple(item.model_dump() for item in shot.text_overlays)
        actual_overlays = tuple(item.model_dump() for item in handoff.text_overlays)
        if actual_overlays != expected_overlays:
            raise EditingPlanError(f"source handoff for {shot.id} has stale overlays")
        if handoff.provider_audio_disposition != "discard_before_editing":
            raise EditingPlanError("all source provider audio must be discarded")
        asset_plan_ids.add(handoff.asset_plan_id)
        source_end = selection.source_start_ms + duration_ms
        snapshot = EditingSourceSnapshot(
            shot_id=shot.id,
            asset_plan_id=handoff.asset_plan_id,
            video_artifact_id=handoff.video_artifact_id,
            video_artifact_sha256=handoff.video_artifact_sha256,
            authorizing_video_review_context_id=handoff.authorizing_video_review_context_id,
            authorizing_video_review_id=handoff.authorizing_video_review_id,
            approved_usable_ranges=tuple(item.model_copy(deep=True) for item in handoff.approved_usable_ranges),
        )
        try:
            segment = EditingSegment(
                shot_id=shot.id,
                source=snapshot,
                source_start_ms=selection.source_start_ms,
                source_end_ms=source_end,
                timeline_start_ms=timeline_start,
                timeline_end_ms=timeline_start + duration_ms,
            )
        except ValidationError as error:
            raise EditingPlanError(f"invalid source segment for {shot.id}: {error}") from error
        segments.append(segment)
        timeline_start += duration_ms
    if len(asset_plan_ids) != 1:
        raise EditingPlanError("all source handoffs must share one current asset plan")

    timing_by_key = {}
    for timing in timings:
        key = (timing.shot_id, timing.overlay_index)
        if key in timing_by_key:
            raise EditingPlanError(f"duplicate overlay timing: {key}")
        timing_by_key[key] = timing
    expected_overlay_keys = [
        (shot.id, index)
        for shot in current_storyboard.shots
        for index, _ in enumerate(shot.text_overlays)
    ]
    if set(timing_by_key) != set(expected_overlay_keys) or len(timing_by_key) != len(expected_overlay_keys):
        raise EditingPlanError("overlay timings must cover every Storyboard overlay exactly once")

    snapshots_by_claim = {item.claim_id: item for item in current_campaign.claim_snapshots}
    overlays = []
    for shot in current_storyboard.shots:
        for overlay_index, overlay in enumerate(shot.text_overlays):
            timing = timing_by_key[(shot.id, overlay_index)]
            if overlay.kind == "claim":
                snapshot = snapshots_by_claim.get(overlay.claim_id)
                if snapshot is None:
                    raise EditingPlanError(f"claim overlay has no Campaign snapshot: {overlay.claim_id}")
                resolved_text = snapshot.display_text
            elif overlay.kind == "product_name":
                resolved_text = _product_display_name(current_product)
            else:
                resolved_text = overlay.text
            overlays.append(
                EditingOverlayCue(
                    shot_id=shot.id,
                    overlay_index=overlay_index,
                    kind=overlay.kind,
                    claim_id=overlay.claim_id,
                    resolved_text=resolved_text,
                    timeline_start_ms=timing.timeline_start_ms,
                    timeline_end_ms=timing.timeline_end_ms,
                    placement=timing.placement,
                )
            )

    if (selected_music is not None or sfx_inputs) and audio_manifest is None:
        raise EditingPlanError("selected audio requires the current verified audio manifest")
    current_manifest = None
    if audio_manifest is not None:
        try:
            current_manifest = EditingAudioManifest.model_validate(audio_manifest.model_dump())
        except ValidationError as error:
            raise EditingPlanError(f"audio manifest is structurally invalid: {error}") from error
        if current_manifest.product_id != current_product.id:
            raise EditingPlanError("audio manifest does not match Product")

    music = None
    if selected_music is not None:
        try:
            asset = resolve_verified_editing_audio_asset(current_manifest, selected_music.asset_id, expected_kind="music")
        except EditingAudioManifestError as error:
            raise EditingPlanError(str(error)) from error
        try:
            music = EditingMusicBed(
                asset=_audio_snapshot(asset),
                source_start_ms=selected_music.source_start_ms,
                source_end_ms=selected_music.source_start_ms + target_duration_ms,
                gain_db=selected_music.gain_db,
            )
        except ValidationError as error:
            raise EditingPlanError(f"invalid music selection: {error}") from error

    sfx = []
    for selected_sfx in sfx_inputs:
        try:
            asset = resolve_verified_editing_audio_asset(current_manifest, selected_sfx.asset_id, expected_kind="sfx")
        except EditingAudioManifestError as error:
            raise EditingPlanError(str(error)) from error
        try:
            sfx.append(
                EditingSfxCue(
                    asset=_audio_snapshot(asset),
                    source_end_ms=asset.duration_ms,
                    timeline_start_ms=selected_sfx.timeline_start_ms,
                    timeline_end_ms=selected_sfx.timeline_start_ms + asset.duration_ms,
                    gain_db=selected_sfx.gain_db,
                )
            )
        except ValidationError as error:
            raise EditingPlanError(f"invalid SFX selection: {error}") from error

    values = {
        "schema_version": 1,
        "product_id": current_product.id,
        "campaign_id": current_campaign.id,
        "storyboard_id": current_storyboard.id,
        "target_duration_ms": target_duration_ms,
        "segments": tuple(segments),
        "overlays": tuple(overlays),
        "music": music,
        "sfx": tuple(sfx),
        "source_video_audio": "discard",
        "audio_mix": AudioMixPolicy(),
        "output": EditingOutputSpec(),
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
    plan_id = "editing-plan:" + hashlib.sha256(_canonical_plan_payload(json_values)).hexdigest()
    try:
        return EditingPlan(id=plan_id, **values)
    except ValidationError as error:
        raise EditingPlanError(f"editing plan could not be created: {error}") from error


def editing_plan_path(
    plan: EditingPlan,
    storage_root: Union[str, Path] = "products",
) -> Path:
    try:
        validated = EditingPlan.model_validate(plan.model_dump())
    except ValidationError as error:
        raise EditingPlanPersistenceError(f"editing plan is invalid: {error}") from error
    fingerprint = validated.id.removeprefix("editing-plan:")
    return (
        Path(storage_root)
        / product_asset_directory_name(validated.product_id)
        / "assets"
        / "editing"
        / "plans"
        / f"{fingerprint}.json"
    )


def save_editing_plan(
    plan: EditingPlan,
    storage_root: Union[str, Path] = "products",
) -> Path:
    """Persist an immutable human-readable plan without overwriting intent."""

    try:
        validated = EditingPlan.model_validate(plan.model_dump())
    except ValidationError as error:
        raise EditingPlanPersistenceError(f"editing plan is invalid: {error}") from error
    root = Path(storage_root)
    target = editing_plan_path(validated, root)
    directory = target.parent
    temporary_path = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        directory.resolve().relative_to(root.resolve())
        if target.is_symlink():
            raise EditingPlanPersistenceError("editing plan path must not be a symbolic link")
        if target.exists():
            existing = load_editing_plan(target)
            if existing != validated:
                raise EditingPlanPersistenceError("immutable editing plan path contains different intent")
            return target.resolve()
        json_text = validated.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".editing-plan-", suffix=".tmp", dir=directory, delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(json_text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(str(temporary_path), str(target))
        temporary_path = None
        return target.resolve()
    except EditingPlanPersistenceError:
        raise
    except (OSError, ValueError) as error:
        raise EditingPlanPersistenceError(f"editing plan could not be saved: {target}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_editing_plan(path: Union[str, Path]) -> EditingPlan:
    """Load and strictly validate one immutable editing plan JSON file."""

    input_path = Path(path)
    try:
        if input_path.is_symlink():
            raise EditingPlanPersistenceError("editing plan path must not be a symbolic link")
        return EditingPlan.model_validate_json(input_path.read_text(encoding="utf-8"))
    except EditingPlanPersistenceError:
        raise
    except (OSError, UnicodeError, ValidationError) as error:
        raise EditingPlanPersistenceError(f"editing plan could not be loaded: {input_path}") from error
