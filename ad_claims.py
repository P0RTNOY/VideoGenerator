"""Deterministic projection from researched products to selectable ad claims."""

import re
from typing import List, Literal, Optional, Set

from pydantic import Field

from product import (
    ClaimEvidence,
    NonEmptyStr,
    Product,
    ProductFeature,
    ProductModel,
    ProductSpec,
)


ClaimType = Literal["spec", "feature"]


class AdClaimCandidate(ProductModel):
    """A source-linked product fact presented for explicit user selection."""

    id: NonEmptyStr
    display_text: NonEmptyStr
    claim_type: ClaimType
    source_id: NonEmptyStr
    source_url: Optional[NonEmptyStr] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1, strict=True)
    verified: bool = False
    evidence: Optional[ClaimEvidence] = None
    context_text: Optional[NonEmptyStr] = None


class DuplicateAdClaimCandidateError(ValueError):
    """Raised when a Product cannot produce unique deterministic claim IDs."""


def _format_primitive(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _ends_with_unit(value_text: str, unit: str) -> bool:
    return bool(
        re.search(
            rf"(?:^|[\s\d]){re.escape(unit)}$",
            value_text,
            re.IGNORECASE,
        )
    )


def _format_measurement(spec: ProductSpec, *, compact: bool = False) -> str:
    value_text = _format_primitive(spec.value)
    if not spec.unit or _ends_with_unit(value_text, spec.unit):
        return value_text
    separator = "" if compact else " "
    return f"{value_text}{separator}{spec.unit}"


def _uppercase_initial(value: str) -> str:
    return f"{value[:1].upper()}{value[1:]}"


def _format_spec_display(spec: ProductSpec) -> str:
    if spec.key == "water_resistance":
        return f"{_format_measurement(spec)} water resistance"
    if spec.key == "playback_time":
        value = _uppercase_initial(_format_measurement(spec))
        return f"{value} playback"
    if spec.key == "output_power":
        return f"{_format_measurement(spec, compact=True)} output"
    if spec.key == "immersion_protection":
        return f"Immersion protection: {_format_measurement(spec)}"
    return f"{spec.label}: {_format_measurement(spec)}"


def _copy_evidence(evidence: Optional[ClaimEvidence]) -> Optional[ClaimEvidence]:
    return evidence.model_copy(deep=True) if evidence is not None else None


def _candidate_from_spec(spec: ProductSpec) -> AdClaimCandidate:
    return AdClaimCandidate(
        id=f"spec:{spec.key}",
        display_text=_format_spec_display(spec),
        claim_type="spec",
        source_id=spec.key,
        source_url=spec.source_url,
        confidence=spec.confidence,
        verified=spec.verified,
        evidence=_copy_evidence(spec.evidence),
    )


def _candidate_from_feature(feature: ProductFeature) -> AdClaimCandidate:
    return AdClaimCandidate(
        id=f"feature:{feature.id}",
        display_text=feature.name,
        claim_type="feature",
        source_id=feature.id,
        source_url=feature.source_url,
        confidence=feature.confidence,
        verified=feature.verified,
        evidence=_copy_evidence(feature.evidence),
        context_text=feature.description,
    )


def generate_ad_claim_candidates(product: Product) -> List[AdClaimCandidate]:
    """Return selectable claims in Product source order without mutating Product."""

    candidates: List[AdClaimCandidate] = []
    seen_ids: Set[str] = set()

    for candidate in (
        *(_candidate_from_spec(spec) for spec in product.specs),
        *(_candidate_from_feature(feature) for feature in product.features),
    ):
        if candidate.id in seen_ids:
            raise DuplicateAdClaimCandidateError(
                f"duplicate ad claim candidate ID: {candidate.id}"
            )
        seen_ids.add(candidate.id)
        candidates.append(candidate)

    return candidates
