"""Strict manifests linking product originals to generated derived assets."""

from pathlib import Path
from typing import List, Literal, Sequence, Union

from pydantic import Field, model_validator

from product import NonEmptyStr, ProductModel
from product_asset_manifest import ProductAssetManifest
from product_reference_sheets import ProductReferenceSheet


class ProductDerivedAssetManifestError(RuntimeError):
    """Base error for derived-asset manifest linkage."""


class DerivedAssetSourceLinkError(ProductDerivedAssetManifestError):
    """Raised when a derived tile cannot be linked to an original asset."""


class ProductDerivedAssetManifest(ProductModel):
    """Product-bound metadata for generated derived assets."""

    schema_version: Literal[1] = 1
    product_id: NonEmptyStr
    reference_sheets: List[ProductReferenceSheet] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_consistent_reference_sheets(
        self,
    ) -> "ProductDerivedAssetManifest":
        path_by_hash = {}
        hash_by_path = {}
        provenance_signatures = set()
        for sheet in self.reference_sheets:
            if sheet.product_id != self.product_id:
                raise ValueError(
                    f"reference sheet belongs to Product {sheet.product_id}, "
                    f"not {self.product_id}"
                )
            existing_path = path_by_hash.get(sheet.sha256)
            if existing_path is not None and existing_path != sheet.local_path:
                raise ValueError(
                    "reference sheets with the same SHA-256 must use the "
                    "same local path"
                )
            path_by_hash[sheet.sha256] = sheet.local_path

            existing_hash = hash_by_path.get(sheet.local_path)
            if existing_hash is not None and existing_hash != sheet.sha256:
                raise ValueError(
                    "reference sheets using the same local path must have "
                    "the same SHA-256"
                )
            hash_by_path[sheet.local_path] = sheet.sha256

            provenance_signature = (
                sheet.sha256,
                sheet.local_path,
                tuple(
                    (
                        tile.source_image_id,
                        tile.source_sha256,
                        tile.source_local_path,
                    )
                    for tile in sheet.tiles
                ),
            )
            if provenance_signature in provenance_signatures:
                raise ValueError(
                    "duplicate reference-sheet binary and tile provenance"
                )
            provenance_signatures.add(provenance_signature)
        return self


def validate_product_derived_asset_sources(
    derived_manifest: ProductDerivedAssetManifest,
    original_manifest: ProductAssetManifest,
) -> ProductDerivedAssetManifest:
    """Validate every reference-sheet tile against its original asset record."""

    if derived_manifest.product_id != original_manifest.product_id:
        raise DerivedAssetSourceLinkError(
            f"Derived manifest Product {derived_manifest.product_id} does not "
            f"match original manifest Product {original_manifest.product_id}."
        )

    originals_by_source = {
        image.source_image_id: image for image in original_manifest.images
    }
    for sheet in derived_manifest.reference_sheets:
        for tile in sheet.tiles:
            original = originals_by_source.get(tile.source_image_id)
            if original is None:
                raise DerivedAssetSourceLinkError(
                    "Reference-sheet tile links to unknown source ProductImage "
                    f"ID: {tile.source_image_id}."
                )
            if tile.source_sha256 != original.sha256:
                raise DerivedAssetSourceLinkError(
                    f"Reference-sheet tile {tile.source_image_id} SHA-256 does "
                    "not match its original asset."
                )
            if tile.source_local_path != original.local_path:
                raise DerivedAssetSourceLinkError(
                    f"Reference-sheet tile {tile.source_image_id} local path "
                    "does not match its original asset."
                )
    return derived_manifest.model_copy(deep=True)


def build_product_derived_asset_manifest(
    original_manifest: ProductAssetManifest,
    reference_sheets: Sequence[ProductReferenceSheet],
) -> ProductDerivedAssetManifest:
    """Build and source-validate one product-scoped derived-asset manifest."""

    derived_manifest = ProductDerivedAssetManifest(
        product_id=original_manifest.product_id,
        reference_sheets=[
            sheet.model_copy(deep=True) for sheet in reference_sheets
        ],
    )
    return validate_product_derived_asset_sources(
        derived_manifest,
        original_manifest,
    )


def save_product_derived_asset_manifest(
    manifest: ProductDerivedAssetManifest,
    path: Union[str, Path],
) -> Path:
    """Write a human-readable UTF-8 derived manifest and return its path."""

    output_path = Path(path)
    json_text = manifest.model_dump_json(
        indent=2,
        ensure_ascii=False,
        exclude_none=True,
    )
    output_path.write_text(json_text + "\n", encoding="utf-8")
    return output_path


def load_product_derived_asset_manifest(
    path: Union[str, Path],
) -> ProductDerivedAssetManifest:
    """Load and structurally validate one UTF-8 derived-asset manifest."""

    input_path = Path(path)
    return ProductDerivedAssetManifest.model_validate_json(
        input_path.read_text(encoding="utf-8")
    )
