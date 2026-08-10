"""Controlled downloading and verification of researched product images."""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union
from urllib.parse import urlsplit

import requests
from PIL import Image, UnidentifiedImageError
from pydantic import Field, StringConstraints, field_validator

from product import NonEmptyStr, ProductImage, ProductModel
from product_identity import slugify_identifier


MAX_IMAGE_BYTES = 20 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 64 * 1024
ACCEPTED_SOURCE_TYPES = frozenset(
    {
        "json_ld_product_image",
        "json_ld_product_image_object",
        "open_graph_product_image",
    }
)
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "VideoGeneratorProductAssetDownloader/1.0 "
        "(+https://github.com/P0RTNOY/VideoGenerator)"
    ),
    "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.5",
}

Sha256Hex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
ImageFormat = Literal["JPEG", "PNG", "WEBP"]
ImageMimeType = Literal["image/jpeg", "image/png", "image/webp"]


class DownloadedProductImage(ProductModel):
    """Verified local original linked to one researched ProductImage."""

    product_id: NonEmptyStr
    source_image_id: NonEmptyStr
    source_url: NonEmptyStr
    final_url: NonEmptyStr
    local_path: NonEmptyStr
    sha256: Sha256Hex
    byte_size: int = Field(gt=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    format: ImageFormat
    mime_type: ImageMimeType
    downloaded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("downloaded_at")
    @classmethod
    def require_utc_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("downloaded_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class ProductImageDownloadError(RuntimeError):
    """Base error for controlled ProductImage downloads."""


class InvalidImageUrlError(ProductImageDownloadError):
    """Raised when a ProductImage has no safe absolute HTTP(S) URL."""


class UnsupportedProductImageSourceError(ProductImageDownloadError):
    """Raised when a ProductImage did not come from an accepted source."""


class ImageHttpError(ProductImageDownloadError):
    """Raised when the remote image request fails."""


class ImageTimeoutError(ImageHttpError):
    """Raised when the remote image request times out."""


class ImageContentTypeError(ProductImageDownloadError):
    """Raised when a response does not declare an image media type."""


class ImageResponseTooLargeError(ProductImageDownloadError):
    """Raised when declared or streamed bytes exceed the configured limit."""


class CorruptImageError(ProductImageDownloadError):
    """Raised when Pillow cannot fully verify and decode image bytes."""


class UnsupportedImageFormatError(ProductImageDownloadError):
    """Raised when decoded bytes are not JPEG, PNG, or WebP."""


class ImageFilesystemError(ProductImageDownloadError):
    """Raised when a verified original cannot be stored safely."""


class ImageIntegrityError(ProductImageDownloadError):
    """Raised when a stored original no longer matches its asset metadata."""


def _validate_image_url(value: Optional[str], label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidImageUrlError(f"{label} must be an absolute HTTP(S) URL.")
    cleaned = value.strip()
    parts = urlsplit(cleaned)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise InvalidImageUrlError(f"{label} must be an absolute HTTP(S) URL.")
    if parts.username is not None or parts.password is not None:
        raise InvalidImageUrlError(f"{label} must not contain credentials.")
    return cleaned


def _validate_source(image: ProductImage) -> str:
    if image.source_type not in ACCEPTED_SOURCE_TYPES:
        displayed_source = image.source_type or "missing source_type"
        raise UnsupportedProductImageSourceError(
            f"ProductImage source is not approved for downloading: "
            f"{displayed_source}."
        )
    return _validate_image_url(image.url, "ProductImage.url")


def _read_bounded_response(response: Any, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise ImageResponseTooLargeError(
                f"Image response exceeds the {max_bytes}-byte limit."
            )

    data = bytearray()
    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
        if not chunk:
            continue
        if len(data) + len(chunk) > max_bytes:
            raise ImageResponseTooLargeError(
                f"Image response exceeds the {max_bytes}-byte limit."
            )
        data.extend(chunk)
    return bytes(data)


def _verified_image_properties(data: bytes) -> tuple:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as candidate:
                image_format = candidate.format
                width, height = candidate.size
                candidate.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        raise CorruptImageError(
            "Downloaded bytes are not a valid fully decodable image."
        ) from error

    format_details = {
        "JPEG": ("image/jpeg", "jpg"),
        "PNG": ("image/png", "png"),
        "WEBP": ("image/webp", "webp"),
    }
    if image_format not in format_details:
        displayed_format = image_format or "unknown"
        raise UnsupportedImageFormatError(
            f"Decoded image format is not supported: {displayed_format}."
        )
    mime_type, extension = format_details[image_format]
    return width, height, image_format, mime_type, extension


def product_asset_directory_name(product_id: str) -> str:
    """Return the deterministic safe directory name used for Product assets."""

    cleaned = product_id.strip()
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", cleaned):
        return cleaned
    slug = slugify_identifier(cleaned)[:96]
    if not slug:
        raise ImageFilesystemError(
            "Product ID cannot be represented as a safe asset directory."
        )
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as existing_file:
        for chunk in iter(lambda: existing_file.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_original(
    data: bytes,
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
        / "originals"
    )
    target = directory / f"{sha256}.{extension}"
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise ImageFilesystemError(
                "Product asset directory resolves outside its storage root."
            ) from error
        if target.is_symlink():
            raise ImageFilesystemError(
                "Canonical image path must not be a symbolic link."
            )
        if target.exists():
            if not target.is_file() or _hash_file(target) != sha256:
                raise ImageFilesystemError(
                    "Existing canonical image path failed its SHA-256 check."
                )
            return target.resolve()

        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".image-download-",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(target)
        temporary_path = None
        return target.resolve()
    except ImageFilesystemError:
        raise
    except OSError as error:
        raise ImageFilesystemError(
            f"Could not store verified image original: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as error:
                raise ImageFilesystemError(
                    f"Could not remove temporary image file: {error}"
                ) from error


def read_verified_product_image_bytes(
    asset: DownloadedProductImage,
) -> bytes:
    """Read one stored original only after all recorded metadata matches."""

    path = Path(asset.local_path)
    try:
        if path.is_symlink():
            raise ImageIntegrityError(
                "Downloaded image path must not be a symbolic link."
            )

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as local_file:
            file_details = os.fstat(local_file.fileno())
            if not stat.S_ISREG(file_details.st_mode):
                raise ImageIntegrityError(
                    "Downloaded image path must be a regular file."
                )
            if file_details.st_size > MAX_IMAGE_BYTES:
                raise ImageIntegrityError(
                    "Downloaded image file exceeds the absolute 20 MB limit."
                )
            if file_details.st_size != asset.byte_size:
                raise ImageIntegrityError(
                    "Downloaded image byte size does not match its metadata."
                )
            data = local_file.read(MAX_IMAGE_BYTES + 1)
    except ImageIntegrityError:
        raise
    except OSError as error:
        raise ImageIntegrityError(
            "Downloaded image path must be an accessible regular file."
        ) from error

    if len(data) > MAX_IMAGE_BYTES:
        raise ImageIntegrityError(
            "Downloaded image file exceeds the absolute 20 MB limit."
        )
    if len(data) != asset.byte_size:
        raise ImageIntegrityError(
            "Downloaded image byte size does not match its metadata."
        )
    if hashlib.sha256(data).hexdigest() != asset.sha256:
        raise ImageIntegrityError(
            "Downloaded image SHA-256 does not match its metadata."
        )

    try:
        width, height, image_format, mime_type, _ = (
            _verified_image_properties(data)
        )
    except (CorruptImageError, UnsupportedImageFormatError) as error:
        raise ImageIntegrityError(
            "Downloaded image could not be decoded as a supported image."
        ) from error

    if (width, height) != (asset.width, asset.height):
        raise ImageIntegrityError(
            "Downloaded image dimensions do not match its metadata."
        )
    if image_format != asset.format:
        raise ImageIntegrityError(
            "Downloaded image format does not match its metadata."
        )
    if mime_type != asset.mime_type:
        raise ImageIntegrityError(
            "Downloaded image MIME type does not match its metadata."
        )

    return data


def verify_downloaded_product_image(
    asset: DownloadedProductImage,
) -> DownloadedProductImage:
    """Fail if a stored original no longer matches its recorded metadata."""

    read_verified_product_image_bytes(asset)
    return asset.model_copy(deep=True)


def download_product_image(
    product_id: str,
    image: ProductImage,
    *,
    storage_root: Union[str, Path] = "products",
    timeout: float = 15,
    max_bytes: int = MAX_IMAGE_BYTES,
    session: Any = None,
) -> DownloadedProductImage:
    """Download one accepted ProductImage and store its verified original."""

    if not isinstance(product_id, str) or not product_id.strip():
        raise ImageFilesystemError("A non-empty Product ID is required.")
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < 1
        or max_bytes > MAX_IMAGE_BYTES
    ):
        raise ValueError(
            "max_bytes must be a positive integer no greater than 20 MB"
        )
    source_url = _validate_source(image)
    client = session or requests
    response = None
    try:
        try:
            response = client.get(
                source_url,
                headers=DOWNLOAD_HEADERS,
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            )
            response.raise_for_status()
        except requests.Timeout as error:
            raise ImageTimeoutError(
                f"Image request timed out after {timeout} seconds."
            ) from error
        except requests.HTTPError as error:
            error_response = getattr(error, "response", None)
            status_source = error_response if error_response is not None else response
            status_code = getattr(status_source, "status_code", "unknown")
            raise ImageHttpError(
                f"Image request returned HTTP status {status_code}."
            ) from error
        except requests.RequestException as error:
            raise ImageHttpError(f"Image request failed: {error}") from error

        final_url = _validate_image_url(
            getattr(response, "url", None) or source_url,
            "Final image URL",
        )
        content_type = str(response.headers.get("Content-Type", ""))
        media_type = content_type.partition(";")[0].strip().casefold()
        if not media_type.startswith("image/"):
            displayed_type = content_type or "missing Content-Type"
            raise ImageContentTypeError(
                f"Image response did not declare an image Content-Type "
                f"({displayed_type})."
            )

        try:
            data = _read_bounded_response(response, max_bytes)
        except requests.Timeout as error:
            raise ImageTimeoutError(
                f"Image response stream timed out after {timeout} seconds."
            ) from error
        except requests.RequestException as error:
            raise ImageHttpError(f"Image response stream failed: {error}") from error
        width, height, image_format, mime_type, extension = (
            _verified_image_properties(data)
        )
        sha256 = hashlib.sha256(data).hexdigest()
        local_path = _store_original(
            data,
            product_id,
            sha256,
            extension,
            storage_root,
        )
        return DownloadedProductImage(
            product_id=product_id,
            source_image_id=image.id,
            source_url=source_url,
            final_url=final_url,
            local_path=str(local_path),
            sha256=sha256,
            byte_size=len(data),
            width=width,
            height=height,
            format=image_format,
            mime_type=mime_type,
        )
    finally:
        if response is not None:
            response.close()
