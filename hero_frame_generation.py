"""Provider capability and preflight boundary for Hero Frame generation."""

from typing import Literal, Protocol, Tuple, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from hero_frame_request import HeroFrameRequest
from product import NonEmptyStr, ProductModel


class HeroFrameGenerationOptions(ProductModel):
    """The deliberately small, provider-neutral Hero Frame v1 options."""

    image_size: Literal["1K"] = "1K"
    output_format: Literal["PNG"] = "PNG"
    attempts: Literal[1] = 1


class HeroFrameProviderCapabilities(ProductModel):
    """Immutable advertised provider limits used before any paid call."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )

    provider: NonEmptyStr
    supports_reference_images: bool
    supports_multiple_object_references: bool
    max_object_references: int = Field(ge=0, strict=True)
    supported_aspect_ratios: Tuple[NonEmptyStr, ...]
    supported_image_sizes: Tuple[NonEmptyStr, ...]
    supported_output_formats: Tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def require_consistent_reference_capabilities(
        self,
    ) -> "HeroFrameProviderCapabilities":
        if not self.supports_reference_images:
            if self.supports_multiple_object_references:
                raise ValueError(
                    "multiple references require reference-image support"
                )
            if self.max_object_references != 0:
                raise ValueError(
                    "a provider without reference support must have capacity 0"
                )
        else:
            if self.max_object_references < 1:
                raise ValueError(
                    "a reference-image provider must accept at least one image"
                )
            if (
                self.supports_multiple_object_references
                and self.max_object_references < 2
            ):
                raise ValueError(
                    "multiple-reference support requires capacity of at least 2"
                )
        for field_name in (
            "supported_aspect_ratios",
            "supported_image_sizes",
            "supported_output_formats",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} must not be empty")
        return self


class HeroFrameGenerationPreflightError(ValueError):
    """Raised when a provider cannot honor the complete generation intent."""


@runtime_checkable
class HeroFrameGenerator(Protocol):
    """Small provider boundary; provider-specific types remain behind it."""

    def capabilities(self) -> HeroFrameProviderCapabilities:
        ...

    def generate(
        self,
        request: HeroFrameRequest,
        options: HeroFrameGenerationOptions,
    ) -> object:
        ...


def preflight_hero_frame_generation(
    request: HeroFrameRequest,
    options: HeroFrameGenerationOptions,
    capabilities: HeroFrameProviderCapabilities,
) -> Tuple[HeroFrameRequest, HeroFrameGenerationOptions]:
    """Validate that every selected reference and output intent is supported."""

    try:
        validated_request = HeroFrameRequest.model_validate(request.model_dump())
        validated_options = HeroFrameGenerationOptions.model_validate(
            options.model_dump()
        )
        validated_capabilities = HeroFrameProviderCapabilities.model_validate(
            capabilities.model_dump()
        )
    except ValidationError as error:
        raise HeroFrameGenerationPreflightError(
            f"generation preflight input is invalid: {error}"
        ) from error

    total_references = 1 + len(validated_request.supporting_references)
    if not validated_capabilities.supports_reference_images:
        raise HeroFrameGenerationPreflightError(
            "provider does not support mandatory product references"
        )
    if (
        validated_request.supporting_references
        and not validated_capabilities.supports_multiple_object_references
    ):
        raise HeroFrameGenerationPreflightError(
            "provider cannot honor mandatory supporting references"
        )
    if total_references > validated_capabilities.max_object_references:
        raise HeroFrameGenerationPreflightError(
            f"request requires {total_references} references but provider "
            f"capacity is {validated_capabilities.max_object_references}"
        )
    if (
        validated_request.aspect_ratio
        not in validated_capabilities.supported_aspect_ratios
    ):
        raise HeroFrameGenerationPreflightError(
            f"provider does not support aspect ratio "
            f"{validated_request.aspect_ratio}"
        )
    if (
        validated_options.image_size
        not in validated_capabilities.supported_image_sizes
    ):
        raise HeroFrameGenerationPreflightError(
            f"provider does not support image size {validated_options.image_size}"
        )
    if (
        validated_options.output_format
        not in validated_capabilities.supported_output_formats
    ):
        raise HeroFrameGenerationPreflightError(
            f"provider does not support output format "
            f"{validated_options.output_format}"
        )
    return validated_request, validated_options
