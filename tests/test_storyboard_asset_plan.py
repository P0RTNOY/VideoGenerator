import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from ad_claims import ProductAdClaimSelection
from campaign import AdCampaign
from demo_campaign_storyboard import build_soundcore_campaign_storyboard
from product import Product, ProductImage
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from product_image_downloads import DownloadedProductImage
from product_reference_sheets import (
    ProductReferenceSheet,
    ProductReferenceSheetTile,
)
from storyboard_asset_plan import (
    AssetPlanContextError,
    SelectedAssetSnapshot,
    SelectedAssetIntegrityError,
    SelectedAssetMismatchError,
    SelectedAssetProvenanceError,
    ShotAssetSelection,
    StoryboardAssetPlan,
    UnknownSelectedAssetError,
    load_storyboard_asset_plan,
    original_asset_id,
    reference_sheet_asset_id,
    save_storyboard_asset_plan,
    snapshot_original_asset,
    snapshot_reference_sheet,
    validate_storyboard_asset_plan,
)
from storyboard import Storyboard, StoryboardShot


FIXED_TIME = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def downloaded_asset(
    source_image_id: str = "image-front",
    *,
    product_id: str = "example-product",
    local_path: str = "/movable/original.png",
    sha256: str = "a" * 64,
    width: int = 1200,
    height: int = 900,
    image_format: str = "PNG",
) -> DownloadedProductImage:
    mime_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }[image_format]
    extension = image_format.casefold()
    return DownloadedProductImage(
        product_id=product_id,
        source_image_id=source_image_id,
        source_url=f"https://cdn.example/{source_image_id}.{extension}",
        final_url=f"https://media.example/{source_image_id}.{extension}",
        local_path=local_path,
        sha256=sha256,
        byte_size=100,
        width=width,
        height=height,
        format=image_format,
        mime_type=mime_type,
        downloaded_at=FIXED_TIME,
    )


def reference_sheet(
    sources=None,
    *,
    product_id: str = "example-product",
    local_path: str = "/movable/reference-sheet.png",
    sha256: str = "f" * 64,
    created_at: datetime = FIXED_TIME,
) -> ProductReferenceSheet:
    active_sources = sources or [downloaded_asset()]
    tile_width = 200 // len(active_sources)
    tiles = []
    for index, source in enumerate(active_sources):
        tiles.append(
            ProductReferenceSheetTile(
                index=index,
                source_image_id=source.source_image_id,
                source_sha256=source.sha256,
                source_local_path=source.local_path,
                x=index * tile_width,
                y=0,
                width=tile_width,
                height=100,
                rendered_x=index * tile_width,
                rendered_y=0,
                rendered_width=tile_width,
                rendered_height=100,
            )
        )
    return ProductReferenceSheet(
        product_id=product_id,
        local_path=local_path,
        width=200,
        height=100,
        format="PNG",
        sha256=sha256,
        byte_size=200,
        created_at=created_at,
        tiles=tiles,
    )


def valid_plan() -> StoryboardAssetPlan:
    primary = snapshot_original_asset(downloaded_asset())
    supporting = snapshot_reference_sheet(reference_sheet())
    return StoryboardAssetPlan(
        id="example-assets",
        product_id="example-product",
        campaign_id="example-campaign",
        storyboard_id="example-storyboard",
        shot_selections=[
            ShotAssetSelection(
                shot_id="hook",
                primary_asset=primary,
                supporting_assets=[supporting],
            )
        ],
    )


def encoded_image_bytes(
    image_format: str,
    size=(24, 18),
    color=(20, 100, 180),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def stored_original(
    root: Path,
    source_image_id: str,
    *,
    product_id: str = "example-product",
    image_format: str = "PNG",
    size=(24, 18),
    color=(20, 100, 180),
):
    data = encoded_image_bytes(image_format, size=size, color=color)
    extension = image_format.casefold()
    local_path = root / f"{source_image_id}.{extension}"
    local_path.write_bytes(data)
    asset = DownloadedProductImage(
        product_id=product_id,
        source_image_id=source_image_id,
        source_url=f"https://cdn.example/{source_image_id}.{extension}",
        final_url=f"https://media.example/{source_image_id}.{extension}",
        local_path=str(local_path),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        width=size[0],
        height=size[1],
        format=image_format,
        mime_type={
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
        }[image_format],
        downloaded_at=FIXED_TIME,
    )
    product_image = ProductImage(
        id=source_image_id,
        url=asset.source_url,
        source_url="https://example.com/product",
        source_type="json_ld_product_image",
        width=size[0],
        height=size[1],
        confidence=0.9,
    )
    return product_image, asset


def stored_reference_sheet(
    root: Path,
    sources,
    *,
    product_id: str = "example-product",
) -> ProductReferenceSheet:
    data = encoded_image_bytes("PNG", size=(200, 100), color=(240, 240, 240))
    local_path = root / "reference-sheet.png"
    local_path.write_bytes(data)
    sheet = reference_sheet(
        sources,
        product_id=product_id,
        local_path=str(local_path),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return sheet.model_copy(update={"byte_size": len(data)}, deep=True)


class StoryboardAssetPlanModelTests(unittest.TestCase):
    def test_snapshot_contains_only_approved_strict_metadata(self) -> None:
        asset = downloaded_asset()

        snapshot = snapshot_original_asset(asset)

        self.assertEqual(
            snapshot.model_dump(),
            {
                "asset_id": "original:image-front",
                "asset_kind": "original_image",
                "sha256_at_selection": "a" * 64,
                "width_at_selection": 1200,
                "height_at_selection": 900,
                "format_at_selection": "PNG",
            },
        )
        for field, value in (
            ("local_path", "/tmp/image.png"),
            ("url", "https://example/image.png"),
            ("provider", "image-provider"),
            ("prompt", "make an ad"),
            ("licensed", True),
        ):
            malformed = snapshot.model_dump()
            malformed[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    SelectedAssetSnapshot.model_validate(malformed)

    def test_snapshot_rejects_malformed_identity_hash_dimensions_and_format(self) -> None:
        valid = snapshot_original_asset(downloaded_asset()).model_dump()
        invalid_updates = (
            {"asset_id": "image-front"},
            {"asset_kind": "reference_sheet"},
            {"sha256_at_selection": "short"},
            {"width_at_selection": 0},
            {"height_at_selection": True},
            {"format_at_selection": "GIF"},
        )
        for update in invalid_updates:
            with self.subTest(update=update):
                with self.assertRaises(ValidationError):
                    SelectedAssetSnapshot.model_validate({**valid, **update})

    def test_shot_selection_requires_one_original_primary(self) -> None:
        primary = snapshot_original_asset(downloaded_asset())
        sheet = snapshot_reference_sheet(reference_sheet())

        selection = ShotAssetSelection(
            shot_id="hook",
            primary_asset=primary,
        )

        self.assertEqual(selection.supporting_assets, [])
        with self.assertRaises(ValidationError):
            ShotAssetSelection.model_validate({"shot_id": "hook"})
        with self.assertRaises(ValidationError):
            ShotAssetSelection(
                shot_id="hook",
                primary_asset=sheet,
            )

    def test_shot_selection_rejects_duplicate_ids_and_binary_content(self) -> None:
        primary_asset = downloaded_asset()
        primary = snapshot_original_asset(primary_asset)
        same_primary = primary.model_copy(deep=True)
        alias = snapshot_original_asset(
            downloaded_asset(
                "image-alias",
                local_path=primary_asset.local_path,
                sha256=primary_asset.sha256,
            )
        )
        unique = snapshot_original_asset(
            downloaded_asset(
                "image-rear",
                sha256="b" * 64,
                local_path="/movable/rear.png",
            )
        )

        for supporting in (
            [same_primary],
            [unique, unique.model_copy(deep=True)],
            [alias],
        ):
            with self.subTest(supporting=supporting):
                with self.assertRaises(ValidationError):
                    ShotAssetSelection(
                        shot_id="hook",
                        primary_asset=primary,
                        supporting_assets=supporting,
                    )

    def test_supporting_asset_order_is_preserved_without_a_maximum(self) -> None:
        supporting = [
            snapshot_original_asset(
                downloaded_asset(
                    f"image-{index}",
                    sha256=f"{index:x}" * 64,
                    local_path=f"/movable/{index}.png",
                )
            )
            for index in range(1, 9)
        ]

        selection = ShotAssetSelection(
            shot_id="hook",
            primary_asset=snapshot_original_asset(downloaded_asset()),
            supporting_assets=supporting,
        )

        self.assertEqual(
            [item.asset_id for item in selection.supporting_assets],
            [item.asset_id for item in supporting],
        )

    def test_plan_allows_empty_and_partial_selections_but_rejects_duplicate_shots(self) -> None:
        empty = StoryboardAssetPlan(
            id="empty-assets",
            product_id="example-product",
            campaign_id="example-campaign",
            storyboard_id="example-storyboard",
        )
        partial = valid_plan()

        self.assertEqual(empty.shot_selections, [])
        self.assertEqual([item.shot_id for item in partial.shot_selections], ["hook"])
        with self.assertRaises(ValidationError):
            StoryboardAssetPlan(
                **{
                    **partial.model_dump(exclude={"shot_selections"}),
                    "shot_selections": [
                        partial.shot_selections[0],
                        partial.shot_selections[0].model_copy(deep=True),
                    ],
                }
            )

    def test_original_identity_is_stable_across_path_and_metadata_changes(self) -> None:
        original = downloaded_asset()
        moved = original.model_copy(
            update={
                "local_path": "/another/root/original.png",
                "source_url": "https://another.example/source.png",
                "downloaded_at": FIXED_TIME + timedelta(days=1),
            },
            deep=True,
        )

        self.assertEqual(original_asset_id(original), "original:image-front")
        self.assertEqual(original_asset_id(moved), original_asset_id(original))

    def test_reference_sheet_identity_is_path_timestamp_and_binary_independent(self) -> None:
        original = reference_sheet()
        moved = original.model_copy(
            update={
                "local_path": "/another/root/reference-sheet.png",
                "created_at": FIXED_TIME + timedelta(days=2),
                "byte_size": 999,
                "sha256": "e" * 64,
                "tiles": [
                    tile.model_copy(
                        update={"source_local_path": "/another/root/source.png"},
                        deep=True,
                    )
                    for tile in original.tiles
                ],
            },
            deep=True,
        )

        identity = reference_sheet_asset_id(original)

        self.assertRegex(identity, r"^reference-sheet:[0-9a-f]{64}$")
        self.assertEqual(reference_sheet_asset_id(moved), identity)
        self.assertNotEqual(
            snapshot_reference_sheet(original).sha256_at_selection,
            snapshot_reference_sheet(moved).sha256_at_selection,
        )

    def test_reference_sheet_identity_changes_with_source_order_or_geometry(self) -> None:
        front = downloaded_asset("image-front", sha256="a" * 64)
        rear = downloaded_asset(
            "image-rear",
            sha256="b" * 64,
            local_path="/movable/rear.png",
        )
        original = reference_sheet([front, rear])
        reordered = reference_sheet([rear, front])
        changed_source = original.model_copy(deep=True)
        changed_source.tiles[0].source_image_id = "image-other"
        changed_geometry = original.model_copy(deep=True)
        changed_geometry.tiles[0].rendered_width -= 1

        identities = {
            reference_sheet_asset_id(sheet)
            for sheet in (
                original,
                reordered,
                changed_source,
                changed_geometry,
            )
        }

        self.assertEqual(len(identities), 4)

    def test_json_persistence_is_human_readable_structural_and_path_free(self) -> None:
        original = valid_plan()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "storyboard-assets.json"
            returned = save_storyboard_asset_plan(original, path)
            raw = path.read_text(encoding="utf-8")
            restored = load_storyboard_asset_plan(path)

            self.assertEqual(returned, path)
            self.assertEqual(restored, original)
            self.assertIn('\n  "schema_version": 1', raw)
            self.assertTrue(raw.endswith("\n"))
            self.assertNotIn("/movable/", raw)
            self.assertNotIn("https://", raw)

            malformed = json.loads(raw)
            malformed["operation_id"] = "provider-operation"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_storyboard_asset_plan(path)


class StoryboardAssetPlanContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        front_image, self.front = stored_original(
            self.root,
            "image-front",
            color=(200, 20, 20),
        )
        three_quarter_image, self.three_quarter = stored_original(
            self.root,
            "image-three-quarter",
            color=(20, 200, 20),
        )
        rear_image, self.rear = stored_original(
            self.root,
            "image-rear",
            color=(20, 20, 200),
        )
        self.product = Product(
            id="example-product",
            name="Example Speaker",
            brand="Example",
            category="Portable Speaker",
            images=[rear_image, front_image, three_quarter_image],
        )
        self.original_manifest = ProductAssetManifest(
            product_id=self.product.id,
            images=[self.front, self.three_quarter, self.rear],
        )
        self.sheet = stored_reference_sheet(
            self.root,
            [self.front, self.rear],
        )
        self.derived_manifest = ProductDerivedAssetManifest(
            product_id=self.product.id,
            reference_sheets=[self.sheet],
        )
        self.campaign = AdCampaign(
            id="example-campaign",
            claim_selection=ProductAdClaimSelection(
                product_id=self.product.id,
                selected_claim_ids=[],
            ),
            ad_format="vertical_social",
            aspect_ratio="9:16",
            target_duration_seconds=8,
            creative_style="ugc",
            environment="Sunny poolside",
            language_tag="en",
            claim_snapshots=[],
        )
        self.storyboard = Storyboard(
            id="example-storyboard",
            campaign_id=self.campaign.id,
            shots=[
                StoryboardShot(
                    id="hook",
                    purpose="hook",
                    duration_seconds=2.5,
                    selected_claim_ids=[],
                    visual_description="Reveal the product.",
                ),
                StoryboardShot(
                    id="feature",
                    purpose="feature",
                    duration_seconds=2.5,
                    selected_claim_ids=[],
                    visual_description="Show product details.",
                ),
                StoryboardShot(
                    id="closing",
                    purpose="lifestyle",
                    duration_seconds=3.0,
                    selected_claim_ids=[],
                    visual_description="Close on the product.",
                ),
            ],
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def plan(self, shot_selections=None, **updates) -> StoryboardAssetPlan:
        values = {
            "id": "example-asset-plan",
            "product_id": self.product.id,
            "campaign_id": self.campaign.id,
            "storyboard_id": self.storyboard.id,
            "shot_selections": shot_selections or [],
        }
        values.update(updates)
        return StoryboardAssetPlan.model_validate(values)

    def selection(
        self,
        shot_id="hook",
        primary=None,
        supporting=None,
    ) -> ShotAssetSelection:
        return ShotAssetSelection(
            shot_id=shot_id,
            primary_asset=snapshot_original_asset(primary or self.front),
            supporting_assets=supporting or [],
        )

    def validate(self, plan, derived_manifest="default") -> StoryboardAssetPlan:
        active_derived = (
            self.derived_manifest
            if derived_manifest == "default"
            else derived_manifest
        )
        return validate_storyboard_asset_plan(
            plan,
            self.product,
            self.campaign,
            self.storyboard,
            self.original_manifest,
            active_derived,
        )

    def test_valid_empty_partial_and_multiple_shot_plans(self) -> None:
        plans = [
            self.plan(),
            self.plan([self.selection()]),
            self.plan(
                [
                    self.selection("hook", self.three_quarter),
                    self.selection("feature", self.front),
                    self.selection("closing", self.rear),
                ]
            ),
        ]

        validated = [self.validate(plan) for plan in plans]

        self.assertEqual([len(plan.shot_selections) for plan in validated], [0, 1, 3])
        self.assertEqual(
            [
                selection.primary_asset.asset_id
                for selection in validated[2].shot_selections
            ],
            [
                "original:image-three-quarter",
                "original:image-front",
                "original:image-rear",
            ],
        )

    def test_supporting_original_and_sheet_resolve_in_caller_order(self) -> None:
        supporting = [
            snapshot_reference_sheet(self.sheet),
            snapshot_original_asset(self.rear),
        ]
        plan = self.plan(
            [self.selection(primary=self.front, supporting=supporting)]
        )

        validated = self.validate(plan)

        self.assertEqual(
            [
                asset.asset_id
                for asset in validated.shot_selections[0].supporting_assets
            ],
            [asset.asset_id for asset in supporting],
        )

    def test_product_campaign_storyboard_and_shot_bindings_are_exact(self) -> None:
        valid = self.plan([self.selection()])
        cases = (
            (
                valid.model_copy(update={"product_id": "other-product"}, deep=True),
                self.product,
                self.campaign,
                self.storyboard,
            ),
            (
                valid.model_copy(update={"campaign_id": "other-campaign"}, deep=True),
                self.product,
                self.campaign,
                self.storyboard,
            ),
            (
                valid.model_copy(update={"storyboard_id": "other-board"}, deep=True),
                self.product,
                self.campaign,
                self.storyboard,
            ),
            (
                valid,
                self.product.model_copy(update={"id": "other-product"}, deep=True),
                self.campaign,
                self.storyboard,
            ),
            (
                valid,
                self.product,
                self.campaign,
                self.storyboard.model_copy(
                    update={"campaign_id": "other-campaign"}, deep=True
                ),
            ),
        )
        for plan, product, campaign, storyboard in cases:
            with self.subTest(plan=plan, product=product, storyboard=storyboard):
                with self.assertRaises(AssetPlanContextError):
                    validate_storyboard_asset_plan(
                        plan,
                        product,
                        campaign,
                        storyboard,
                        self.original_manifest,
                        self.derived_manifest,
                    )

        unknown_shot = self.plan([self.selection("unknown-shot")])
        with self.assertRaisesRegex(AssetPlanContextError, "unknown-shot"):
            self.validate(unknown_shot)

    def test_unknown_or_unverified_original_is_rejected(self) -> None:
        unknown_snapshot = snapshot_original_asset(self.front).model_copy(
            update={"asset_id": "original:image-unknown"}, deep=True
        )
        unknown = self.plan(
            [
                ShotAssetSelection(
                    shot_id="hook",
                    primary_asset=unknown_snapshot,
                )
            ]
        )
        with self.assertRaises(UnknownSelectedAssetError):
            self.validate(unknown)

        product_without_front = self.product.model_copy(
            update={
                "images": [
                    image
                    for image in self.product.images
                    if image.id != self.front.source_image_id
                ]
            },
            deep=True,
        )
        with self.assertRaises(SelectedAssetProvenanceError):
            validate_storyboard_asset_plan(
                self.plan([self.selection()]),
                product_without_front,
                self.campaign,
                self.storyboard,
                self.original_manifest,
                self.derived_manifest,
            )

    def test_selected_original_and_sheet_integrity_failures_are_rejected(self) -> None:
        original_plan = self.plan([self.selection()])
        Path(self.front.local_path).write_bytes(b"corrupt original")
        with self.assertRaises(SelectedAssetIntegrityError):
            self.validate(original_plan)

        _, replacement_front = stored_original(
            self.root,
            "image-front",
            color=(200, 20, 20),
        )
        self.front = replacement_front
        self.original_manifest = self.original_manifest.model_copy(
            update={
                "images": [self.front, self.three_quarter, self.rear]
            },
            deep=True,
        )
        sheet_plan = self.plan(
            [
                self.selection(
                    supporting=[snapshot_reference_sheet(self.sheet)]
                )
            ]
        )
        Path(self.sheet.local_path).write_bytes(b"corrupt sheet")
        with self.assertRaises(SelectedAssetIntegrityError):
            self.validate(sheet_plan)

    def test_snapshot_sha_dimensions_and_format_must_match_current_asset(self) -> None:
        original = snapshot_original_asset(self.front)
        sheet = snapshot_reference_sheet(self.sheet)
        changes = (
            (original, {"sha256_at_selection": "d" * 64}),
            (original, {"width_at_selection": original.width_at_selection + 1}),
            (original, {"format_at_selection": "JPEG"}),
            (sheet, {"sha256_at_selection": "d" * 64}),
            (sheet, {"height_at_selection": sheet.height_at_selection + 1}),
            (sheet, {"format_at_selection": "WEBP"}),
        )
        for snapshot, update in changes:
            changed = snapshot.model_copy(update=update, deep=True)
            if changed.asset_kind == "original_image":
                selection = ShotAssetSelection(
                    shot_id="hook",
                    primary_asset=changed,
                )
            else:
                selection = self.selection(supporting=[changed])
            with self.subTest(snapshot=snapshot, update=update):
                with self.assertRaises(SelectedAssetMismatchError):
                    self.validate(self.plan([selection]))

    def test_same_logical_original_with_new_valid_bytes_fails_snapshot_sha(self) -> None:
        plan = self.plan([self.selection()])
        _, changed_front = stored_original(
            self.root,
            "image-front",
            color=(1, 2, 3),
        )
        changed_manifest = ProductAssetManifest(
            product_id=self.product.id,
            images=[changed_front, self.three_quarter, self.rear],
        )

        with self.assertRaisesRegex(SelectedAssetMismatchError, "SHA-256"):
            validate_storyboard_asset_plan(
                plan,
                self.product,
                self.campaign,
                self.storyboard,
                changed_manifest,
                self.derived_manifest,
            )

    def test_same_logical_sheet_with_new_valid_bytes_fails_snapshot_sha(self) -> None:
        plan = self.plan(
            [self.selection(supporting=[snapshot_reference_sheet(self.sheet)])]
        )
        new_data = encoded_image_bytes(
            "PNG",
            size=(200, 100),
            color=(10, 11, 12),
        )
        Path(self.sheet.local_path).write_bytes(new_data)
        changed_sheet = self.sheet.model_copy(
            update={
                "sha256": hashlib.sha256(new_data).hexdigest(),
                "byte_size": len(new_data),
            },
            deep=True,
        )
        self.assertEqual(
            reference_sheet_asset_id(changed_sheet),
            reference_sheet_asset_id(self.sheet),
        )
        changed_derived = ProductDerivedAssetManifest(
            product_id=self.product.id,
            reference_sheets=[changed_sheet],
        )

        with self.assertRaisesRegex(SelectedAssetMismatchError, "SHA-256"):
            self.validate(plan, derived_manifest=changed_derived)

    def test_missing_derived_manifest_fails_only_when_sheet_is_selected(self) -> None:
        originals_only = self.plan([self.selection()])
        with_sheet = self.plan(
            [
                self.selection(
                    supporting=[snapshot_reference_sheet(self.sheet)]
                )
            ]
        )

        self.assertEqual(
            self.validate(originals_only, derived_manifest=None),
            originals_only,
        )
        with self.assertRaisesRegex(AssetPlanContextError, "derived manifest"):
            self.validate(with_sheet, derived_manifest=None)

    def test_selected_sheet_requires_current_tile_provenance(self) -> None:
        changed_tile = self.sheet.tiles[0].model_copy(
            update={"source_sha256": "d" * 64}, deep=True
        )
        changed_sheet = self.sheet.model_copy(
            update={"tiles": [changed_tile, self.sheet.tiles[1]]}, deep=True
        )
        changed_derived = self.derived_manifest.model_copy(
            update={"reference_sheets": [changed_sheet]}, deep=True
        )
        changed_snapshot = snapshot_reference_sheet(changed_sheet)
        plan = self.plan([self.selection(supporting=[changed_snapshot])])

        with self.assertRaises(SelectedAssetProvenanceError):
            self.validate(plan, derived_manifest=changed_derived)

    def test_unknown_sheet_and_cross_product_manifests_are_rejected(self) -> None:
        unknown_sheet = snapshot_reference_sheet(self.sheet).model_copy(
            update={"asset_id": f"reference-sheet:{'0' * 64}"},
            deep=True,
        )
        with self.assertRaises(UnknownSelectedAssetError):
            self.validate(
                self.plan([self.selection(supporting=[unknown_sheet])])
            )

        ambiguous_sheet = self.sheet.model_copy(
            update={
                "local_path": str(self.root / "ambiguous-sheet.png"),
                "sha256": "e" * 64,
            },
            deep=True,
        )
        ambiguous_manifest = ProductDerivedAssetManifest(
            product_id=self.product.id,
            reference_sheets=[self.sheet, ambiguous_sheet],
        )
        with self.assertRaisesRegex(AssetPlanContextError, "ambiguous"):
            self.validate(
                self.plan(
                    [
                        self.selection(
                            supporting=[snapshot_reference_sheet(self.sheet)]
                        )
                    ]
                ),
                derived_manifest=ambiguous_manifest,
            )

        other_original = self.front.model_copy(
            update={"product_id": "other-product"}, deep=True
        )
        other_original_manifest = ProductAssetManifest(
            product_id="other-product",
            images=[other_original],
        )
        with self.assertRaises(AssetPlanContextError):
            validate_storyboard_asset_plan(
                self.plan([self.selection()]),
                self.product,
                self.campaign,
                self.storyboard,
                other_original_manifest,
            )

        other_sheet = self.sheet.model_copy(
            update={"product_id": "other-product"}, deep=True
        )
        other_derived_manifest = ProductDerivedAssetManifest(
            product_id="other-product",
            reference_sheets=[other_sheet],
        )
        with self.assertRaises(AssetPlanContextError):
            self.validate(
                self.plan(
                    [
                        self.selection(
                            supporting=[snapshot_reference_sheet(other_sheet)]
                        )
                    ]
                ),
                derived_manifest=other_derived_manifest,
            )

    def test_unselected_stale_assets_do_not_block_a_valid_selection(self) -> None:
        stale_image, stale_asset = stored_original(
            self.root,
            "image-stale",
            color=(100, 100, 100),
        )
        Path(stale_asset.local_path).write_bytes(b"stale")
        product = self.product.model_copy(
            update={"images": [*self.product.images, stale_image]},
            deep=True,
        )
        manifest = self.original_manifest.model_copy(
            update={"images": [*self.original_manifest.images, stale_asset]},
            deep=True,
        )

        validated = validate_storyboard_asset_plan(
            self.plan([self.selection()]),
            product,
            self.campaign,
            self.storyboard,
            manifest,
            self.derived_manifest,
        )

        self.assertEqual(validated, self.plan([self.selection()]))

    def test_unselected_stale_reference_sheet_does_not_block_valid_selection(self) -> None:
        _, extra_source = stored_original(
            self.root,
            "image-extra",
            color=(80, 90, 100),
        )
        extra_product_image = ProductImage(
            id=extra_source.source_image_id,
            url=extra_source.source_url,
            source_url="https://example.com/product",
            source_type="json_ld_product_image",
        )
        product = self.product.model_copy(
            update={"images": [*self.product.images, extra_product_image]},
            deep=True,
        )
        original_manifest = ProductAssetManifest(
            product_id=self.product.id,
            images=[*self.original_manifest.images, extra_source],
        )
        stale_sheet = stored_reference_sheet(
            self.root,
            [extra_source],
        ).model_copy(
            update={
                "local_path": str(self.root / "stale-sheet.png"),
                "sha256": "e" * 64,
            },
            deep=True,
        )
        Path(stale_sheet.local_path).write_bytes(b"stale")
        ambiguous_unselected_sheet = stale_sheet.model_copy(
            update={
                "local_path": str(self.root / "ambiguous-unselected-sheet.png"),
                "sha256": "d" * 64,
            },
            deep=True,
        )
        Path(ambiguous_unselected_sheet.local_path).write_bytes(b"also stale")
        self.assertEqual(
            reference_sheet_asset_id(stale_sheet),
            reference_sheet_asset_id(ambiguous_unselected_sheet),
        )
        derived_manifest = ProductDerivedAssetManifest(
            product_id=self.product.id,
            reference_sheets=[
                self.sheet,
                stale_sheet,
                ambiguous_unselected_sheet,
            ],
        )
        plan = self.plan(
            [self.selection(supporting=[snapshot_reference_sheet(self.sheet)])]
        )

        validated = validate_storyboard_asset_plan(
            plan,
            product,
            self.campaign,
            self.storyboard,
            original_manifest,
            derived_manifest,
        )

        self.assertEqual(validated, plan)

    def test_same_assets_may_be_reused_across_shots(self) -> None:
        supporting = [snapshot_reference_sheet(self.sheet)]
        plan = self.plan(
            [
                self.selection("hook", self.front, supporting),
                self.selection("feature", self.front, supporting),
                self.selection("closing", self.front, supporting),
            ]
        )

        validated = self.validate(plan)

        self.assertEqual(validated, plan)

    def test_product_ranking_changes_do_not_reorder_supporting_assets(self) -> None:
        supporting = [
            snapshot_original_asset(self.rear),
            snapshot_reference_sheet(self.sheet),
            snapshot_original_asset(self.three_quarter),
        ]
        plan = self.plan([self.selection(supporting=supporting)])
        reranked_product = self.product.model_copy(deep=True)
        for index, image in enumerate(reranked_product.images):
            image.confidence = index / 10

        validated = validate_storyboard_asset_plan(
            plan,
            reranked_product,
            self.campaign,
            self.storyboard,
            self.original_manifest,
            self.derived_manifest,
        )

        self.assertEqual(
            validated.shot_selections[0].supporting_assets,
            supporting,
        )

    def test_validation_revalidates_bypass_and_does_not_mutate_inputs(self) -> None:
        plan = self.plan([self.selection()])
        bypassed_selection = plan.shot_selections[0].model_copy(
            update={"primary_asset": snapshot_reference_sheet(self.sheet)},
            deep=True,
        )
        bypassed = plan.model_copy(
            update={"shot_selections": [bypassed_selection]}, deep=True
        )
        with self.assertRaisesRegex(AssetPlanContextError, "structurally valid"):
            self.validate(bypassed)

        before = deepcopy(
            (
                plan,
                self.product,
                self.campaign,
                self.storyboard,
                self.original_manifest,
                self.derived_manifest,
            )
        )
        validated = self.validate(plan)
        self.assertEqual(
            (
                plan,
                self.product,
                self.campaign,
                self.storyboard,
                self.original_manifest,
                self.derived_manifest,
            ),
            before,
        )
        validated.shot_selections[0].primary_asset.asset_id = "original:changed"
        self.assertNotEqual(validated, plan)

    def test_structural_json_load_does_not_check_current_filesystem(self) -> None:
        plan = self.plan([self.selection()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asset-plan.json"
            save_storyboard_asset_plan(plan, path)
            Path(self.front.local_path).unlink()

            restored = load_storyboard_asset_plan(path)

            self.assertEqual(restored, plan)
            with self.assertRaises(SelectedAssetIntegrityError):
                self.validate(restored)


class StoryboardAssetPlanSoundcoreRegressionTests(unittest.TestCase):
    def test_documented_soundcore_assets_validate_for_all_storyboard_shots(
        self,
    ) -> None:
        product_id = "soundcore-com-group-products-a31x1021"
        source_image_id = "image-a13f7a58e3b50532"
        original_sha256 = (
            "27f46a38d92121db356cf431d4e6a5f5e"
            "edb3e891ba76e81ba675b48f9b67820"
        )
        sheet_sha256 = (
            "7feb5befb9604920f0073181826ebc55"
            "d2741bfa6b05c3dca43518018b15450e"
        )
        original = DownloadedProductImage(
            product_id=product_id,
            source_image_id=source_image_id,
            source_url="https://cdn.example/soundcore-select-4-go.webp",
            final_url="https://cdn.example/soundcore-select-4-go.webp",
            local_path="/documented/soundcore-select-4-go.webp",
            sha256=original_sha256,
            byte_size=194112,
            width=2000,
            height=2000,
            format="WEBP",
            mime_type="image/webp",
            downloaded_at=FIXED_TIME,
        )
        sheet = ProductReferenceSheet(
            product_id=product_id,
            local_path="/documented/soundcore-reference-sheet.png",
            width=2048,
            height=2048,
            format="PNG",
            sha256=sheet_sha256,
            byte_size=906405,
            created_at=FIXED_TIME,
            tiles=[
                ProductReferenceSheetTile(
                    index=0,
                    source_image_id=source_image_id,
                    source_sha256=original_sha256,
                    source_local_path=original.local_path,
                    x=64,
                    y=64,
                    width=1920,
                    height=1920,
                    rendered_x=64,
                    rendered_y=64,
                    rendered_width=1920,
                    rendered_height=1920,
                )
            ],
        )
        product = Product(
            id=product_id,
            name="Select 4 Go",
            brand="Soundcore",
            category="Portable Bluetooth Speaker",
            images=[
                ProductImage(
                    id=source_image_id,
                    url=original.source_url,
                    source_url="https://www.soundcore.com/products/a31x1021",
                    source_type="json_ld_product_image",
                    width=2000,
                    height=2000,
                    confidence=0.9,
                )
            ],
        )
        campaign, storyboard = build_soundcore_campaign_storyboard()
        original_manifest = ProductAssetManifest(
            product_id=product_id,
            images=[original],
        )
        derived_manifest = ProductDerivedAssetManifest(
            product_id=product_id,
            reference_sheets=[sheet],
        )
        primary = snapshot_original_asset(original)
        supporting = snapshot_reference_sheet(sheet)
        plan = StoryboardAssetPlan(
            id="soundcore-select-4-go-summer-assets",
            product_id=product_id,
            campaign_id=campaign.id,
            storyboard_id=storyboard.id,
            shot_selections=[
                ShotAssetSelection(
                    shot_id=shot.id,
                    primary_asset=primary,
                    supporting_assets=[supporting],
                )
                for shot in storyboard.shots
            ],
        )

        with patch(
            "storyboard_asset_plan.build_verified_product_image_set",
            return_value=[original.model_copy(deep=True)],
        ), patch(
            "storyboard_asset_plan.verify_product_reference_sheet",
            return_value=sheet.model_copy(deep=True),
        ):
            validated = validate_storyboard_asset_plan(
                plan,
                product,
                campaign,
                storyboard,
                original_manifest,
                derived_manifest,
            )

        self.assertEqual(validated, plan)
        self.assertEqual(
            [selection.shot_id for selection in validated.shot_selections],
            ["hook", "water-feature", "poolside-lifestyle"],
        )
        self.assertEqual(primary.sha256_at_selection, original_sha256)
        self.assertEqual(supporting.sha256_at_selection, sheet_sha256)
        self.assertEqual(
            supporting.asset_id,
            reference_sheet_asset_id(sheet),
        )


if __name__ == "__main__":
    unittest.main()
