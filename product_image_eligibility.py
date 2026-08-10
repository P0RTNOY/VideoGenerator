"""Objective, caller-defined eligibility checks for verified product images."""

from typing import List, Literal, Optional

from pydantic import Field, field_validator, model_validator

from product import NonEmptyStr, ProductModel
from product_image_downloads import (
    DownloadedProductImage,
    ImageFormat,
    Sha256Hex,
    verify_downloaded_product_image,
)


EligibilityViolation = Literal[
    "width_below_minimum",
    "height_below_minimum",
    "pixel_count_below_minimum",
    "format_not_allowed",
]


class ProductImageEligibilityRequirements(ProductModel):
    """Explicit measurable requirements; no creative defaults are implied."""

    min_width: Optional[int] = Field(default=None, gt=0, strict=True)
    min_height: Optional[int] = Field(default=None, gt=0, strict=True)
    min_pixel_count: Optional[int] = Field(default=None, gt=0, strict=True)
    allowed_formats: Optional[List[ImageFormat]] = None

    @field_validator("allowed_formats")
    @classmethod
    def require_unique_nonempty_formats(
        cls,
        value: Optional[List[ImageFormat]],
    ) -> Optional[List[ImageFormat]]:
        if value is not None and not value:
            raise ValueError("allowed_formats must not be empty")
        if value is not None and len(value) != len(set(value)):
            raise ValueError("allowed_formats must not contain duplicates")
        return value


class ProductImageEligibilityResult(ProductModel):
    """Measured local properties and violations of explicit requirements."""

    product_id: NonEmptyStr
    source_image_id: NonEmptyStr
    sha256: Sha256Hex
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    pixel_count: int = Field(gt=0, strict=True)
    aspect_ratio: float = Field(gt=0, strict=True)
    format: ImageFormat
    eligible: bool
    violations: List[EligibilityViolation] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_consistent_outcome(self) -> "ProductImageEligibilityResult":
        if len(self.violations) != len(set(self.violations)):
            raise ValueError("eligibility violations must not contain duplicates")
        if self.eligible == bool(self.violations):
            raise ValueError(
                "eligible must be true exactly when violations are empty"
            )
        return self


def evaluate_product_image_eligibility(
    asset: DownloadedProductImage,
    requirements: ProductImageEligibilityRequirements,
) -> ProductImageEligibilityResult:
    """Evaluate one integrity-verified image against explicit measurements."""

    verified = verify_downloaded_product_image(asset)
    pixel_count = verified.width * verified.height
    violations: List[EligibilityViolation] = []

    if (
        requirements.min_width is not None
        and verified.width < requirements.min_width
    ):
        violations.append("width_below_minimum")
    if (
        requirements.min_height is not None
        and verified.height < requirements.min_height
    ):
        violations.append("height_below_minimum")
    if (
        requirements.min_pixel_count is not None
        and pixel_count < requirements.min_pixel_count
    ):
        violations.append("pixel_count_below_minimum")
    if (
        requirements.allowed_formats is not None
        and verified.format not in requirements.allowed_formats
    ):
        violations.append("format_not_allowed")

    return ProductImageEligibilityResult(
        product_id=verified.product_id,
        source_image_id=verified.source_image_id,
        sha256=verified.sha256,
        width=verified.width,
        height=verified.height,
        pixel_count=pixel_count,
        aspect_ratio=verified.width / verified.height,
        format=verified.format,
        eligible=not violations,
        violations=violations,
    )
