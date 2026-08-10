"""Strict JSON manifests for verified local product image assets."""

from pathlib import Path
from typing import List, Literal, Union

from pydantic import Field, model_validator

from product import NonEmptyStr, ProductModel
from product_image_downloads import DownloadedProductImage


class ProductAssetManifest(ProductModel):
    """Downloaded image metadata grouped under one Product identity."""

    schema_version: Literal[1] = 1
    product_id: NonEmptyStr
    images: List[DownloadedProductImage] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_consistent_product_assets(self) -> "ProductAssetManifest":
        source_ids = set()
        path_by_hash = {}
        hash_by_path = {}
        for image in self.images:
            if image.product_id != self.product_id:
                raise ValueError(
                    f"asset {image.source_image_id} belongs to Product "
                    f"{image.product_id}, not {self.product_id}"
                )
            if image.source_image_id in source_ids:
                raise ValueError(
                    f"duplicate source ProductImage ID: {image.source_image_id}"
                )
            source_ids.add(image.source_image_id)

            existing_path = path_by_hash.get(image.sha256)
            if existing_path is not None and existing_path != image.local_path:
                raise ValueError(
                    "assets with the same SHA-256 must use the same local path"
                )
            path_by_hash[image.sha256] = image.local_path

            existing_hash = hash_by_path.get(image.local_path)
            if existing_hash is not None and existing_hash != image.sha256:
                raise ValueError(
                    "assets using the same local path must have the same SHA-256"
                )
            hash_by_path[image.local_path] = image.sha256
        return self


def save_product_asset_manifest(
    manifest: ProductAssetManifest,
    path: Union[str, Path],
) -> Path:
    """Write a human-readable UTF-8 asset manifest and return its path."""

    output_path = Path(path)
    json_text = manifest.model_dump_json(
        indent=2,
        ensure_ascii=False,
        exclude_none=True,
    )
    output_path.write_text(json_text + "\n", encoding="utf-8")
    return output_path


def load_product_asset_manifest(path: Union[str, Path]) -> ProductAssetManifest:
    """Load and validate one UTF-8 product asset manifest."""

    input_path = Path(path)
    return ProductAssetManifest.model_validate_json(
        input_path.read_text(encoding="utf-8")
    )
