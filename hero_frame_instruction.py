"""Provider-neutral structured instructions for Hero Frame generation."""

from typing import List, Literal, Optional, Tuple

from pydantic import Field, ValidationError, model_validator

from campaign import CampaignClaimSnapshot, CreativeStyle
from hero_frame_request import HeroFrameRequest, ResolvedProductIdentity
from product import NonEmptyStr, ProductModel
from storyboard import ShotPurpose
from storyboard_asset_plan import AssetKind


MAX_CREATIVE_INSTRUCTION_LENGTH = 4000


class HeroFrameInstructionError(ValueError):
    """Base error for controlled instruction compilation failures."""


class CreativeInstructionTooLongError(HeroFrameInstructionError):
    """Raised when authored creative data exceeds the explicit boundary."""


class HeroFrameCreativeInstruction(ProductModel):
    """Untrusted authored scene data, kept separate from generation policy."""

    shot_purpose: ShotPurpose
    environment: NonEmptyStr
    scene_direction: NonEmptyStr
    camera_direction: Optional[NonEmptyStr] = None
    creative_style: CreativeStyle
    creative_brief: Optional[NonEmptyStr] = None


class HeroFrameProductFidelityInstruction(ProductModel):
    policy: Literal["preserve_exact_product"] = "preserve_exact_product"
    authoritative_traits: Tuple[
        Literal["geometry"],
        Literal["proportions"],
        Literal["color / variant"],
        Literal["materials"],
        Literal["control locations"],
        Literal["buttons"],
        Literal["ports"],
        Literal["seams"],
        Literal["panel boundaries"],
        Literal["logos"],
        Literal["existing markings"],
        Literal["visible construction"],
    ] = (
        "geometry",
        "proportions",
        "color / variant",
        "materials",
        "control locations",
        "buttons",
        "ports",
        "seams",
        "panel boundaries",
        "logos",
        "existing markings",
        "visible construction",
    )
    allowed_creative_changes: Tuple[
        Literal["scene"],
        Literal["human interaction"],
        Literal["framing"],
        Literal["camera"],
        Literal["lighting"],
        Literal["composition"],
    ] = (
        "scene",
        "human interaction",
        "framing",
        "camera",
        "lighting",
        "composition",
    )


class HeroFrameFactualGroundingInstruction(ProductModel):
    policy: Literal["selected_claims_only"] = "selected_claims_only"
    authority: Literal["factual_claims_section_only"] = (
        "factual_claims_section_only"
    )


class HeroFrameAddedTextInstruction(ProductModel):
    policy: Literal["no_added_text"] = "no_added_text"
    prohibited_additions: Tuple[
        Literal["advertising text"],
        Literal["captions"],
        Literal["CTA copy"],
        Literal["overlays"],
    ] = ("advertising text", "captions", "CTA copy", "overlays")
    preserved_markings: Tuple[
        Literal["physical brand logos"],
        Literal["button markings"],
        Literal["product labels"],
    ] = ("physical brand logos", "button markings", "product labels")


ReferenceRole = Literal[
    "primary_product_identity",
    "supporting_product_geometry",
    "supporting_multi_view_product_geometry",
]


class HeroFrameReferenceInstruction(ProductModel):
    asset_id: NonEmptyStr
    asset_kind: AssetKind
    sha256: NonEmptyStr
    role: ReferenceRole


class HeroFrameOutputInstruction(ProductModel):
    aspect_ratio: Literal["9:16"] = "9:16"
    image_size: Literal["1K"] = "1K"
    format: Literal["PNG"] = "PNG"


class HeroFrameGenerationInstruction(ProductModel):
    """Provider-neutral generation intent with policy and creative channels."""

    product: ResolvedProductIdentity
    factual_claims: List[CampaignClaimSnapshot] = Field(default_factory=list)
    creative_direction: HeroFrameCreativeInstruction
    product_fidelity: HeroFrameProductFidelityInstruction
    factual_grounding: HeroFrameFactualGroundingInstruction
    added_text: HeroFrameAddedTextInstruction
    primary_reference: HeroFrameReferenceInstruction
    supporting_references: List[HeroFrameReferenceInstruction] = Field(
        default_factory=list
    )
    output: HeroFrameOutputInstruction

    @model_validator(mode="after")
    def require_reference_roles(self) -> "HeroFrameGenerationInstruction":
        if self.primary_reference.role != "primary_product_identity":
            raise ValueError("primary reference must retain its identity role")
        for reference in self.supporting_references:
            expected = (
                "supporting_multi_view_product_geometry"
                if reference.asset_kind == "reference_sheet"
                else "supporting_product_geometry"
            )
            if reference.role != expected:
                raise ValueError(
                    f"supporting reference role does not match asset kind: "
                    f"{reference.asset_id}"
                )
        return self


def _reference_instruction(
    asset,
    role: ReferenceRole,
) -> HeroFrameReferenceInstruction:
    return HeroFrameReferenceInstruction(
        asset_id=asset.asset_id,
        asset_kind=asset.asset_kind,
        sha256=asset.sha256,
        role=role,
    )


def compile_hero_frame_instruction(
    request: HeroFrameRequest,
) -> HeroFrameGenerationInstruction:
    """Compile one request without copying paths or broad Product facts."""

    try:
        validated_request = HeroFrameRequest.model_validate(request.model_dump())
    except ValidationError as error:
        raise HeroFrameInstructionError(
            f"Hero Frame request must be structurally valid: {error}"
        ) from error

    creative = validated_request.creative_direction
    for field_name in (
        "environment",
        "scene_direction",
        "camera_direction",
        "creative_brief",
    ):
        value = getattr(creative, field_name)
        if value is not None and len(value) > MAX_CREATIVE_INSTRUCTION_LENGTH:
            raise CreativeInstructionTooLongError(
                f"creative field exceeds {MAX_CREATIVE_INSTRUCTION_LENGTH} "
                f"characters: {field_name}"
            )

    supporting_references = [
        _reference_instruction(
            asset,
            (
                "supporting_multi_view_product_geometry"
                if asset.asset_kind == "reference_sheet"
                else "supporting_product_geometry"
            ),
        )
        for asset in validated_request.supporting_references
    ]
    return HeroFrameGenerationInstruction(
        product=validated_request.product.model_copy(deep=True),
        factual_claims=[
            claim.model_copy(deep=True)
            for claim in validated_request.factual_claims
        ],
        creative_direction=HeroFrameCreativeInstruction(
            **creative.model_dump()
        ),
        product_fidelity=HeroFrameProductFidelityInstruction(),
        factual_grounding=HeroFrameFactualGroundingInstruction(),
        added_text=HeroFrameAddedTextInstruction(),
        primary_reference=_reference_instruction(
            validated_request.primary_reference,
            "primary_product_identity",
        ),
        supporting_references=supporting_references,
        output=HeroFrameOutputInstruction(
            aspect_ratio=validated_request.aspect_ratio,
        ),
    )
