"""Durable, provider-neutral authored motion intent for one Storyboard shot."""

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Literal, Sequence, Tuple, Union

from pydantic import ConfigDict, Field, ValidationError, model_validator

from campaign import AdCampaign
from product import NonEmptyStr, Product, ProductModel
from storyboard import Storyboard, StoryboardContextError, validate_storyboard
from storyboard_asset_plan import StoryboardAssetPlan
from video_shot_request import VideoMotionIntent


SHOT_VIDEO_INTENT_PREFIX = "shot-video-intent:"


class FrozenShotVideoIntentModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


def _intent_payload(values: dict) -> dict:
    return {
        "schema_version": values["schema_version"],
        "product_id": values["product_id"],
        "campaign_id": values["campaign_id"],
        "storyboard_id": values["storyboard_id"],
        "asset_plan_id": values["asset_plan_id"],
        "shot_id": values["shot_id"],
        "requested_generation_duration_seconds": values[
            "requested_generation_duration_seconds"
        ],
        "motion": values["motion"],
        "motion_relevant_claim_ids": values["motion_relevant_claim_ids"],
    }


def _intent_id(values: dict) -> str:
    canonical = json.dumps(
        _intent_payload(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SHOT_VIDEO_INTENT_PREFIX + hashlib.sha256(canonical).hexdigest()


class ShotVideoIntent(FrozenShotVideoIntentModel):
    """Path-free durable intent later consumed by VideoShotRequest creation."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    product_id: NonEmptyStr
    campaign_id: NonEmptyStr
    storyboard_id: NonEmptyStr
    asset_plan_id: NonEmptyStr
    shot_id: NonEmptyStr
    requested_generation_duration_seconds: float = Field(gt=0, strict=True)
    motion: VideoMotionIntent
    motion_relevant_claim_ids: Tuple[NonEmptyStr, ...] = Field(
        default_factory=tuple
    )

    @model_validator(mode="after")
    def require_canonical_identity_and_claims(self) -> "ShotVideoIntent":
        if not math.isfinite(self.requested_generation_duration_seconds):
            raise ValueError("requested generation duration must be finite")
        if len(self.motion_relevant_claim_ids) != len(
            set(self.motion_relevant_claim_ids)
        ):
            raise ValueError("motion-relevant claim IDs must be unique")
        values = self.model_dump(mode="json", exclude={"id"}, exclude_none=True)
        if self.id != _intent_id(values):
            raise ValueError("Shot Video intent ID must match canonical intent")
        return self


class ShotVideoIntentError(ValueError):
    """Base error for durable Shot Video intent failures."""


class ShotVideoIntentContextError(ShotVideoIntentError):
    """Raised when authored intent does not match current domain context."""


class ShotVideoIntentPersistenceError(ShotVideoIntentError):
    """Raised when an intent cannot be stored or loaded safely."""


def _validated_context(
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    asset_plan: StoryboardAssetPlan,
):
    try:
        current_product = Product.model_validate(product.model_dump())
        current_campaign = AdCampaign.model_validate(campaign.model_dump())
        current_storyboard = Storyboard.model_validate(storyboard.model_dump())
        current_plan = StoryboardAssetPlan.model_validate(asset_plan.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise ShotVideoIntentContextError(
            f"Shot Video intent context must be structurally valid: {error}"
        ) from error
    try:
        current_storyboard = validate_storyboard(current_storyboard, current_campaign)
    except StoryboardContextError as error:
        raise ShotVideoIntentContextError(str(error)) from error
    if current_campaign.claim_selection.product_id != current_product.id:
        raise ShotVideoIntentContextError(
            "Campaign does not belong to the supplied Product"
        )
    if current_plan.product_id != current_product.id:
        raise ShotVideoIntentContextError(
            "asset plan does not belong to the supplied Product"
        )
    if current_plan.campaign_id != current_campaign.id:
        raise ShotVideoIntentContextError(
            "asset plan does not belong to the supplied Campaign"
        )
    if current_plan.storyboard_id != current_storyboard.id:
        raise ShotVideoIntentContextError(
            "asset plan does not belong to the supplied Storyboard"
        )
    return current_product, current_campaign, current_storyboard, current_plan


def _validated_claim_ids(value: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ShotVideoIntentContextError(
            "motion-relevant claim IDs must be an ordered collection"
        )
    try:
        claim_ids = tuple(value)
    except TypeError as error:
        raise ShotVideoIntentContextError(
            "motion-relevant claim IDs must be an ordered collection"
        ) from error
    if any(type(item) is not str or not item.strip() for item in claim_ids):
        raise ShotVideoIntentContextError(
            "motion-relevant claim IDs must be non-empty strings"
        )
    if len(claim_ids) != len(set(claim_ids)):
        raise ShotVideoIntentContextError(
            "motion-relevant claim IDs must not contain duplicates"
        )
    return claim_ids


def _validated_duration(value: object) -> float:
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        raise ShotVideoIntentContextError(
            "requested generation duration must be a finite positive float"
        )
    return value


def create_shot_video_intent(
    *,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    asset_plan: StoryboardAssetPlan,
    shot_id: str,
    requested_generation_duration_seconds: float,
    motion: VideoMotionIntent,
    motion_relevant_claim_ids: Sequence[str] = (),
) -> ShotVideoIntent:
    """Create exact durable intent without invoking a provider."""

    (
        current_product,
        current_campaign,
        current_storyboard,
        current_plan,
    ) = _validated_context(product, campaign, storyboard, asset_plan)
    try:
        current_motion = VideoMotionIntent.model_validate(motion.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise ShotVideoIntentContextError(
            f"motion intent must be structurally valid: {error}"
        ) from error
    duration = _validated_duration(requested_generation_duration_seconds)
    claim_ids = _validated_claim_ids(motion_relevant_claim_ids)
    matching_shots = [shot for shot in current_storyboard.shots if shot.id == shot_id]
    if len(matching_shots) != 1:
        raise ShotVideoIntentContextError(
            f"Storyboard shot must resolve exactly once: {shot_id}"
        )
    matching_selections = [
        selection
        for selection in current_plan.shot_selections
        if selection.shot_id == shot_id
    ]
    if len(matching_selections) != 1:
        raise ShotVideoIntentContextError(
            f"Storyboard shot must have one prepared asset selection: {shot_id}"
        )
    shot_claim_ids = set(matching_shots[0].selected_claim_ids)
    if any(claim_id not in shot_claim_ids for claim_id in claim_ids):
        raise ShotVideoIntentContextError(
            "motion-relevant claims must be selected by the matching shot"
        )
    campaign_claim_ids = {item.claim_id for item in current_campaign.claim_snapshots}
    if any(claim_id not in campaign_claim_ids for claim_id in claim_ids):
        raise ShotVideoIntentContextError(
            "motion-relevant claims must have current Campaign snapshots"
        )
    values = {
        "schema_version": 1,
        "product_id": current_product.id,
        "campaign_id": current_campaign.id,
        "storyboard_id": current_storyboard.id,
        "asset_plan_id": current_plan.id,
        "shot_id": matching_shots[0].id,
        "requested_generation_duration_seconds": duration,
        "motion": current_motion.model_dump(mode="json", exclude_none=True),
        "motion_relevant_claim_ids": claim_ids,
    }
    return ShotVideoIntent(id=_intent_id(values), **values)


def validate_shot_video_intent(
    intent: ShotVideoIntent,
    product: Product,
    campaign: AdCampaign,
    storyboard: Storyboard,
    asset_plan: StoryboardAssetPlan,
) -> ShotVideoIntent:
    """Revalidate one durable intent against current exact domain inputs."""

    try:
        current = ShotVideoIntent.model_validate(intent.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise ShotVideoIntentContextError(
            f"Shot Video intent must be structurally valid: {error}"
        ) from error
    expected = create_shot_video_intent(
        product=product,
        campaign=campaign,
        storyboard=storyboard,
        asset_plan=asset_plan,
        shot_id=current.shot_id,
        requested_generation_duration_seconds=(
            current.requested_generation_duration_seconds
        ),
        motion=current.motion,
        motion_relevant_claim_ids=current.motion_relevant_claim_ids,
    )
    if current != expected:
        raise ShotVideoIntentContextError(
            "Shot Video intent no longer matches the current domain context"
        )
    return current.model_copy(deep=True)


def save_shot_video_intent(
    intent: ShotVideoIntent,
    path: Union[str, Path] = "shot_video_intent.json",
) -> Path:
    """Atomically write one human-readable Shot Video intent JSON document."""

    try:
        current = ShotVideoIntent.model_validate(intent.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise ShotVideoIntentPersistenceError(
            f"Shot Video intent is invalid: {error}"
        ) from error
    target = Path(path)
    temporary_path = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = current.model_dump_json(
            indent=2,
            ensure_ascii=False,
            exclude_none=True,
        ) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".shot-video-intent-",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(str(temporary_path), str(target))
        temporary_path = None
        return target.resolve()
    except OSError as error:
        raise ShotVideoIntentPersistenceError(
            f"Shot Video intent could not be saved: {target}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_shot_video_intent(
    path: Union[str, Path] = "shot_video_intent.json",
) -> ShotVideoIntent:
    """Load and strictly validate one Shot Video intent JSON document."""

    return ShotVideoIntent.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
