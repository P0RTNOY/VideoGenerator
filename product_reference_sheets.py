"""Deterministic PNG reference sheets built from verified product images."""

import hashlib
import io
import os
import stat
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional, Tuple, Union

from PIL import Image, UnidentifiedImageError
from pydantic import Field, field_validator, model_validator

from product import NonEmptyStr, Product, ProductModel
from product_asset_manifest import ProductAssetManifest
from product_image_downloads import (
    DownloadedProductImage,
    ImageFilesystemError,
    Sha256Hex,
    product_asset_directory_name,
    read_verified_product_image_bytes,
)
from product_image_eligibility import (
    ProductImageEligibilityRequirements,
    evaluate_product_image_eligibility,
)
from product_image_sets import build_verified_product_image_set


MAX_REFERENCE_IMAGES = 6
MAX_CANVAS_SIZE = 2048
MAX_REFERENCE_SHEET_BYTES = 20 * 1024 * 1024
DEFAULT_CANVAS_SIZE = 2048
DEFAULT_OUTER_MARGIN = 64
DEFAULT_TILE_GAP = 32
WHITE = (255, 255, 255)


def _default_eligibility() -> ProductImageEligibilityRequirements:
    return ProductImageEligibilityRequirements(
        min_width=512,
        min_height=512,
        allowed_formats=["JPEG", "PNG", "WEBP"],
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProductReferenceSheetError(RuntimeError):
    """Base error for deterministic product reference-sheet generation."""


class NoEligibleReferenceImagesError(ProductReferenceSheetError):
    """Raised when no verified image satisfies reference-sheet policy."""


class ReferenceSheetRenderError(ProductReferenceSheetError):
    """Raised when verified source bytes cannot be rendered safely."""


class ReferenceSheetFilesystemError(ProductReferenceSheetError):
    """Raised when deterministic sheet output cannot be stored safely."""


class ReferenceSheetIntegrityError(ProductReferenceSheetError):
    """Raised when a stored reference sheet no longer matches its metadata."""


class ProductReferenceSheetPolicy(ProductModel):
    """Technical layout and input policy for one reference sheet."""

    canvas_width: int = Field(
        default=DEFAULT_CANVAS_SIZE,
        gt=0,
        le=MAX_CANVAS_SIZE,
        strict=True,
    )
    canvas_height: int = Field(
        default=DEFAULT_CANVAS_SIZE,
        gt=0,
        le=MAX_CANVAS_SIZE,
        strict=True,
    )
    outer_margin: int = Field(
        default=DEFAULT_OUTER_MARGIN,
        ge=0,
        strict=True,
    )
    tile_gap: int = Field(
        default=DEFAULT_TILE_GAP,
        ge=0,
        strict=True,
    )
    eligibility: ProductImageEligibilityRequirements = Field(
        default_factory=_default_eligibility
    )

    @model_validator(mode="after")
    def require_usable_six_image_grid(self) -> "ProductReferenceSheetPolicy":
        usable_width = (
            self.canvas_width
            - 2 * self.outer_margin
            - 2 * self.tile_gap
        )
        usable_height = (
            self.canvas_height
            - 2 * self.outer_margin
            - self.tile_gap
        )
        if usable_width < 3 or usable_height < 2:
            raise ValueError(
                "canvas, margin, and gap must leave positive 3x2 tile bounds"
            )
        return self


class ProductReferenceSheetTile(ProductModel):
    """Rendered tile geometry linked to one verified downloaded original."""

    index: int = Field(ge=0, strict=True)
    source_image_id: NonEmptyStr
    source_sha256: Sha256Hex
    source_local_path: NonEmptyStr
    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    rendered_x: int = Field(ge=0, strict=True)
    rendered_y: int = Field(ge=0, strict=True)
    rendered_width: int = Field(gt=0, strict=True)
    rendered_height: int = Field(gt=0, strict=True)


class ProductReferenceSheet(ProductModel):
    """Metadata for one deterministic, product-bound PNG reference sheet."""

    product_id: NonEmptyStr
    local_path: NonEmptyStr
    width: int = Field(gt=0, le=MAX_CANVAS_SIZE, strict=True)
    height: int = Field(gt=0, le=MAX_CANVAS_SIZE, strict=True)
    format: Literal["PNG"] = "PNG"
    sha256: Sha256Hex
    byte_size: Optional[int] = Field(default=None, gt=0, strict=True)
    created_at: datetime = Field(default_factory=_utc_now)
    tiles: List[ProductReferenceSheetTile] = Field(
        min_length=1,
        max_length=MAX_REFERENCE_IMAGES,
    )

    @field_validator("created_at")
    @classmethod
    def require_utc_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_consistent_tile_geometry(self) -> "ProductReferenceSheet":
        if [tile.index for tile in self.tiles] != list(range(len(self.tiles))):
            raise ValueError("reference-sheet tile indices must be sequential")
        if len({tile.source_sha256 for tile in self.tiles}) != len(self.tiles):
            raise ValueError("reference-sheet tiles must use unique source bytes")
        if len({tile.source_image_id for tile in self.tiles}) != len(self.tiles):
            raise ValueError("reference-sheet source image IDs must be unique")

        for tile in self.tiles:
            if tile.x + tile.width > self.width:
                raise ValueError("reference-sheet tile exceeds canvas width")
            if tile.y + tile.height > self.height:
                raise ValueError("reference-sheet tile exceeds canvas height")
            if tile.rendered_x < tile.x or tile.rendered_y < tile.y:
                raise ValueError("rendered image must begin inside its tile")
            if tile.rendered_x + tile.rendered_width > tile.x + tile.width:
                raise ValueError("rendered image exceeds tile width")
            if tile.rendered_y + tile.rendered_height > tile.y + tile.height:
                raise ValueError("rendered image exceeds tile height")
        return self


def _grid_shape(image_count: int) -> Tuple[int, int]:
    if image_count == 1:
        return 1, 1
    if image_count == 2:
        return 2, 1
    if image_count <= 4:
        return 2, 2
    return 3, 2


def _partition(total: int, count: int) -> List[int]:
    base, remainder = divmod(total, count)
    return [base + int(index < remainder) for index in range(count)]


def _tile_bounds(
    image_count: int,
    policy: ProductReferenceSheetPolicy,
) -> List[Tuple[int, int, int, int]]:
    columns, rows = _grid_shape(image_count)
    usable_width = (
        policy.canvas_width
        - 2 * policy.outer_margin
        - policy.tile_gap * (columns - 1)
    )
    usable_height = (
        policy.canvas_height
        - 2 * policy.outer_margin
        - policy.tile_gap * (rows - 1)
    )
    column_widths = _partition(usable_width, columns)
    row_heights = _partition(usable_height, rows)

    x_positions = []
    cursor = policy.outer_margin
    for width in column_widths:
        x_positions.append(cursor)
        cursor += width + policy.tile_gap

    y_positions = []
    cursor = policy.outer_margin
    for height in row_heights:
        y_positions.append(cursor)
        cursor += height + policy.tile_gap

    bounds = []
    for index in range(image_count):
        row, column = divmod(index, columns)
        bounds.append(
            (
                x_positions[column],
                y_positions[row],
                column_widths[column],
                row_heights[row],
            )
        )
    return bounds


def _contained_size(
    source_width: int,
    source_height: int,
    tile_width: int,
    tile_height: int,
) -> Tuple[int, int]:
    if source_width <= tile_width and source_height <= tile_height:
        return source_width, source_height
    if tile_width * source_height <= tile_height * source_width:
        return tile_width, max(1, source_height * tile_width // source_width)
    return max(1, source_width * tile_height // source_height), tile_height


def _rgb_source_image(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            if "A" in source.getbands() or "transparency" in source.info:
                rgba = source.convert("RGBA")
                flattened = Image.new("RGBA", rgba.size, WHITE + (255,))
                flattened.alpha_composite(rgba)
                return flattened.convert("RGB")
            return source.convert("RGB")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise ReferenceSheetRenderError(
            "Verified source image could not be decoded for rendering."
        ) from error


def _render_reference_sheet(
    images: List[DownloadedProductImage],
    policy: ProductReferenceSheetPolicy,
) -> Tuple[bytes, List[ProductReferenceSheetTile]]:
    canvas = Image.new(
        "RGB",
        (policy.canvas_width, policy.canvas_height),
        WHITE,
    )
    try:
        tiles = []
        for index, (asset, bounds) in enumerate(
            zip(images, _tile_bounds(len(images), policy))
        ):
            tile_x, tile_y, tile_width, tile_height = bounds
            source_data = read_verified_product_image_bytes(asset)
            source = _rgb_source_image(source_data)
            rendered_width, rendered_height = _contained_size(
                asset.width,
                asset.height,
                tile_width,
                tile_height,
            )
            rendered = source
            try:
                if (rendered_width, rendered_height) != source.size:
                    rendered = source.resize(
                        (rendered_width, rendered_height),
                        resample=Image.Resampling.LANCZOS,
                    )
                rendered_x = tile_x + (tile_width - rendered_width) // 2
                rendered_y = tile_y + (tile_height - rendered_height) // 2
                canvas.paste(rendered, (rendered_x, rendered_y))
            finally:
                if rendered is not source:
                    rendered.close()
                source.close()
            tiles.append(
                ProductReferenceSheetTile(
                    index=index,
                    source_image_id=asset.source_image_id,
                    source_sha256=asset.sha256,
                    source_local_path=asset.local_path,
                    x=tile_x,
                    y=tile_y,
                    width=tile_width,
                    height=tile_height,
                    rendered_x=rendered_x,
                    rendered_y=rendered_y,
                    rendered_width=rendered_width,
                    rendered_height=rendered_height,
                )
            )

        output = io.BytesIO()
        canvas.save(
            output,
            format="PNG",
            optimize=False,
            compress_level=9,
        )
        return output.getvalue(), tiles
    finally:
        canvas.close()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stored_file:
        for chunk in iter(lambda: stored_file.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_verified_product_reference_sheet_bytes(
    sheet: ProductReferenceSheet,
) -> bytes:
    """Read a canonical sheet only after file and decoded metadata match."""

    path = Path(sheet.local_path)
    try:
        if path.is_symlink():
            raise ReferenceSheetIntegrityError(
                "Reference-sheet path must not be a symbolic link."
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as local_file:
            file_details = os.fstat(local_file.fileno())
            if not stat.S_ISREG(file_details.st_mode):
                raise ReferenceSheetIntegrityError(
                    "Reference-sheet path must be a regular file."
                )
            if file_details.st_size > MAX_REFERENCE_SHEET_BYTES:
                raise ReferenceSheetIntegrityError(
                    "Reference-sheet file exceeds the absolute 20 MB limit."
                )
            if (
                sheet.byte_size is not None
                and file_details.st_size != sheet.byte_size
            ):
                raise ReferenceSheetIntegrityError(
                    "Reference-sheet byte size does not match its metadata."
                )
            data = local_file.read(MAX_REFERENCE_SHEET_BYTES + 1)
    except ReferenceSheetIntegrityError:
        raise
    except OSError as error:
        raise ReferenceSheetIntegrityError(
            "Reference-sheet path must be an accessible regular file."
        ) from error

    if len(data) > MAX_REFERENCE_SHEET_BYTES:
        raise ReferenceSheetIntegrityError(
            "Reference-sheet file exceeds the absolute 20 MB limit."
        )
    if sheet.byte_size is not None and len(data) != sheet.byte_size:
        raise ReferenceSheetIntegrityError(
            "Reference-sheet byte size does not match its metadata."
        )
    if hashlib.sha256(data).hexdigest() != sheet.sha256:
        raise ReferenceSheetIntegrityError(
            "Reference-sheet SHA-256 does not match its metadata."
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as candidate:
                image_format = candidate.format
                image_size = candidate.size
                image_mode = candidate.mode
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
        raise ReferenceSheetIntegrityError(
            "Reference-sheet bytes could not be fully decoded."
        ) from error

    if image_format != "PNG":
        raise ReferenceSheetIntegrityError(
            "Reference-sheet decoded format must remain PNG."
        )
    if image_size != (sheet.width, sheet.height):
        raise ReferenceSheetIntegrityError(
            "Reference-sheet decoded dimensions do not match its metadata."
        )
    if image_mode != "RGB":
        raise ReferenceSheetIntegrityError(
            "Reference-sheet decoded mode must remain RGB."
        )
    return data


def verify_product_reference_sheet(
    sheet: ProductReferenceSheet,
) -> ProductReferenceSheet:
    """Fail if a stored reference sheet no longer matches its metadata."""

    read_verified_product_reference_sheet_bytes(sheet)
    return sheet.model_copy(deep=True)


def _store_reference_sheet(
    data: bytes,
    product_id: str,
    sha256: str,
    storage_root: Union[str, Path],
) -> Path:
    root = Path(storage_root)
    try:
        product_directory = product_asset_directory_name(product_id)
    except ImageFilesystemError as error:
        raise ReferenceSheetFilesystemError(
            "Product ID cannot be represented as a reference-sheet directory."
        ) from error
    directory = root / product_directory / "assets" / "derived" / "reference-sheets"
    target = directory / f"{sha256}.png"
    temporary_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise ReferenceSheetFilesystemError(
                "Reference-sheet directory resolves outside its storage root."
            ) from error
        if target.is_symlink():
            raise ReferenceSheetFilesystemError(
                "Reference-sheet path must not be a symbolic link."
            )
        if target.exists():
            if not target.is_file() or _hash_file(target) != sha256:
                raise ReferenceSheetFilesystemError(
                    "Existing reference-sheet path failed its SHA-256 check."
                )
            return target.resolve()

        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".reference-sheet-",
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
    except ReferenceSheetFilesystemError:
        raise
    except OSError as error:
        raise ReferenceSheetFilesystemError(
            f"Could not store reference-sheet PNG: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as error:
                raise ReferenceSheetFilesystemError(
                    f"Could not remove temporary reference-sheet file: {error}"
                ) from error


def _eligible_reference_images(
    product: Product,
    manifest: ProductAssetManifest,
    policy: ProductReferenceSheetPolicy,
) -> List[DownloadedProductImage]:
    verified_images = build_verified_product_image_set(product, manifest)
    eligible_images = [
        image
        for image in verified_images
        if evaluate_product_image_eligibility(
            image,
            policy.eligibility,
        ).eligible
    ]
    if not eligible_images:
        raise NoEligibleReferenceImagesError(
            "No verified product images satisfy reference-sheet eligibility."
        )
    return eligible_images


def _build_reference_sheet_from_images(
    product_id: str,
    images: List[DownloadedProductImage],
    storage_root: Union[str, Path],
    policy: ProductReferenceSheetPolicy,
) -> ProductReferenceSheet:
    png_bytes, tiles = _render_reference_sheet(images, policy)
    sha256 = hashlib.sha256(png_bytes).hexdigest()
    local_path = _store_reference_sheet(
        png_bytes,
        product_id,
        sha256,
        storage_root,
    )
    return ProductReferenceSheet(
        product_id=product_id,
        local_path=str(local_path),
        width=policy.canvas_width,
        height=policy.canvas_height,
        sha256=sha256,
        byte_size=len(png_bytes),
        created_at=_utc_now(),
        tiles=tiles,
    )


def build_product_reference_sheet(
    product: Product,
    manifest: ProductAssetManifest,
    *,
    storage_root: Union[str, Path] = "products",
    policy: Optional[ProductReferenceSheetPolicy] = None,
) -> ProductReferenceSheet:
    """Build one product-bound PNG from the first six eligible verified images."""

    active_policy = (
        policy.model_copy(deep=True)
        if policy is not None
        else ProductReferenceSheetPolicy()
    )
    eligible_images = _eligible_reference_images(
        product,
        manifest,
        active_policy,
    )
    return _build_reference_sheet_from_images(
        product.id,
        eligible_images[:MAX_REFERENCE_IMAGES],
        storage_root,
        active_policy,
    )


def build_product_reference_sheet_set(
    product: Product,
    manifest: ProductAssetManifest,
    *,
    storage_root: Union[str, Path] = "products",
    policy: Optional[ProductReferenceSheetPolicy] = None,
) -> List[ProductReferenceSheet]:
    """Build ordered sheets covering every eligible verified image once."""

    active_policy = (
        policy.model_copy(deep=True)
        if policy is not None
        else ProductReferenceSheetPolicy()
    )
    eligible_images = _eligible_reference_images(
        product,
        manifest,
        active_policy,
    )
    return [
        _build_reference_sheet_from_images(
            product.id,
            eligible_images[start : start + MAX_REFERENCE_IMAGES],
            storage_root,
            active_policy,
        )
        for start in range(0, len(eligible_images), MAX_REFERENCE_IMAGES)
    ]
