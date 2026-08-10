"""Local FFmpeg adapter for exact approved final-ad editing instructions."""

import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont
from pydantic import ConfigDict

from campaign import AdCampaign
from editing_artifact import (
    EditingArtifact,
    EditingArtifactError,
    EditingReceipt,
    admit_editing_artifact,
)
from editing_audio_assets import (
    EditingAudioManifest,
    EditingAudioManifestError,
    resolve_verified_editing_audio_asset,
)
from editing_instruction import (
    EditingInstruction,
    EditingInstructionError,
    compile_editing_instruction,
)
from editing_plan import EditingPlan
from product import Product, ProductModel
from storyboard import Storyboard
from video_shot_artifact import (
    VideoShotArtifact,
    VideoShotArtifactValidationError,
    read_verified_video_shot_bytes,
)
from video_shot_review import VideoShotReviewHistory


class EditingRenderResult(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    artifact: EditingArtifact
    receipt: EditingReceipt
    instruction: EditingInstruction


class FFmpegEditingError(RuntimeError):
    """Raised when a local final-ad render cannot complete safely."""


def _seconds(milliseconds: int) -> str:
    whole, remainder = divmod(milliseconds, 1000)
    return str(whole) if remainder == 0 else f"{whole}.{remainder:03d}".rstrip("0")


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int):
    lines = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            width = draw.textbbox((0, 0), candidate, font=font, stroke_width=3)[2]
            if width <= max_width:
                current = candidate
            elif current:
                lines.append(current)
                current = word
            else:
                raise FFmpegEditingError("overlay text cannot fit social_bold_v1 without rewriting")
        if current:
            lines.append(current)
    return lines


def _render_overlay_png(cue, font_path: Path, path: Path) -> None:
    image = Image.new("RGBA", (720, 1280), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(str(font_path), 58)
    except (OSError, ValueError) as error:
        raise FFmpegEditingError("social_bold_v1 runtime font could not be loaded") from error
    lines = _wrap_text(draw, cue.resolved_text, font, 620)
    line_height = 76
    block_height = line_height * len(lines)
    if cue.placement == "top_center":
        center_y = 130 + block_height // 2
    elif cue.placement == "center":
        center_y = 640
    else:
        center_y = 1150 - block_height // 2
    first_y = center_y - block_height // 2 + line_height // 2
    for index, line in enumerate(lines):
        draw.text(
            (360, first_y + index * line_height),
            line,
            font=font,
            anchor="mm",
            fill=(255, 255, 255, 255),
            stroke_width=4,
            stroke_fill=(0, 0, 0, 230),
        )
    try:
        image.save(path, format="PNG", optimize=False)
    except OSError as error:
        raise FFmpegEditingError("overlay raster could not be created") from error


def _tool_version() -> Optional[str]:
    try:
        line = subprocess.run(
            ["ffmpeg", "-version"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        return None
    return line[:300] or None


def _reverify_immediately_before_render(
    instruction: EditingInstruction,
    artifacts: Sequence[VideoShotArtifact],
    audio_manifest: Optional[EditingAudioManifest],
) -> None:
    artifacts_by_id = {item.id: item for item in artifacts}
    for segment in instruction.video_segments:
        artifact = artifacts_by_id.get(segment.artifact_id)
        if artifact is None:
            raise FFmpegEditingError(f"current source artifact disappeared for shot {segment.shot_id}")
        try:
            read_verified_video_shot_bytes(artifact)
        except VideoShotArtifactValidationError as error:
            raise FFmpegEditingError(f"source changed immediately before render: {segment.shot_id}") from error
    try:
        if instruction.music is not None:
            resolve_verified_editing_audio_asset(
                audio_manifest, instruction.music.asset_id, expected_kind="music"
            )
        for cue in instruction.sfx:
            resolve_verified_editing_audio_asset(
                audio_manifest, cue.asset_id, expected_kind="sfx"
            )
    except EditingAudioManifestError as error:
        raise FFmpegEditingError("selected audio changed immediately before render") from error


def _build_ffmpeg_command(
    instruction: EditingInstruction,
    overlay_paths: Sequence[Path],
    output_path: Path,
) -> list[str]:
    command = ["ffmpeg", "-v", "error", "-y"]
    for segment in instruction.video_segments:
        command.extend(["-i", str(segment.local_path)])
    for overlay_path in overlay_paths:
        command.extend(["-i", str(overlay_path)])

    music_input = None
    if instruction.music is not None:
        music_input = len(instruction.video_segments) + len(overlay_paths)
        command.extend(["-i", str(instruction.music.local_path)])
    sfx_inputs = []
    for cue in instruction.sfx:
        sfx_inputs.append(len(instruction.video_segments) + len(overlay_paths) + (1 if music_input is not None else 0) + len(sfx_inputs))
        command.extend(["-i", str(cue.local_path)])
    silence_input = None
    if instruction.music is None and not instruction.sfx:
        silence_input = len(instruction.video_segments) + len(overlay_paths)
        command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])

    filters = []
    video_labels = []
    for index, segment in enumerate(instruction.video_segments):
        operations = [
            f"trim=start={_seconds(segment.source_start_ms)}:end={_seconds(segment.source_end_ms)}",
            "setpts=PTS-STARTPTS",
        ]
        if segment.resize_mode == "downscale":
            operations.append("scale=720:1280:flags=lanczos")
        operations.append("format=yuv420p")
        label = f"v{index}"
        filters.append(f"[{index}:v]{','.join(operations)}[{label}]")
        video_labels.append(f"[{label}]")
    filters.append(
        "".join(video_labels)
        + f"concat=n={len(video_labels)}:v=1:a=0[vcat]"
    )
    current_video = "vcat"
    overlay_input_start = len(instruction.video_segments)
    for index, (cue, _) in enumerate(zip(instruction.overlays, overlay_paths)):
        overlay_label = f"overlay{index}"
        next_video = f"vo{index}"
        filters.append(f"[{overlay_input_start + index}:v]format=rgba[{overlay_label}]")
        filters.append(
            f"[{current_video}][{overlay_label}]overlay=0:0:eof_action=repeat:repeatlast=1:"
            f"enable='between(t,{_seconds(cue.timeline_start_ms)},{_seconds(cue.timeline_end_ms)})'[{next_video}]"
        )
        current_video = next_video
    filters.append(f"[{current_video}]trim=duration={_seconds(instruction.target_duration_ms)},setpts=PTS-STARTPTS[vout]")

    audio_labels = []
    if instruction.music is not None:
        filters.append(
            f"[{music_input}:a]atrim=start={_seconds(instruction.music.source_start_ms)}:"
            f"end={_seconds(instruction.music.source_end_ms)},asetpts=PTS-STARTPTS,"
            f"volume={instruction.music.gain_db}dB[amusic]"
        )
        audio_labels.append("[amusic]")
    for index, (cue, input_index) in enumerate(zip(instruction.sfx, sfx_inputs)):
        label = f"asfx{index}"
        filters.append(
            f"[{input_index}:a]atrim=start=0:end={_seconds(cue.source_end_ms)},"
            f"asetpts=PTS-STARTPTS,volume={cue.gain_db}dB,"
            f"adelay={cue.timeline_start_ms}:all=1[{label}]"
        )
        audio_labels.append(f"[{label}]")
    if silence_input is not None:
        filters.append(
            f"[{silence_input}:a]atrim=duration={_seconds(instruction.target_duration_ms)},"
            "asetpts=PTS-STARTPTS[aout]"
        )
    else:
        if len(audio_labels) == 1:
            mixed = audio_labels[0]
        else:
            filters.append(
                "".join(audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0[amixed]"
            )
            mixed = "[amixed]"
        filters.append(
            f"{mixed}apad=whole_dur={_seconds(instruction.target_duration_ms)},"
            f"atrim=duration={_seconds(instruction.target_duration_ms)},"
            "loudnorm=I=-14:TP=-1:LRA=11,aresample=48000[aout]"
        )

    command.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-t", _seconds(instruction.target_duration_ms),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
            "-map_metadata", "-1", str(output_path),
        ]
    )
    return command


def render_editing_plan(
    plan: EditingPlan,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    video_artifacts: Sequence[VideoShotArtifact],
    video_review_histories: Sequence[VideoShotReviewHistory],
    *,
    audio_manifest: Optional[EditingAudioManifest],
    storage_root: Path = Path("products"),
    font_candidates: Optional[Sequence[Path]] = None,
    created_at: Optional[datetime] = None,
) -> EditingRenderResult:
    """Render one canonical local edit after rebuilding every current approval."""

    started_at = datetime.now(timezone.utc)
    try:
        instruction = compile_editing_instruction(
            plan=plan,
            product=product,
            campaign=campaign,
            storyboard=storyboard,
            video_artifacts=video_artifacts,
            video_review_histories=video_review_histories,
            audio_manifest=audio_manifest,
            font_candidates=font_candidates,
        )
    except EditingInstructionError as error:
        raise FFmpegEditingError(str(error)) from error
    _reverify_immediately_before_render(instruction, video_artifacts, audio_manifest)

    with tempfile.TemporaryDirectory(prefix="final-ad-edit-") as directory:
        temporary_directory = Path(directory)
        overlay_paths = []
        for index, cue in enumerate(instruction.overlays):
            overlay_path = temporary_directory / f"overlay-{index}.png"
            _render_overlay_png(cue, instruction.runtime_font_path, overlay_path)
            overlay_paths.append(overlay_path)
        output_path = temporary_directory / "rendered.mp4"
        command = _build_ffmpeg_command(instruction, overlay_paths, output_path)
        try:
            subprocess.run(command, check=True, capture_output=True)
        except OSError as error:
            raise FFmpegEditingError("FFmpeg is unavailable for final-ad editing") from error
        except subprocess.CalledProcessError as error:
            raise FFmpegEditingError("FFmpeg could not render the approved Editing Plan") from error
        try:
            artifact = admit_editing_artifact(
                output_path,
                plan,
                storage_root,
                created_at=created_at,
            )
        except EditingArtifactError as error:
            raise FFmpegEditingError(str(error)) from error

    completed_at = datetime.now(timezone.utc)
    receipt = EditingReceipt(
        attempt_id=f"editing-attempt:{uuid.uuid4()}",
        editing_plan_id=plan.id,
        artifact_id=artifact.id,
        tool="ffmpeg",
        tool_version=_tool_version(),
        started_at=started_at,
        completed_at=completed_at,
        source_video_audio_discarded=True,
    )
    return EditingRenderResult(
        artifact=artifact,
        receipt=receipt,
        instruction=instruction,
    )
