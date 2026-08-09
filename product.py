"""Typed product domain models for the product-ad pipeline."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
SpecValue = Union[str, int, float, bool]


class ProductModel(BaseModel):
    """Base configuration shared by product domain models."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ClaimEvidence(ProductModel):
    raw_text: NonEmptyStr
    source_url: NonEmptyStr
    extraction_method: NonEmptyStr
    section_heading: Optional[NonEmptyStr] = None
    extracted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    locator: Optional[NonEmptyStr] = None

    @field_validator("extracted_at")
    @classmethod
    def require_utc_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("extracted_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class ProductVariant(ProductModel):
    color: Optional[NonEmptyStr] = None
    size: Optional[NonEmptyStr] = None
    model_number: Optional[NonEmptyStr] = None
    sku: Optional[NonEmptyStr] = None


class ProductImage(ProductModel):
    id: NonEmptyStr
    url: Optional[NonEmptyStr] = None
    local_path: Optional[NonEmptyStr] = None
    angle: Optional[NonEmptyStr] = None
    source_url: Optional[NonEmptyStr] = None
    source_type: Optional[NonEmptyStr] = None
    width: Optional[int] = Field(default=None, gt=0, strict=True)
    height: Optional[int] = Field(default=None, gt=0, strict=True)
    confidence: Optional[float] = Field(default=None, ge=0, le=1, strict=True)


class ProductSpec(ProductModel):
    key: NonEmptyStr
    label: NonEmptyStr
    value: SpecValue
    unit: Optional[NonEmptyStr] = None
    source_url: Optional[NonEmptyStr] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1, strict=True)
    verified: bool = False
    evidence: Optional[ClaimEvidence] = None


class ProductFeature(ProductModel):
    id: NonEmptyStr
    name: NonEmptyStr
    description: Optional[NonEmptyStr] = None
    source_url: Optional[NonEmptyStr] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1, strict=True)
    verified: bool = False
    evidence: Optional[ClaimEvidence] = None


class ProductSource(ProductModel):
    url: NonEmptyStr
    source_type: NonEmptyStr
    domain: Optional[NonEmptyStr] = None
    title: Optional[NonEmptyStr] = None


class Product(ProductModel):
    id: NonEmptyStr
    name: NonEmptyStr
    brand: NonEmptyStr
    category: NonEmptyStr
    description: Optional[NonEmptyStr] = None
    variant: Optional[ProductVariant] = None
    images: List[ProductImage] = Field(default_factory=list)
    specs: List[ProductSpec] = Field(default_factory=list)
    features: List[ProductFeature] = Field(default_factory=list)
    sources: List[ProductSource] = Field(default_factory=list)


def save_product(
    product: Product,
    path: Union[str, Path] = "product.json",
) -> Path:
    """Serialize a product to readable UTF-8 JSON and return its path."""

    output_path = Path(path)
    json_text = product.model_dump_json(
        indent=2,
        ensure_ascii=False,
        exclude_none=True,
    )
    output_path.write_text(json_text + "\n", encoding="utf-8")
    return output_path


def load_product(path: Union[str, Path] = "product.json") -> Product:
    """Load and validate a product from a UTF-8 JSON file."""

    input_path = Path(path)
    return Product.model_validate_json(input_path.read_text(encoding="utf-8"))
