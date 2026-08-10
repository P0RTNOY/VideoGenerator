"""Explicit verified product-asset selections for Storyboard shots."""

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

from pydantic import Field, ValidationError, model_validator

from campaign import AdCampaign
from product import NonEmptyStr, Product, ProductModel
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import (
    DerivedAssetSourceLinkError,
    ProductDerivedAssetManifest,
    validate_product_derived_asset_sources,
)
from product_image_downloads import (
    DownloadedProductImage,
    ImageIntegrityError,
    Sha256Hex,
)
from product_image_sets import ProductImageSetError, build_verified_product_image_set
from product_reference_sheets import (
    ProductReferenceSheet,
    ReferenceSheetIntegrityError,
    verify_product_reference_sheet,
)
from storyboard import Storyboard


AssetKind = Literal["original_image", "reference_sheet"]
AssetFormat = Literal["JPEG", "PNG", "WEBP"]

_REFERENCE_SHEET_ID_PATTERN = re.compile(r"^reference-sheet:[0-9a-f]{64}$")


class SelectedAssetSnapshot(ProductModel):
    """Path-free logical identity and selection-time binary metadata."""

    asset_id: NonEmptyStr
    asset_kind: AssetKind
    sha256_at_selection: Sha256Hex
    width_at_selection: int = Field(gt=0, strict=True)
    height_at_selection: int = Field(gt=0, strict=True)
    format_at_selection: AssetFormat

    @model_validator(mode="after")
    def require_kind_specific_asset_id(self) -> "SelectedAssetSnapshot":
        if self.asset_kind == "original_image":
            if not self.asset_id.startswith("original:") or not self.asset_id[9:]:
                raise ValueError(
                    "original image asset_id must use original:<source_image_id>"
                )
        elif not _REFERENCE_SHEET_ID_PATTERN.fullmatch(self.asset_id):
            raise ValueError(
                "reference sheet asset_id must use reference-sheet:<sha256>"
            )
        return self


class ShotAssetSelection(ProductModel):
    """One shot's explicit primary and ordered supporting references."""

    shot_id: NonEmptyStr
    primary_asset: SelectedAssetSnapshot
    supporting_assets: List[SelectedAssetSnapshot] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_original_primary_and_unique_assets(self) -> "ShotAssetSelection":
        if self.primary_asset.asset_kind != "original_image":
            raise ValueError("primary_asset must be an original product image")

        all_assets = [self.primary_asset, *self.supporting_assets]
        asset_ids = [asset.asset_id for asset in all_assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("shot assets must not contain duplicate asset IDs")
        hashes = [asset.sha256_at_selection for asset in all_assets]
        if len(hashes) != len(set(hashes)):
            raise ValueError("shot assets must not contain duplicate binary SHA-256")
        return self


class StoryboardAssetPlan(ProductModel):
    """Product-, Campaign-, and Storyboard-bound shot asset selections."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    shot_selections: List[ShotAssetSelection] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_shot_selections(self) -> "StoryboardAssetPlan":
        shot_ids = [selection.shot_id for selection in self.shot_selections]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("shot_selections must contain unique shot IDs")
        return self


class AssetPlanContextError(ValueError):
    """Raised when a plan cannot be validated against current domain context."""


class UnknownSelectedAssetError(AssetPlanContextError):
    """Raised when a selected logical asset ID no longer resolves."""


class SelectedAssetMismatchError(AssetPlanContextError):
    """Raised when current verified metadata differs from its snapshot."""


class SelectedAssetIntegrityError(AssetPlanContextError):
    """Raised when selected or provenance-required bytes fail verification."""


class SelectedAssetProvenanceError(AssetPlanContextError):
    """Raised when a selected asset cannot link to current Product provenance."""


def original_asset_id(asset: DownloadedProductImage) -> str:
    """Return the path-independent logical identity for an original image."""

    return f"original:{asset.source_image_id}"


def _reference_sheet_derivation_descriptor(
    sheet: ProductReferenceSheet,
) -> dict:
    return {
        "descriptor_version": 1,
        "product_id": sheet.product_id,
        "width": sheet.width,
        "height": sheet.height,
        "format": sheet.format,
        "tiles": [
            {
                "index": tile.index,
                "source_image_id": tile.source_image_id,
                "source_sha256": tile.source_sha256,
                "x": tile.x,
                "y": tile.y,
                "width": tile.width,
                "height": tile.height,
                "rendered_x": tile.rendered_x,
                "rendered_y": tile.rendered_y,
                "rendered_width": tile.rendered_width,
                "rendered_height": tile.rendered_height,
            }
            for tile in sheet.tiles
        ],
    }


def reference_sheet_asset_id(sheet: ProductReferenceSheet) -> str:
    """Hash a deterministic, path-free reference-sheet derivation descriptor."""

    canonical_json = json.dumps(
        _reference_sheet_derivation_descriptor(sheet),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"reference-sheet:{digest}"


def snapshot_original_asset(
    asset: DownloadedProductImage,
) -> SelectedAssetSnapshot:
    """Copy minimal selection metadata from one original asset record."""

    return SelectedAssetSnapshot(
        asset_id=original_asset_id(asset),
        asset_kind="original_image",
        sha256_at_selection=asset.sha256,
        width_at_selection=asset.width,
        height_at_selection=asset.height,
        format_at_selection=asset.format,
    )


def snapshot_reference_sheet(
    sheet: ProductReferenceSheet,
) -> SelectedAssetSnapshot:
    """Copy minimal selection metadata from one reference-sheet record."""

    return SelectedAssetSnapshot(
        asset_id=reference_sheet_asset_id(sheet),
        asset_kind="reference_sheet",
        sha256_at_selection=sheet.sha256,
        width_at_selection=sheet.width,
        height_at_selection=sheet.height,
        format_at_selection=sheet.format,
    )


def _require_snapshot_match(
    selected: SelectedAssetSnapshot,
    current: SelectedAssetSnapshot,
) -> None:
    if selected.sha256_at_selection != current.sha256_at_selection:
        raise SelectedAssetMismatchError(
            f"selected asset SHA-256 changed: {selected.asset_id}"
        )
    if (
        selected.width_at_selection != current.width_at_selection
        or selected.height_at_selection != current.height_at_selection
        or selected.format_at_selection != current.format_at_selection
    ):
        raise SelectedAssetMismatchError(
            f"selected asset dimensions or format changed: {selected.asset_id}"
        )


def validate_storyboard_asset_plan(
    plan: StoryboardAssetPlan,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    original_manifest: ProductAssetManifest,
    derived_manifest: Optional[ProductDerivedAssetManifest] = None,
) -> StoryboardAssetPlan:
    """Validate selected assets against current manifests without mutation."""

    try:
        validated_plan = StoryboardAssetPlan.model_validate(plan.model_dump())
        validated_product = Product.model_validate(product.model_dump())
        validated_campaign = AdCampaign.model_validate(campaign.model_dump())
        validated_storyboard = Storyboard.model_validate(storyboard.model_dump())
        validated_original_manifest = ProductAssetManifest.model_validate(
            original_manifest.model_dump()
        )
    except ValidationError as error:
        raise AssetPlanContextError(
            f"asset-plan context must be structurally valid: {error}"
        ) from error

    product_id = validated_plan.product_id
    if product_id != validated_product.id:
        raise AssetPlanContextError(
            f"plan Product {product_id} does not match Product {validated_product.id}"
        )
    if validated_campaign.claim_selection.product_id != product_id:
        raise AssetPlanContextError(
            "Campaign claim selection does not belong to the plan Product"
        )
    if validated_original_manifest.product_id != product_id:
        raise AssetPlanContextError(
            "original asset manifest does not belong to the plan Product"
        )
    if validated_plan.campaign_id != validated_campaign.id:
        raise AssetPlanContextError(
            "plan campaign_id does not match the supplied Campaign"
        )
    if validated_plan.storyboard_id != validated_storyboard.id:
        raise AssetPlanContextError(
            "plan storyboard_id does not match the supplied Storyboard"
        )
    if validated_storyboard.campaign_id != validated_campaign.id:
        raise AssetPlanContextError(
            "Storyboard does not belong to the supplied Campaign"
        )

    storyboard_shot_ids = {shot.id for shot in validated_storyboard.shots}
    for selection in validated_plan.shot_selections:
        if selection.shot_id not in storyboard_shot_ids:
            raise AssetPlanContextError(
                f"unknown Storyboard shot ID: {selection.shot_id}"
            )

    selected_snapshots = [
        asset
        for selection in validated_plan.shot_selections
        for asset in [selection.primary_asset, *selection.supporting_assets]
    ]
    original_records_by_id: Dict[str, DownloadedProductImage] = {
        original_asset_id(asset): asset
        for asset in validated_original_manifest.images
    }
    verified_originals: Dict[str, DownloadedProductImage] = {}

    def verify_original(
        asset_id: str,
        *,
        provenance_only: bool = False,
    ) -> DownloadedProductImage:
        cached = verified_originals.get(asset_id)
        if cached is not None:
            return cached
        current = original_records_by_id.get(asset_id)
        if current is None:
            error_type = (
                SelectedAssetProvenanceError
                if provenance_only
                else UnknownSelectedAssetError
            )
            raise error_type(f"unknown original asset ID: {asset_id}")
        selected_manifest = ProductAssetManifest(
            product_id=validated_original_manifest.product_id,
            images=[current.model_copy(deep=True)],
        )
        try:
            verified_set = build_verified_product_image_set(
                validated_product,
                selected_manifest,
            )
        except ImageIntegrityError as error:
            raise SelectedAssetIntegrityError(
                f"original asset failed integrity verification: {asset_id}"
            ) from error
        except ProductImageSetError as error:
            raise SelectedAssetProvenanceError(
                f"original asset failed Product linkage: {asset_id}"
            ) from error
        if len(verified_set) != 1:
            raise SelectedAssetProvenanceError(
                f"original asset was not admitted to the verified image set: {asset_id}"
            )
        verified = verified_set[0]
        verified_originals[asset_id] = verified
        return verified

    for snapshot in selected_snapshots:
        if snapshot.asset_kind != "original_image":
            continue
        current_original = verify_original(snapshot.asset_id)
        _require_snapshot_match(
            snapshot,
            snapshot_original_asset(current_original),
        )

    selected_sheet_snapshots = [
        snapshot
        for snapshot in selected_snapshots
        if snapshot.asset_kind == "reference_sheet"
    ]
    if selected_sheet_snapshots:
        if derived_manifest is None:
            raise AssetPlanContextError(
                "a derived manifest is required for selected reference sheets"
            )
        try:
            validated_derived_manifest = ProductDerivedAssetManifest.model_validate(
                derived_manifest.model_dump()
            )
        except ValidationError as error:
            raise AssetPlanContextError(
                f"derived manifest must be structurally valid: {error}"
            ) from error
        if validated_derived_manifest.product_id != product_id:
            raise AssetPlanContextError(
                "derived manifest does not belong to the plan Product"
            )

        sheets_by_id: Dict[str, List[ProductReferenceSheet]] = {}
        for sheet in validated_derived_manifest.reference_sheets:
            asset_id = reference_sheet_asset_id(sheet)
            sheets_by_id.setdefault(asset_id, []).append(sheet)

        for snapshot in selected_sheet_snapshots:
            matching_sheets = sheets_by_id.get(snapshot.asset_id, [])
            if not matching_sheets:
                raise UnknownSelectedAssetError(
                    f"unknown reference-sheet asset ID: {snapshot.asset_id}"
                )
            if len(matching_sheets) > 1:
                raise AssetPlanContextError(
                    "selected reference-sheet asset ID is ambiguous: "
                    f"{snapshot.asset_id}"
                )
            current_sheet = matching_sheets[0]
            selected_derived_manifest = ProductDerivedAssetManifest(
                product_id=validated_derived_manifest.product_id,
                reference_sheets=[current_sheet.model_copy(deep=True)],
            )
            try:
                validate_product_derived_asset_sources(
                    selected_derived_manifest,
                    validated_original_manifest,
                )
            except DerivedAssetSourceLinkError as error:
                raise SelectedAssetProvenanceError(
                    f"reference sheet failed original provenance: {snapshot.asset_id}"
                ) from error
            for tile in current_sheet.tiles:
                verify_original(
                    f"original:{tile.source_image_id}",
                    provenance_only=True,
                )
            try:
                verified_sheet = verify_product_reference_sheet(current_sheet)
            except ReferenceSheetIntegrityError as error:
                raise SelectedAssetIntegrityError(
                    f"reference sheet failed integrity verification: {snapshot.asset_id}"
                ) from error
            _require_snapshot_match(
                snapshot,
                snapshot_reference_sheet(verified_sheet),
            )

    return validated_plan.model_copy(deep=True)


def save_storyboard_asset_plan(
    plan: StoryboardAssetPlan,
    path: Union[str, Path] = "storyboard_asset_plan.json",
) -> Path:
    """Serialize a StoryboardAssetPlan as readable UTF-8 JSON."""

    output_path = Path(path)
    json_text = plan.model_dump_json(
        indent=2,
        ensure_ascii=False,
        exclude_none=True,
    )
    output_path.write_text(json_text + "\n", encoding="utf-8")
    return output_path


def load_storyboard_asset_plan(
    path: Union[str, Path] = "storyboard_asset_plan.json",
) -> StoryboardAssetPlan:
    """Load and structurally validate a StoryboardAssetPlan from UTF-8 JSON."""

    input_path = Path(path)
    return StoryboardAssetPlan.model_validate_json(
        input_path.read_text(encoding="utf-8")
    )
