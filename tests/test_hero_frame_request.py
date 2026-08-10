import hashlib
import io
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

import hero_frame_request
from ad_claims import ProductAdClaimSelection
from campaign import AdCampaign, CampaignClaimSnapshot
from demo_campaign_storyboard import build_soundcore_campaign_storyboard
from hero_frame_request import (
    ConsumerAssetIntegrityError,
    HeroFrameRequest,
    HeroFrameRequestContextError,
    ShotNotPreparedError,
    UnknownHeroFrameShotError,
    build_hero_frame_request,
    read_verified_runtime_asset_bytes,
)
from product import (
    ClaimEvidence,
    Product,
    ProductFeature,
    ProductImage,
    ProductSpec,
    ProductVariant,
)
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from product_image_downloads import DownloadedProductImage
from product_reference_sheets import (
    ProductReferenceSheet,
    ProductReferenceSheetTile,
)
from storyboard import Storyboard, StoryboardShot, StoryboardTextOverlay
from storyboard_asset_plan import (
    SelectedAssetIntegrityError,
    SelectedAssetMismatchError,
    SelectedAssetProvenanceError,
    ShotAssetSelection,
    StoryboardAssetPlan,
    reference_sheet_asset_id,
    snapshot_original_asset,
    snapshot_reference_sheet,
)


FIXED_TIME = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def image_bytes(
    image_format: str = "PNG",
    *,
    size=(32, 24),
    color=(20, 100, 180),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def store_original(
    root: Path,
    source_image_id: str,
    *,
    product_id: str = "example-product",
    image_format: str = "PNG",
    size=(32, 24),
    color=(20, 100, 180),
):
    data = image_bytes(image_format, size=size, color=color)
    extension = image_format.casefold()
    path = root / f"{source_image_id}.{extension}"
    path.write_bytes(data)
    asset = DownloadedProductImage(
        product_id=product_id,
        source_image_id=source_image_id,
        source_url=f"https://cdn.example/{source_image_id}.{extension}",
        final_url=f"https://media.example/{source_image_id}.{extension}",
        local_path=str(path),
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


def store_reference_sheet(
    root: Path,
    sources,
    *,
    product_id: str = "example-product",
) -> ProductReferenceSheet:
    data = image_bytes("PNG", size=(240, 120), color=(240, 240, 240))
    path = root / "reference-sheet.png"
    path.write_bytes(data)
    tile_width = 240 // len(sources)
    return ProductReferenceSheet(
        product_id=product_id,
        local_path=str(path),
        width=240,
        height=120,
        format="PNG",
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        created_at=FIXED_TIME,
        tiles=[
            ProductReferenceSheetTile(
                index=index,
                source_image_id=source.source_image_id,
                source_sha256=source.sha256,
                source_local_path=source.local_path,
                x=index * tile_width,
                y=0,
                width=tile_width,
                height=120,
                rendered_x=index * tile_width,
                rendered_y=0,
                rendered_width=tile_width,
                rendered_height=120,
            )
            for index, source in enumerate(sources)
        ],
    )


class HeroFrameRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        front_image, self.front = store_original(
            self.root,
            "image-front",
            color=(200, 20, 20),
        )
        three_quarter_image, self.three_quarter = store_original(
            self.root,
            "image-three-quarter",
            color=(20, 200, 20),
        )
        rear_image, self.rear = store_original(
            self.root,
            "image-rear",
            color=(20, 20, 200),
        )
        evidence = ClaimEvidence(
            raw_text="Up to 20 hours",
            source_url="https://example.com/product",
            extraction_method="json_ld",
            extracted_at=FIXED_TIME,
        )
        self.product = Product(
            id="example-product",
            source_name="SEO product title",
            name="Example Speaker",
            brand="Example",
            category="Portable Speaker",
            description="A description that must not enter the request.",
            variant=ProductVariant(color="Green", model_number="EX-1"),
            images=[rear_image, front_image, three_quarter_image],
            specs=[
                ProductSpec(
                    key="playback_time",
                    label="Playback time",
                    value="Up to 20 hours",
                    evidence=evidence,
                )
            ],
            features=[
                ProductFeature(
                    id="floating-design",
                    name="Floating design",
                    evidence=evidence,
                )
            ],
        )
        self.original_manifest = ProductAssetManifest(
            product_id=self.product.id,
            images=[self.front, self.three_quarter, self.rear],
        )
        self.sheet = store_reference_sheet(
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
                selected_claim_ids=[
                    "spec:playback_time",
                    "feature:floating-design",
                ],
            ),
            ad_format="vertical_social",
            aspect_ratio="9:16",
            target_duration_seconds=8,
            creative_style="ugc",
            environment="Sunny poolside",
            language_tag="en-US",
            creative_brief="Keep the scene energetic and natural.",
            claim_snapshots=[
                CampaignClaimSnapshot(
                    claim_id="spec:playback_time",
                    display_text="Up to 20 hours of playback",
                    context_text="At moderate volume.",
                    verification_status_at_selection="corroborated",
                ),
                CampaignClaimSnapshot(
                    claim_id="feature:floating-design",
                    display_text="Floating design",
                    verification_status_at_selection="supported",
                ),
            ],
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
                    visual_description="Reveal the speaker floating in the pool.",
                    camera_direction="Low close-up from water level.",
                    text_overlays=[
                        StoryboardTextOverlay(
                            kind="creative",
                            text="BIG SUMMER ENERGY",
                        )
                    ],
                ),
                StoryboardShot(
                    id="feature",
                    purpose="feature",
                    duration_seconds=2.5,
                    selected_claim_ids=["feature:floating-design"],
                    visual_description="Show the speaker floating at the surface.",
                    text_overlays=[
                        StoryboardTextOverlay(
                            kind="claim",
                            claim_id="feature:floating-design",
                        )
                    ],
                ),
                StoryboardShot(
                    id="closing",
                    purpose="lifestyle",
                    duration_seconds=3.0,
                    selected_claim_ids=[
                        "feature:floating-design",
                        "spec:playback_time",
                    ],
                    visual_description="Friends listen beside the pool.",
                    camera_direction="Wide lifestyle composition.",
                    text_overlays=[
                        StoryboardTextOverlay(kind="product_name"),
                        StoryboardTextOverlay(kind="cta", text="SHOP NOW"),
                    ],
                ),
            ],
        )
        self.asset_plan = StoryboardAssetPlan(
            id="example-asset-plan",
            product_id=self.product.id,
            campaign_id=self.campaign.id,
            storyboard_id=self.storyboard.id,
            shot_selections=[
                ShotAssetSelection(
                    shot_id="hook",
                    primary_asset=snapshot_original_asset(self.front),
                    supporting_assets=[snapshot_reference_sheet(self.sheet)],
                ),
                ShotAssetSelection(
                    shot_id="feature",
                    primary_asset=snapshot_original_asset(self.three_quarter),
                    supporting_assets=[
                        snapshot_reference_sheet(self.sheet),
                        snapshot_original_asset(self.rear),
                    ],
                ),
                ShotAssetSelection(
                    shot_id="closing",
                    primary_asset=snapshot_original_asset(self.rear),
                    supporting_assets=[snapshot_original_asset(self.front)],
                ),
            ],
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def build(self, shot_id="feature", **updates) -> HeroFrameRequest:
        values = {
            "product": self.product,
            "campaign": self.campaign,
            "storyboard": self.storyboard,
            "asset_plan": self.asset_plan,
            "shot_id": shot_id,
            "original_manifest": self.original_manifest,
            "derived_manifest": self.derived_manifest,
        }
        values.update(updates)
        return build_hero_frame_request(**values)

    def test_valid_request_has_only_approved_identity_and_policy_fields(self) -> None:
        request = self.build()

        self.assertEqual(
            request.product.model_dump(),
            {
                "product_id": "example-product",
                "name": "Example Speaker",
                "brand": "Example",
                "category": "Portable Speaker",
                "variant": {
                    "color": "Green",
                    "size": None,
                    "model_number": "EX-1",
                    "sku": None,
                },
            },
        )
        self.assertEqual(request.product_fidelity, "preserve_exact_product")
        self.assertEqual(request.added_text_policy, "no_added_text")
        self.assertEqual(request.factual_grounding, "selected_claims_only")
        self.assertEqual(request.aspect_ratio, "9:16")
        self.assertEqual(request.asset_plan_id, self.asset_plan.id)

        serialized = request.model_dump(mode="json")
        self.assertNotIn("language_tag", serialized)
        self.assertNotIn("text_overlays", serialized)
        self.assertNotIn("specs", serialized["product"])
        self.assertNotIn("features", serialized["product"])
        self.assertNotIn("description", serialized["product"])
        self.assertNotIn("evidence", str(serialized).casefold())
        self.assertNotIn("observations", str(serialized).casefold())

    def test_request_model_rejects_provider_generation_and_job_state(self) -> None:
        request_data = self.build().model_dump()
        forbidden = {
            "provider": "example",
            "model": "image-model",
            "seed": 42,
            "prompt": "provider prompt",
            "negative_prompt": "bad",
            "output_path": "/tmp/output.png",
            "billing": {"cost": 1},
            "retry_count": 1,
            "operation_id": "job-1",
            "response": {},
            "variants": [],
        }
        for field, value in forbidden.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    HeroFrameRequest.model_validate(
                        {**request_data, field: value}
                    )

    def test_shot_claim_snapshots_are_deep_copied_in_shot_order(self) -> None:
        request = self.build("closing")

        self.assertEqual(
            [claim.claim_id for claim in request.factual_claims],
            ["feature:floating-design", "spec:playback_time"],
        )
        self.assertEqual(request.factual_claims[0].display_text, "Floating design")
        self.assertIsNone(request.factual_claims[0].context_text)
        self.assertEqual(
            request.factual_claims[0].verification_status_at_selection,
            "supported",
        )
        self.assertEqual(
            request.factual_claims[1].context_text,
            "At moderate volume.",
        )
        request.factual_claims[0].display_text = "changed"
        self.assertEqual(
            self.campaign.claim_snapshots[1].display_text,
            "Floating design",
        )

    def test_zero_claim_creative_prose_does_not_become_factual_authority(self) -> None:
        request = self.build("hook")

        self.assertEqual(request.factual_claims, [])
        self.assertIn("floating", request.creative_direction.scene_direction)
        self.assertEqual(request.factual_grounding, "selected_claims_only")

    def test_creative_direction_preserves_authored_campaign_and_shot_values(self) -> None:
        request = self.build("hook")

        self.assertEqual(request.creative_direction.shot_purpose, "hook")
        self.assertEqual(request.creative_direction.environment, "Sunny poolside")
        self.assertEqual(
            request.creative_direction.scene_direction,
            "Reveal the speaker floating in the pool.",
        )
        self.assertEqual(
            request.creative_direction.camera_direction,
            "Low close-up from water level.",
        )
        self.assertEqual(request.creative_direction.creative_style, "ugc")
        self.assertEqual(
            request.creative_direction.creative_brief,
            "Keep the scene energetic and natural.",
        )

    def test_runtime_assets_resolve_paths_and_preserve_reference_roles_and_order(
        self,
    ) -> None:
        request = self.build("feature")

        self.assertEqual(request.primary_reference.asset_kind, "original_image")
        self.assertEqual(
            request.primary_reference.asset_id,
            "original:image-three-quarter",
        )
        self.assertEqual(
            request.primary_reference.local_path,
            Path(self.three_quarter.local_path),
        )
        self.assertEqual(
            [asset.asset_id for asset in request.supporting_references],
            [
                reference_sheet_asset_id(self.sheet),
                "original:image-rear",
            ],
        )
        self.assertEqual(
            [asset.asset_kind for asset in request.supporting_references],
            ["reference_sheet", "original_image"],
        )
        for asset in [request.primary_reference, *request.supporting_references]:
            self.assertIsInstance(asset.local_path, Path)
            self.assertNotIn("bytes", type(asset).model_fields)

    def test_unknown_and_unprepared_shots_fail_without_asset_fallback(self) -> None:
        with self.assertRaises(UnknownHeroFrameShotError):
            self.build("missing")

        partial_plan = self.asset_plan.model_copy(
            update={
                "shot_selections": [self.asset_plan.shot_selections[0]]
            },
            deep=True,
        )
        with self.assertRaises(ShotNotPreparedError):
            self.build("feature", asset_plan=partial_plan)

    def test_unknown_shot_is_reported_before_unrelated_asset_io(self) -> None:
        Path(self.front.local_path).write_bytes(b"stale")

        with self.assertRaises(UnknownHeroFrameShotError):
            self.build("missing")

    def test_context_bindings_are_checked_before_shot_prepared_state(self) -> None:
        mismatched_partial_plan = self.asset_plan.model_copy(
            update={
                "storyboard_id": "other-storyboard",
                "shot_selections": [self.asset_plan.shot_selections[0]],
            },
            deep=True,
        )

        with self.assertRaises(HeroFrameRequestContextError):
            self.build("feature", asset_plan=mismatched_partial_plan)

    def test_supplied_derived_manifest_must_bind_even_for_original_only_shot(
        self,
    ) -> None:
        original_only_plan = self.asset_plan.model_copy(
            update={
                "shot_selections": [self.asset_plan.shot_selections[2]]
            },
            deep=True,
        )
        other_sheet = self.sheet.model_copy(
            update={"product_id": "other-product"},
            deep=True,
        )
        other_derived_manifest = ProductDerivedAssetManifest(
            product_id="other-product",
            reference_sheets=[other_sheet],
        )

        with self.assertRaises(HeroFrameRequestContextError):
            self.build(
                "closing",
                asset_plan=original_only_plan,
                derived_manifest=other_derived_manifest,
            )

    def test_exact_product_campaign_storyboard_plan_and_manifest_bindings(self) -> None:
        cases = (
            {"product": self.product.model_copy(update={"id": "other"}, deep=True)},
            {
                "campaign": self.campaign.model_copy(
                    update={"id": "other"}, deep=True
                )
            },
            {
                "storyboard": self.storyboard.model_copy(
                    update={"id": "other"}, deep=True
                )
            },
            {
                "asset_plan": self.asset_plan.model_copy(
                    update={"storyboard_id": "other"}, deep=True
                )
            },
            {
                "original_manifest": self.original_manifest.model_copy(
                    update={"product_id": "other"}, deep=True
                )
            },
            {
                "derived_manifest": self.derived_manifest.model_copy(
                    update={"product_id": "other"}, deep=True
                )
            },
        )
        for update in cases:
            with self.subTest(update=update):
                with self.assertRaises(HeroFrameRequestContextError):
                    self.build(**update)

    def test_structural_bypass_for_shots_and_campaign_snapshots_is_rejected(self) -> None:
        duplicate_storyboard = self.storyboard.model_copy(
            update={"shots": [self.storyboard.shots[0], self.storyboard.shots[0]]},
            deep=True,
        )
        with self.assertRaises(HeroFrameRequestContextError):
            self.build(storyboard=duplicate_storyboard)

        corrupt_campaign = self.campaign.model_copy(
            update={"claim_snapshots": [self.campaign.claim_snapshots[0]]},
            deep=True,
        )
        with self.assertRaises(HeroFrameRequestContextError):
            self.build(campaign=corrupt_campaign)

    def test_selection_snapshot_metadata_is_rechecked(self) -> None:
        original_selection = self.asset_plan.shot_selections[1]
        changes = (
            {"sha256_at_selection": "d" * 64},
            {"width_at_selection": self.three_quarter.width + 1},
            {"height_at_selection": self.three_quarter.height + 1},
            {"format_at_selection": "JPEG"},
        )
        for update in changes:
            changed_primary = original_selection.primary_asset.model_copy(
                update=update,
                deep=True,
            )
            changed_selection = original_selection.model_copy(
                update={"primary_asset": changed_primary},
                deep=True,
            )
            changed_plan = self.asset_plan.model_copy(
                update={
                    "shot_selections": [
                        self.asset_plan.shot_selections[0],
                        changed_selection,
                        self.asset_plan.shot_selections[2],
                    ]
                },
                deep=True,
            )
            with self.subTest(update=update):
                with self.assertRaises(SelectedAssetMismatchError):
                    self.build(asset_plan=changed_plan)

    def test_stale_original_and_reference_sheet_are_rejected(self) -> None:
        Path(self.three_quarter.local_path).write_bytes(b"stale original")
        with self.assertRaises(SelectedAssetIntegrityError):
            self.build("feature")

        _, replacement = store_original(
            self.root,
            "image-three-quarter",
            color=(20, 200, 20),
        )
        self.three_quarter = replacement
        Path(self.sheet.local_path).write_bytes(b"stale sheet")
        with self.assertRaises(SelectedAssetIntegrityError):
            self.build("feature")

    def test_reference_sheet_provenance_is_revalidated(self) -> None:
        changed_tile = self.sheet.tiles[0].model_copy(
            update={"source_sha256": "d" * 64},
            deep=True,
        )
        changed_sheet = self.sheet.model_copy(
            update={"tiles": [changed_tile, self.sheet.tiles[1]]},
            deep=True,
        )
        changed_derived = ProductDerivedAssetManifest(
            product_id=self.product.id,
            reference_sheets=[changed_sheet],
        )
        feature_selection = self.asset_plan.shot_selections[1]
        changed_selection = feature_selection.model_copy(
            update={
                "supporting_assets": [
                    snapshot_reference_sheet(changed_sheet),
                    feature_selection.supporting_assets[1],
                ]
            },
            deep=True,
        )
        changed_plan = self.asset_plan.model_copy(
            update={
                "shot_selections": [changed_selection]
            },
            deep=True,
        )

        with self.assertRaises(SelectedAssetProvenanceError):
            self.build(
                asset_plan=changed_plan,
                derived_manifest=changed_derived,
            )

    def test_consumer_reads_verified_bytes_and_detects_post_build_change(self) -> None:
        request = self.build("feature")
        asset = request.primary_reference
        original_bytes = Path(asset.local_path).read_bytes()

        self.assertEqual(
            read_verified_runtime_asset_bytes(asset),
            original_bytes,
        )

        Path(asset.local_path).write_bytes(
            image_bytes("PNG", size=(32, 24), color=(1, 2, 3))
        )
        with self.assertRaises(ConsumerAssetIntegrityError):
            read_verified_runtime_asset_bytes(asset)

    def test_consumer_revalidates_bypassed_runtime_metadata(self) -> None:
        asset = self.build("feature").primary_reference
        malformed = asset.model_copy(update={"width": 0}, deep=True)

        with self.assertRaises(ConsumerAssetIntegrityError):
            read_verified_runtime_asset_bytes(malformed)

    def test_request_and_inputs_are_isolated_without_persistence_helpers(self) -> None:
        inputs_before = deepcopy(
            (
                self.product,
                self.campaign,
                self.storyboard,
                self.asset_plan,
                self.original_manifest,
                self.derived_manifest,
            )
        )

        request = self.build("feature")

        self.assertEqual(
            (
                self.product,
                self.campaign,
                self.storyboard,
                self.asset_plan,
                self.original_manifest,
                self.derived_manifest,
            ),
            inputs_before,
        )
        request.product.name = "Changed"
        request.supporting_references.reverse()
        self.assertEqual(self.product.name, "Example Speaker")
        self.assertEqual(
            [asset.asset_id for asset in self.asset_plan.shot_selections[1].supporting_assets],
            [
                reference_sheet_asset_id(self.sheet),
                "original:image-rear",
            ],
        )
        self.assertFalse(hasattr(hero_frame_request, "save_hero_frame_request"))
        self.assertFalse(hasattr(hero_frame_request, "load_hero_frame_request"))
        self.assertIsInstance(request.model_dump_json(), str)

    def test_generic_multi_asset_requests_use_explicit_different_primaries(self) -> None:
        requests = [self.build(shot_id) for shot_id in ("hook", "feature", "closing")]

        self.assertEqual(
            [request.primary_reference.asset_id for request in requests],
            [
                "original:image-front",
                "original:image-three-quarter",
                "original:image-rear",
            ],
        )
        self.assertEqual([request.shot_id for request in requests], ["hook", "feature", "closing"])


class HeroFrameRequestSoundcoreTests(unittest.TestCase):
    def test_three_soundcore_shots_reuse_documented_assets_without_prompts(self) -> None:
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
        plan = StoryboardAssetPlan(
            id="soundcore-select-4-go-summer-assets",
            product_id=product_id,
            campaign_id=campaign.id,
            storyboard_id=storyboard.id,
            shot_selections=[
                ShotAssetSelection(
                    shot_id=shot.id,
                    primary_asset=snapshot_original_asset(original),
                    supporting_assets=[snapshot_reference_sheet(sheet)],
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
            requests = [
                build_hero_frame_request(
                    product=product,
                    campaign=campaign,
                    storyboard=storyboard,
                    asset_plan=plan,
                    shot_id=shot.id,
                    original_manifest=original_manifest,
                    derived_manifest=derived_manifest,
                )
                for shot in storyboard.shots
            ]

        self.assertEqual(
            [request.shot_id for request in requests],
            ["hook", "water-feature", "poolside-lifestyle"],
        )
        self.assertEqual(
            [[claim.claim_id for claim in request.factual_claims] for request in requests],
            [
                [],
                ["spec:water_resistance", "feature:floating-design"],
                ["spec:playback_time"],
            ],
        )
        self.assertEqual(
            {request.primary_reference.asset_id for request in requests},
            {f"original:{source_image_id}"},
        )
        self.assertEqual(
            {request.supporting_references[0].sha256 for request in requests},
            {sheet_sha256},
        )
        for request in requests:
            dumped = request.model_dump(mode="json")
            self.assertNotIn("prompt", dumped)
            self.assertEqual(request.aspect_ratio, "9:16")
            self.assertEqual(request.product_fidelity, "preserve_exact_product")


if __name__ == "__main__":
    unittest.main()
