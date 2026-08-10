"""Typed storyboard domain bound to an existing ad campaign."""

import math
from pathlib import Path
from typing import List, Literal, Optional, Union

from pydantic import Field, ValidationError, field_validator, model_validator

from campaign import AdCampaign
from product import NonEmptyStr, ProductModel


ShotPurpose = Literal["hook", "feature", "lifestyle", "cta"]
OverlayKind = Literal["claim", "creative", "product_name", "cta"]
_DURATION_TOLERANCE_SECONDS = 0.5


class StoryboardTextOverlay(ProductModel):
    """Typed text intent that keeps factual wording in Campaign snapshots."""

    kind: OverlayKind
    text: Optional[NonEmptyStr] = None
    claim_id: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def require_kind_specific_content(self) -> "StoryboardTextOverlay":
        if self.kind == "claim":
            if self.claim_id is None or self.text is not None:
                raise ValueError(
                    "claim overlays require claim_id and forbid custom text"
                )
        elif self.kind in ("creative", "cta"):
            if self.text is None or self.claim_id is not None:
                raise ValueError(
                    f"{self.kind} overlays require text and forbid claim_id"
                )
        elif self.text is not None or self.claim_id is not None:
            raise ValueError(
                "product_name overlays forbid custom text and claim_id"
            )
        return self


class StoryboardShot(ProductModel):
    """One ordered timeline shot and its explicitly assigned campaign claims."""

    id: NonEmptyStr
    purpose: ShotPurpose
    duration_seconds: float = Field(gt=0, strict=True)
    selected_claim_ids: List[NonEmptyStr] = Field(default_factory=list)
    visual_description: NonEmptyStr
    camera_direction: Optional[NonEmptyStr] = None
    text_overlays: List[StoryboardTextOverlay] = Field(default_factory=list)

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def require_float_duration(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("duration_seconds must be a strict float")
        return value

    @field_validator("selected_claim_ids")
    @classmethod
    def require_unique_claim_ids(cls, value: List[str]) -> List[str]:
        if len(value) != len(set(value)):
            raise ValueError("shot selected_claim_ids must not contain duplicates")
        return value


class Storyboard(ProductModel):
    """Ordered shot plan containing no Product or generation-provider state."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    campaign_id: NonEmptyStr
    shots: List[StoryboardShot] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_shot_ids(self) -> "Storyboard":
        shot_ids = [shot.id for shot in self.shots]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("storyboard shot IDs must not contain duplicates")
        return self


class StoryboardContextError(ValueError):
    """Raised when a Storyboard is inconsistent with its Campaign context."""


def validate_storyboard(
    storyboard: Storyboard,
    campaign: AdCampaign,
) -> Storyboard:
    """Validate Campaign binding, claim use, and timeline duration, then copy."""

    try:
        validated_storyboard = Storyboard.model_validate(storyboard.model_dump())
    except ValidationError as error:
        raise StoryboardContextError(
            f"storyboard must be structurally valid: {error}"
        ) from error

    if validated_storyboard.campaign_id != campaign.id:
        raise StoryboardContextError(
            f"storyboard campaign ID {validated_storyboard.campaign_id} does not match "
            f"Campaign ID {campaign.id}"
        )
    if not validated_storyboard.shots:
        raise StoryboardContextError("storyboard must contain at least one shot")

    shot_ids = set()
    selected_campaign_claim_ids = set(
        campaign.claim_selection.selected_claim_ids
    )
    for shot in validated_storyboard.shots:
        if (
            type(shot.duration_seconds) is not float
            or not math.isfinite(shot.duration_seconds)
            or shot.duration_seconds <= 0
        ):
            raise StoryboardContextError(
                f"shot {shot.id} duration must be a finite positive float"
            )
        if shot.id in shot_ids:
            raise StoryboardContextError(f"duplicate storyboard shot ID: {shot.id}")
        shot_ids.add(shot.id)

        if len(shot.selected_claim_ids) != len(set(shot.selected_claim_ids)):
            raise StoryboardContextError(
                f"duplicate selected claim ID in shot {shot.id}"
            )
        for claim_id in shot.selected_claim_ids:
            if claim_id not in selected_campaign_claim_ids:
                raise StoryboardContextError(
                    f"claim {claim_id} in shot {shot.id} is not selected by Campaign"
                )
        for overlay in shot.text_overlays:
            if overlay.kind != "claim":
                continue
            if overlay.claim_id not in selected_campaign_claim_ids:
                raise StoryboardContextError(
                    f"claim overlay {overlay.claim_id} in shot {shot.id} is not "
                    "selected by Campaign"
                )
            if overlay.claim_id not in shot.selected_claim_ids:
                raise StoryboardContextError(
                    f"claim overlay {overlay.claim_id} must be assigned to the "
                    f"same shot {shot.id}"
                )

    total_duration = math.fsum(
        shot.duration_seconds for shot in validated_storyboard.shots
    )
    duration_delta = abs(total_duration - campaign.target_duration_seconds)
    if duration_delta > _DURATION_TOLERANCE_SECONDS:
        raise StoryboardContextError(
            f"storyboard duration {total_duration:g}s differs from Campaign target "
            f"{campaign.target_duration_seconds}s by more than "
            f"{_DURATION_TOLERANCE_SECONDS:g}s"
        )

    return validated_storyboard.model_copy(deep=True)


def save_storyboard(
    storyboard: Storyboard,
    path: Union[str, Path] = "storyboard.json",
) -> Path:
    """Serialize a Storyboard to readable UTF-8 JSON and return its path."""

    output_path = Path(path)
    json_text = storyboard.model_dump_json(
        indent=2,
        ensure_ascii=False,
        exclude_none=True,
    )
    output_path.write_text(json_text + "\n", encoding="utf-8")
    return output_path


def load_storyboard(
    path: Union[str, Path] = "storyboard.json",
) -> Storyboard:
    """Load and strictly validate a Storyboard from UTF-8 JSON."""

    input_path = Path(path)
    return Storyboard.model_validate_json(input_path.read_text(encoding="utf-8"))
