"""Verified local MP3/WAV assets for deterministic final-ad editing."""

import hashlib
import json
import os
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Literal, Optional, Tuple, Union

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from product import NonEmptyStr, ProductModel
from product_image_downloads import Sha256Hex, product_asset_directory_name


EditingAudioKind = Literal["music", "sfx"]
EditingAudioContainer = Literal["mp3", "wav"]


class FrozenEditingAudioModel(ProductModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )


class EditingAudioAsset(FrozenEditingAudioModel):
    """One product-scoped content-addressed and technically verified asset."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    product_id: NonEmptyStr
    kind: EditingAudioKind
    sha256: Sha256Hex
    byte_size: int = Field(gt=0, strict=True)
    container: EditingAudioContainer
    codec: NonEmptyStr
    duration_ms: int = Field(gt=0, strict=True)
    channels: int = Field(gt=0, strict=True)
    sample_rate_hz: int = Field(gt=0, strict=True)
    source_label: NonEmptyStr
    local_path: NonEmptyStr

    @model_validator(mode="after")
    def require_content_addressed_identity(self) -> "EditingAudioAsset":
        if self.id != f"editing-audio:{self.kind}:{self.sha256}":
            raise ValueError("editing audio ID must contain kind and SHA-256")
        if self.container == "mp3" and self.codec != "mp3":
            raise ValueError("MP3 assets require the MP3 codec")
        if self.container == "wav" and not self.codec.startswith("pcm_"):
            raise ValueError("WAV assets require an uncompressed PCM codec")
        return self


class EditingAudioManifest(FrozenEditingAudioModel):
    """Product-scoped runtime catalog; EditingPlan never copies its paths."""

    schema_version: Literal[1] = 1
    product_id: NonEmptyStr
    assets: Tuple[EditingAudioAsset, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def require_consistent_assets(self) -> "EditingAudioManifest":
        ids = set()
        path_by_sha = {}
        sha_by_path = {}
        for asset in self.assets:
            if asset.product_id != self.product_id:
                raise ValueError(
                    f"editing audio asset {asset.id} belongs to another Product"
                )
            if asset.id in ids:
                raise ValueError(f"duplicate editing audio asset ID: {asset.id}")
            ids.add(asset.id)
            existing_path = path_by_sha.get(asset.sha256)
            if existing_path is not None and existing_path != asset.local_path:
                raise ValueError("same audio SHA must resolve to the same local path")
            path_by_sha[asset.sha256] = asset.local_path
            existing_sha = sha_by_path.get(asset.local_path)
            if existing_sha is not None and existing_sha != asset.sha256:
                raise ValueError("one audio path cannot represent different bytes")
            sha_by_path[asset.local_path] = asset.sha256
        return self


class EditingAudioAssetError(ValueError):
    """Raised when local audio cannot be admitted or reverified."""


class EditingAudioManifestError(ValueError):
    """Raised when catalog binding, resolution, or persistence fails."""


def _run_media_command(command: list[str], message: str) -> bytes:
    try:
        completed = subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise EditingAudioAssetError(message) from error
    return completed.stdout


def _duration_ms(raw_duration: object) -> int:
    try:
        value = Decimal(str(raw_duration)) * Decimal("1000")
        milliseconds = int(value.to_integral_value(rounding=ROUND_FLOOR))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise EditingAudioAssetError(
            "audio duration could not be represented in milliseconds"
        ) from error
    if milliseconds <= 0:
        raise EditingAudioAssetError("audio duration must be positive")
    return milliseconds


def _inspect_audio_bytes(data: bytes) -> dict:
    if not isinstance(data, bytes) or not data:
        raise EditingAudioAssetError("editing audio source must contain bytes")
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".editing-audio-probe-",
            suffix=".bin",
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
            "editing audio source is not inspectable",
        )
        _run_media_command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(temporary_path),
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            "editing audio stream is not decodable",
        )
        try:
            metadata = json.loads(probe_output.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise EditingAudioAssetError(
                "ffprobe returned malformed editing audio metadata"
            ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    streams = metadata.get("streams")
    if not isinstance(streams, list):
        raise EditingAudioAssetError("editing audio source has no stream list")
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    video_streams = [
        stream for stream in streams if stream.get("codec_type") == "video"
    ]
    if len(audio_streams) != 1 or video_streams:
        raise EditingAudioAssetError(
            "editing audio source must contain one audio stream and no video"
        )
    stream = audio_streams[0]
    format_data = metadata.get("format") or {}
    format_name = format_data.get("format_name")
    if format_name not in ("mp3", "wav"):
        raise EditingAudioAssetError("editing audio format must be MP3 or WAV")
    codec = stream.get("codec_name")
    if not isinstance(codec, str) or not codec.strip():
        raise EditingAudioAssetError("editing audio codec is missing")
    if format_name == "mp3" and codec != "mp3":
        raise EditingAudioAssetError("MP3 container must contain MP3 audio")
    if format_name == "wav" and not codec.startswith("pcm_"):
        raise EditingAudioAssetError("WAV container must contain PCM audio")
    channels = stream.get("channels")
    if type(channels) is not int or channels <= 0:
        raise EditingAudioAssetError("editing audio channels must be positive")
    try:
        sample_rate_hz = int(stream["sample_rate"])
    except (KeyError, TypeError, ValueError) as error:
        raise EditingAudioAssetError(
            "editing audio sample rate could not be identified"
        ) from error
    if sample_rate_hz <= 0:
        raise EditingAudioAssetError("editing audio sample rate must be positive")
    return {
        "container": format_name,
        "codec": codec,
        "duration_ms": _duration_ms(format_data.get("duration")),
        "channels": channels,
        "sample_rate_hz": sample_rate_hz,
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stored_file:
        for chunk in iter(lambda: stored_file.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_exact_audio(
    data: bytes,
    *,
    product_id: str,
    sha256: str,
    extension: str,
    storage_root: Union[str, Path],
) -> Path:
    root = Path(storage_root)
    directory = (
        root
        / product_asset_directory_name(product_id)
        / "assets"
        / "editing"
        / "audio"
    )
    target = directory / f"{sha256}.{extension}"
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise EditingAudioAssetError(
                "editing audio directory resolves outside its storage root"
            ) from error
        if target.is_symlink():
            raise EditingAudioAssetError(
                "canonical editing audio path must not be a symbolic link"
            )
        if target.exists():
            if not target.is_file() or _hash_file(target) != sha256:
                raise EditingAudioAssetError(
                    "existing editing audio failed its SHA-256 check"
                )
            return target.resolve()
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".editing-audio-",
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
            if (
                target.is_symlink()
                or not target.is_file()
                or _hash_file(target) != sha256
            ):
                raise EditingAudioAssetError(
                    "concurrent editing audio failed its SHA-256 check"
                )
        return target.resolve()
    except EditingAudioAssetError:
        raise
    except OSError as error:
        raise EditingAudioAssetError(
            f"editing audio could not be stored: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def ingest_editing_audio_asset(
    *,
    product_id: str,
    kind: EditingAudioKind,
    source_path: Union[str, Path],
    source_label: str,
    storage_root: Union[str, Path] = "products",
) -> EditingAudioAsset:
    """Ingest one explicit local MP3/WAV without modifying the source file."""

    if kind not in ("music", "sfx"):
        raise EditingAudioAssetError("editing audio kind must be music or sfx")
    try:
        identity = TypeAdapter(NonEmptyStr).validate_python(product_id)
        validated_source_label = TypeAdapter(NonEmptyStr).validate_python(
            source_label
        )
    except ValidationError as error:
        raise EditingAudioAssetError(
            f"editing audio identity metadata is invalid: {error}"
        ) from error
    try:
        data = Path(source_path).read_bytes()
    except OSError as error:
        raise EditingAudioAssetError(
            f"editing audio source could not be read: {source_path}"
        ) from error
    metadata = _inspect_audio_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    stored_path = _store_exact_audio(
        data,
        product_id=identity,
        sha256=sha256,
        extension=metadata["container"],
        storage_root=storage_root,
    )
    try:
        return EditingAudioAsset(
            id=f"editing-audio:{kind}:{sha256}",
            product_id=identity,
            kind=kind,
            sha256=sha256,
            byte_size=len(data),
            source_label=validated_source_label,
            local_path=str(stored_path),
            **metadata,
        )
    except ValidationError as error:
        raise EditingAudioAssetError(
            f"editing audio metadata is invalid: {error}"
        ) from error


def read_verified_editing_audio_bytes(asset: EditingAudioAsset) -> bytes:
    """Re-read exact stored bytes and compare all inspected metadata."""

    try:
        validated = EditingAudioAsset.model_validate(asset.model_dump())
    except ValidationError as error:
        raise EditingAudioAssetError(
            f"editing audio asset must be structurally valid: {error}"
        ) from error
    try:
        data = Path(validated.local_path).read_bytes()
    except OSError as error:
        raise EditingAudioAssetError(
            f"editing audio asset could not be read: {validated.id}"
        ) from error
    if len(data) != validated.byte_size:
        raise EditingAudioAssetError(
            f"editing audio byte size changed: {validated.id}"
        )
    if hashlib.sha256(data).hexdigest() != validated.sha256:
        raise EditingAudioAssetError(
            f"editing audio SHA-256 changed: {validated.id}"
        )
    metadata = _inspect_audio_bytes(data)
    for field_name, current_value in metadata.items():
        if getattr(validated, field_name) != current_value:
            raise EditingAudioAssetError(
                f"editing audio {field_name} changed: {validated.id}"
            )
    return data


def add_editing_audio_asset(
    manifest: EditingAudioManifest,
    asset: EditingAudioAsset,
) -> EditingAudioManifest:
    """Return an immutable catalog extension after strict Product binding."""

    try:
        validated_manifest = EditingAudioManifest.model_validate(
            manifest.model_dump()
        )
        validated_asset = EditingAudioAsset.model_validate(asset.model_dump())
    except ValidationError as error:
        raise EditingAudioManifestError(
            f"editing audio manifest input is invalid: {error}"
        ) from error
    if validated_asset.product_id != validated_manifest.product_id:
        raise EditingAudioManifestError(
            "editing audio asset does not belong to manifest Product"
        )
    if any(item.id == validated_asset.id for item in validated_manifest.assets):
        raise EditingAudioManifestError(
            f"duplicate editing audio asset ID: {validated_asset.id}"
        )
    return EditingAudioManifest(
        product_id=validated_manifest.product_id,
        assets=(
            *(item.model_copy(deep=True) for item in validated_manifest.assets),
            validated_asset.model_copy(deep=True),
        ),
    )


def resolve_verified_editing_audio_asset(
    manifest: EditingAudioManifest,
    asset_id: str,
    *,
    expected_kind: Optional[EditingAudioKind] = None,
) -> EditingAudioAsset:
    """Resolve one exact catalog item and then verify only its current bytes."""

    try:
        validated = EditingAudioManifest.model_validate(manifest.model_dump())
    except ValidationError as error:
        raise EditingAudioManifestError(
            f"editing audio manifest is invalid: {error}"
        ) from error
    matches = [item for item in validated.assets if item.id == asset_id]
    if len(matches) != 1:
        raise EditingAudioManifestError(
            f"editing audio asset must resolve exactly once: {asset_id}"
        )
    asset = matches[0]
    if expected_kind is not None and asset.kind != expected_kind:
        raise EditingAudioManifestError(
            f"editing audio asset {asset.id} is not {expected_kind}"
        )
    try:
        read_verified_editing_audio_bytes(asset)
    except EditingAudioAssetError as error:
        raise EditingAudioManifestError(
            f"editing audio asset is stale: {asset.id}"
        ) from error
    return asset.model_copy(deep=True)


def editing_audio_manifest_path(
    manifest: EditingAudioManifest,
    storage_root: Union[str, Path] = "products",
) -> Path:
    try:
        validated = EditingAudioManifest.model_validate(manifest.model_dump())
    except ValidationError as error:
        raise EditingAudioManifestError(
            f"editing audio manifest is invalid: {error}"
        ) from error
    return (
        Path(storage_root)
        / product_asset_directory_name(validated.product_id)
        / "assets"
        / "editing"
        / "audio"
        / "manifest.json"
    )


def save_editing_audio_manifest(
    manifest: EditingAudioManifest,
    storage_root: Union[str, Path] = "products",
) -> Path:
    """Atomically save one human-readable product-scoped runtime catalog."""

    try:
        validated = EditingAudioManifest.model_validate(manifest.model_dump())
    except ValidationError as error:
        raise EditingAudioManifestError(
            f"editing audio manifest is invalid: {error}"
        ) from error
    root = Path(storage_root)
    target = editing_audio_manifest_path(validated, root)
    directory = target.parent
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise EditingAudioManifestError(
                "editing audio manifest resolves outside storage root"
            ) from error
        if target.is_symlink():
            raise EditingAudioManifestError(
                "editing audio manifest path must not be a symbolic link"
            )
        json_text = validated.model_dump_json(
            indent=2,
            ensure_ascii=False,
            exclude_none=True,
        ) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".editing-audio-manifest-",
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
    except EditingAudioManifestError:
        raise
    except OSError as error:
        raise EditingAudioManifestError(
            f"editing audio manifest could not be saved: {target}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_editing_audio_manifest(
    product_id: str,
    storage_root: Union[str, Path] = "products",
) -> EditingAudioManifest:
    """Load one strict catalog or return a new empty product catalog."""

    try:
        empty = EditingAudioManifest(product_id=product_id)
    except ValidationError as error:
        raise EditingAudioManifestError(f"invalid Product identity: {error}") from error
    path = editing_audio_manifest_path(empty, storage_root)
    if not path.exists():
        return empty
    try:
        if path.is_symlink():
            raise EditingAudioManifestError(
                "editing audio manifest path must not be a symbolic link"
            )
        loaded = EditingAudioManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except EditingAudioManifestError:
        raise
    except (OSError, UnicodeError, ValidationError) as error:
        raise EditingAudioManifestError(
            f"editing audio manifest could not be loaded: {path}"
        ) from error
    if loaded.product_id != empty.product_id:
        raise EditingAudioManifestError(
            "editing audio manifest Product does not match requested Product"
        )
    return loaded
