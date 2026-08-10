"""Provider-neutral runtime requests for one prepared Storyboard shot."""

import hashlib
import io
from pathlib import Path
from typing import Dict, List, Literal, Optional

from PIL import Image, UnidentifiedImageError
from pydantic import Field, ValidationError, model_validator

from campaign import (
    AdCampaign,
    AspectRatio,
    CampaignClaimSnapshot,
    CreativeStyle,
)
from product import NonEmptyStr, Product, ProductModel, ProductVariant
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from product_image_downloads import DownloadedProductImage, Sha256Hex
from product_reference_sheets import ProductReferenceSheet
from storyboard import (
    ShotPurpose,
    Storyboard,
    StoryboardContextError,
    validate_storyboard,
)
from storyboard_asset_plan import (
    AssetFormat,
    AssetKind,
    AssetPlanContextError,
    SelectedAssetIntegrityError,
    SelectedAssetMismatchError,
    SelectedAssetProvenanceError,
    StoryboardAssetPlan,
    UnknownSelectedAssetError,
    original_asset_id,
    reference_sheet_asset_id,
    validate_storyboard_asset_plan,
)


ProductFidelityPolicy = Literal["preserve_exact_product"]
AddedTextPolicy = Literal["no_added_text"]
FactualGroundingPolicy = Literal["selected_claims_only"]


class ResolvedProductIdentity(ProductModel):
    """Minimal Product identity required at the generation boundary."""

    product_id: NonEmptyStr
    name: NonEmptyStr
    brand: NonEmptyStr
    category: NonEmptyStr
    variant: Optional[ProductVariant] = None


class ResolvedRuntimeAsset(ProductModel):
    """Current verified local asset handle for a future provider adapter."""

    asset_id: NonEmptyStr
    asset_kind: AssetKind
    local_path: Path
    sha256: Sha256Hex
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    format: AssetFormat


class HeroFrameCreativeDirection(ProductModel):
    """Authored scene intent that is explicitly not a product-fact source."""

    shot_purpose: ShotPurpose
    environment: NonEmptyStr
    scene_direction: NonEmptyStr
    camera_direction: Optional[NonEmptyStr] = None
    creative_style: CreativeStyle
    creative_brief: Optional[NonEmptyStr] = None


class HeroFrameRequest(ProductModel):
    """Ephemeral, provider-neutral input for one prepared Hero Frame."""

    product: ResolvedProductIdentity
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    factual_claims: List[CampaignClaimSnapshot] = Field(default_factory=list)
    creative_direction: HeroFrameCreativeDirection
    aspect_ratio: AspectRatio
    product_fidelity: ProductFidelityPolicy = "preserve_exact_product"
    added_text_policy: AddedTextPolicy = "no_added_text"
    factual_grounding: FactualGroundingPolicy = "selected_claims_only"
    primary_reference: ResolvedRuntimeAsset
    supporting_references: List[ResolvedRuntimeAsset] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_original_primary_and_unique_references(self) -> "HeroFrameRequest":
        if self.primary_reference.asset_kind != "original_image":
            raise ValueError("primary_reference must be an original product image")
        references = [self.primary_reference, *self.supporting_references]
        asset_ids = [asset.asset_id for asset in references]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("Hero Frame references must have unique asset IDs")
        hashes = [asset.sha256 for asset in references]
        if len(hashes) != len(set(hashes)):
            raise ValueError("Hero Frame references must have unique binary SHA-256")
        return self


class HeroFrameRequestError(ValueError):
    """Base error for controlled Hero Frame request preparation failures."""


class HeroFrameRequestContextError(HeroFrameRequestError):
    """Raised when durable domain inputs do not form a valid request context."""


class UnknownHeroFrameShotError(HeroFrameRequestError):
    """Raised when the requested shot does not exist in the Storyboard."""


class ShotNotPreparedError(HeroFrameRequestError):
    """Raised when the requested shot has no explicit asset selection."""


class HeroFrameAssetResolutionError(HeroFrameRequestError):
    """Raised when a validated selected asset cannot be resolved at runtime."""


class ConsumerAssetIntegrityError(HeroFrameRequestError):
    """Raised when runtime asset bytes change or cannot be decoded."""


def _resolved_original(asset: DownloadedProductImage) -> ResolvedRuntimeAsset:
    return ResolvedRuntimeAsset(
        asset_id=original_asset_id(asset),
        asset_kind="original_image",
        local_path=Path(asset.local_path),
        sha256=asset.sha256,
        width=asset.width,
        height=asset.height,
        format=asset.format,
    )


def _resolved_reference_sheet(
    sheet: ProductReferenceSheet,
) -> ResolvedRuntimeAsset:
    return ResolvedRuntimeAsset(
        asset_id=reference_sheet_asset_id(sheet),
        asset_kind="reference_sheet",
        local_path=Path(sheet.local_path),
        sha256=sheet.sha256,
        width=sheet.width,
        height=sheet.height,
        format=sheet.format,
    )


def _resolve_asset(
    asset_id: str,
    asset_kind: AssetKind,
    originals: Dict[str, DownloadedProductImage],
    sheets: Dict[str, ProductReferenceSheet],
) -> ResolvedRuntimeAsset:
    if asset_kind == "original_image":
        original = originals.get(asset_id)
        if original is not None:
            return _resolved_original(original)
    else:
        sheet = sheets.get(asset_id)
        if sheet is not None:
            return _resolved_reference_sheet(sheet)
    raise HeroFrameAssetResolutionError(
        f"validated selected asset could not be resolved: {asset_id}"
    )


def build_hero_frame_request(
    *,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    asset_plan: StoryboardAssetPlan,
    shot_id: str,
    original_manifest: ProductAssetManifest,
    derived_manifest: Optional[ProductDerivedAssetManifest] = None,
) -> HeroFrameRequest:
    """Resolve one prepared shot after rechecking current context and assets."""

    try:
        validated_product = Product.model_validate(product.model_dump())
        validated_campaign = AdCampaign.model_validate(campaign.model_dump())
        validated_storyboard = Storyboard.model_validate(storyboard.model_dump())
        validated_plan = StoryboardAssetPlan.model_validate(asset_plan.model_dump())
    except ValidationError as error:
        raise HeroFrameRequestContextError(
            f"Hero Frame request inputs must be structurally valid: {error}"
        ) from error

    try:
        validated_storyboard = validate_storyboard(
            validated_storyboard,
            validated_campaign,
        )
    except StoryboardContextError as error:
        raise HeroFrameRequestContextError(str(error)) from error

    try:
        validated_original_manifest = ProductAssetManifest.model_validate(
            original_manifest.model_dump()
        )
        validated_derived_manifest = (
            ProductDerivedAssetManifest.model_validate(derived_manifest.model_dump())
            if derived_manifest is not None
            else None
        )
    except ValidationError as error:
        raise HeroFrameRequestContextError(
            f"Hero Frame manifests must be structurally valid: {error}"
        ) from error
    if validated_original_manifest.product_id != validated_product.id:
        raise HeroFrameRequestContextError(
            "original asset manifest does not belong to the Product"
        )
    if (
        validated_derived_manifest is not None
        and validated_derived_manifest.product_id != validated_product.id
    ):
        raise HeroFrameRequestContextError(
            "derived asset manifest does not belong to the Product"
        )
    if validated_plan.product_id != validated_product.id:
        raise HeroFrameRequestContextError(
            "asset plan does not belong to the Product"
        )
    if validated_campaign.claim_selection.product_id != validated_product.id:
        raise HeroFrameRequestContextError(
            "Campaign does not belong to the Product"
        )
    if validated_plan.campaign_id != validated_campaign.id:
        raise HeroFrameRequestContextError(
            "asset plan does not belong to the Campaign"
        )
    if validated_plan.storyboard_id != validated_storyboard.id:
        raise HeroFrameRequestContextError(
            "asset plan does not belong to the Storyboard"
        )
    storyboard_shot_ids = {item.id for item in validated_storyboard.shots}
    unknown_plan_shot_ids = [
        item.shot_id
        for item in validated_plan.shot_selections
        if item.shot_id not in storyboard_shot_ids
    ]
    if unknown_plan_shot_ids:
        raise HeroFrameRequestContextError(
            "asset plan references unknown Storyboard shot IDs: "
            + ", ".join(unknown_plan_shot_ids)
        )

    matching_shots = [
        shot for shot in validated_storyboard.shots if shot.id == shot_id
    ]
    if not matching_shots:
        raise UnknownHeroFrameShotError(f"unknown Storyboard shot ID: {shot_id}")
    if len(matching_shots) != 1:
        raise HeroFrameRequestContextError(
            f"Storyboard shot ID is ambiguous: {shot_id}"
        )
    shot = matching_shots[0]

    matching_selections = [
        selection
        for selection in validated_plan.shot_selections
        if selection.shot_id == shot_id
    ]
    if not matching_selections:
        raise ShotNotPreparedError(
            f"Storyboard shot has no prepared asset selection: {shot_id}"
        )
    if len(matching_selections) != 1:
        raise HeroFrameRequestContextError(
            f"asset plan shot selection is ambiguous: {shot_id}"
        )

    try:
        validated_plan = validate_storyboard_asset_plan(
            validated_plan,
            validated_product,
            validated_campaign,
            validated_storyboard,
            validated_original_manifest,
            validated_derived_manifest,
        )
    except (
        SelectedAssetIntegrityError,
        SelectedAssetMismatchError,
        SelectedAssetProvenanceError,
        UnknownSelectedAssetError,
    ):
        raise
    except AssetPlanContextError as error:
        raise HeroFrameRequestContextError(str(error)) from error

    selection = next(
        item for item in validated_plan.shot_selections if item.shot_id == shot_id
    )

    originals = {
        original_asset_id(asset): asset
        for asset in validated_original_manifest.images
    }
    sheets = {
        reference_sheet_asset_id(sheet): sheet
        for sheet in (
            validated_derived_manifest.reference_sheets
            if validated_derived_manifest is not None
            else []
        )
    }

    snapshots_by_id = {
        snapshot.claim_id: snapshot
        for snapshot in validated_campaign.claim_snapshots
    }
    try:
        factual_claims = [
            snapshots_by_id[claim_id].model_copy(deep=True)
            for claim_id in shot.selected_claim_ids
        ]
    except KeyError as error:
        raise HeroFrameRequestContextError(
            f"missing Campaign claim snapshot for shot claim: {error.args[0]}"
        ) from error

    primary_reference = _resolve_asset(
        selection.primary_asset.asset_id,
        selection.primary_asset.asset_kind,
        originals,
        sheets,
    )
    supporting_references = [
        _resolve_asset(
            snapshot.asset_id,
            snapshot.asset_kind,
            originals,
            sheets,
        )
        for snapshot in selection.supporting_assets
    ]

    return HeroFrameRequest(
        product=ResolvedProductIdentity(
            product_id=validated_product.id,
            name=validated_product.name,
            brand=validated_product.brand,
            category=validated_product.category,
            variant=(
                validated_product.variant.model_copy(deep=True)
                if validated_product.variant is not None
                else None
            ),
        ),
        campaign_id=validated_campaign.id,
        storyboard_id=validated_storyboard.id,
        asset_plan_id=validated_plan.id,
        shot_id=shot.id,
        factual_claims=factual_claims,
        creative_direction=HeroFrameCreativeDirection(
            shot_purpose=shot.purpose,
            environment=validated_campaign.environment,
            scene_direction=shot.visual_description,
            camera_direction=shot.camera_direction,
            creative_style=validated_campaign.creative_style,
            creative_brief=validated_campaign.creative_brief,
        ),
        aspect_ratio=validated_campaign.aspect_ratio,
        primary_reference=primary_reference,
        supporting_references=supporting_references,
    )


def read_verified_runtime_asset_bytes(asset: ResolvedRuntimeAsset) -> bytes:
    """Read once and verify the exact bytes a future adapter will upload."""

    try:
        validated_asset = ResolvedRuntimeAsset.model_validate(asset.model_dump())
    except ValidationError as error:
        raise ConsumerAssetIntegrityError(
            f"runtime asset metadata is invalid: {error}"
        ) from error
    try:
        data = validated_asset.local_path.read_bytes()
    except OSError as error:
        raise ConsumerAssetIntegrityError(
            f"runtime asset could not be read: {validated_asset.asset_id}"
        ) from error

    if hashlib.sha256(data).hexdigest() != validated_asset.sha256:
        raise ConsumerAssetIntegrityError(
            f"runtime asset SHA-256 changed: {validated_asset.asset_id}"
        )
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            decoded_format = image.format
            decoded_size = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise ConsumerAssetIntegrityError(
            f"runtime asset could not be decoded: {validated_asset.asset_id}"
        ) from error
    if decoded_format != validated_asset.format:
        raise ConsumerAssetIntegrityError(
            f"runtime asset format changed: {validated_asset.asset_id}"
        )
    if decoded_size != (validated_asset.width, validated_asset.height):
        raise ConsumerAssetIntegrityError(
            f"runtime asset dimensions changed: {validated_asset.asset_id}"
        )
    return data
