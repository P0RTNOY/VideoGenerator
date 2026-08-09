"""Deterministic metadata-only ranking for researched product images."""

from typing import List, Optional, Sequence, Tuple

from product import ProductImage


def _ranking_key(image: ProductImage) -> Tuple[int, float, int, int]:
    confidence_known = int(image.confidence is not None)
    confidence = image.confidence if image.confidence is not None else 0.0
    dimension_count = int(image.width is not None) + int(image.height is not None)
    pixel_area = (
        image.width * image.height
        if image.width is not None and image.height is not None
        else 0
    )
    return confidence_known, confidence, dimension_count, pixel_area


def rank_product_images(images: Sequence[ProductImage]) -> List[ProductImage]:
    """Return isolated images ordered by confidence then dimension metadata."""

    return [
        image.model_copy(deep=True)
        for image in sorted(images, key=_ranking_key, reverse=True)
    ]


def select_primary_product_image(
    images: Sequence[ProductImage],
) -> Optional[ProductImage]:
    """Return an isolated highest-ranked image, or None when none are known."""

    ranked = rank_product_images(images)
    return ranked[0] if ranked else None
