"""Deterministic construction of locally verified product image sets."""

from typing import Dict, List, Set

from product import Product
from product_asset_manifest import ProductAssetManifest
from product_image_downloads import (
    DownloadedProductImage,
    verify_downloaded_product_image,
)
from product_images import rank_product_images


class ProductImageSetError(RuntimeError):
    """Base error for constructing a verified product image set."""


class ProductImageSetProductMismatchError(ProductImageSetError):
    """Raised when Product and manifest identities do not match."""


class UnknownManifestImageError(ProductImageSetError):
    """Raised when a manifest asset has no current ProductImage source."""


class DuplicateProductImageIdError(ProductImageSetError):
    """Raised when current ProductImage IDs cannot be joined unambiguously."""


def build_verified_product_image_set(
    product: Product,
    manifest: ProductAssetManifest,
) -> List[DownloadedProductImage]:
    """Return integrity-checked unique originals in ProductImage rank order."""

    if product.id != manifest.product_id:
        raise ProductImageSetProductMismatchError(
            f"Product {product.id} does not match asset manifest "
            f"{manifest.product_id}."
        )

    product_image_ids: Set[str] = set()
    for image in product.images:
        if image.id in product_image_ids:
            raise DuplicateProductImageIdError(
                f"Product contains duplicate ProductImage ID: {image.id}."
            )
        product_image_ids.add(image.id)

    unknown_ids = sorted(
        image.source_image_id
        for image in manifest.images
        if image.source_image_id not in product_image_ids
    )
    if unknown_ids:
        raise UnknownManifestImageError(
            "Asset manifest references ProductImage IDs not present in the "
            f"current Product: {', '.join(unknown_ids)}."
        )

    verified_by_source: Dict[str, DownloadedProductImage] = {
        image.source_image_id: verify_downloaded_product_image(image)
        for image in manifest.images
    }

    selected: List[DownloadedProductImage] = []
    selected_hashes: Set[str] = set()
    for product_image in rank_product_images(product.images):
        downloaded = verified_by_source.get(product_image.id)
        if downloaded is None or downloaded.sha256 in selected_hashes:
            continue
        selected.append(downloaded.model_copy(deep=True))
        selected_hashes.add(downloaded.sha256)
    return selected
