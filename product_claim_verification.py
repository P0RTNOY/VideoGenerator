"""Derived claim corroboration reports for reconciled product research."""

from __future__ import annotations

import ipaddress
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Literal, Optional, Set, Union
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from product import (
    ClaimEvidence,
    NonEmptyStr,
    Product,
    ProductFeature,
    ProductModel,
    ProductSpec,
)
from product_source_research import (
    MultiSourceProductResearch,
    ProductRelationship,
    ProductResearchFailure,
    ReconciliationStatus,
)


ClaimType = Literal["spec", "feature"]
ClaimVerificationStatus = Literal[
    "supported",
    "corroborated",
    "conflicting",
    "ambiguous",
]
ObservationComparison = Literal["agrees", "conflicts", "ambiguous"]
SourceParticipation = Literal[
    "admitted",
    "same_publisher",
    "publisher_unknown",
    "not_matched",
    "ambiguous_reconciliation",
    "failed",
]
ClaimValue = Union[str, int, float, bool]

_COMMON_COUNTRY_SECOND_LEVEL_DOMAINS = {
    "ac",
    "co",
    "com",
    "edu",
    "gov",
    "net",
    "org",
}
_GROUP_LEVEL_SPEC_KEYS = {
    "immersion_protection",
    "playback_time",
    "water_resistance",
}
_GROUP_LEVEL_FEATURE_IDS = {
    "floating-design",
}
_QUALIFIER_PATTERNS = (
    r"\bup\s+to\b",
    r"\bat\s+least\b",
    r"\bapproximately\b",
    r"\bmaximum\b",
    r"\bminimum\b",
    r"\bunder\b",
)


class DuplicatePrimaryClaimError(ValueError):
    """Raised when primary claim identities cannot be joined deterministically."""


class ClaimVerificationLookupError(ValueError):
    """Raised when a report result cannot be resolved by claim ID."""


class ClaimVerificationObservation(ProductModel):
    """One relevant supplementary claim and its deterministic comparison."""

    requested_url: str = Field(strict=True)
    source_url: NonEmptyStr
    normalized_domain: NonEmptyStr
    relationship: ProductRelationship
    claim_id: NonEmptyStr
    observed_value: ClaimValue
    unit: Optional[NonEmptyStr] = None
    context: Optional[NonEmptyStr] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1, strict=True)
    evidence: Optional[ClaimEvidence] = None
    comparison: ObservationComparison


class ClaimVerificationSourceSummary(ProductModel):
    """Participation outcome for one Feature 2.21 supplementary source."""

    requested_url: str = Field(strict=True)
    source_role: Literal["supplementary"] = "supplementary"
    final_url: Optional[NonEmptyStr] = None
    normalized_domain: Optional[NonEmptyStr] = None
    reconciliation_status: Optional[ReconciliationStatus] = None
    relationship: Optional[ProductRelationship] = None
    participation: SourceParticipation
    failure: Optional[ProductResearchFailure] = None

    @model_validator(mode="after")
    def require_consistent_participation(self) -> "ClaimVerificationSourceSummary":
        if self.participation == "failed":
            if (
                self.failure is None
                or self.final_url is not None
                or self.normalized_domain is not None
                or self.reconciliation_status is not None
                or self.relationship is not None
            ):
                raise ValueError(
                    "failed source summary requires failure metadata only"
                )
            return self
        if self.failure is not None or self.final_url is None:
            raise ValueError(
                "researched source summary requires final URL without failure"
            )
        if self.participation in {
            "admitted",
            "same_publisher",
            "publisher_unknown",
        }:
            if self.reconciliation_status != "matched" or self.relationship is None:
                raise ValueError(
                    "participating source summary requires matched reconciliation"
                )
        elif self.participation == "not_matched":
            if self.reconciliation_status != "not_matched" or self.relationship is not None:
                raise ValueError(
                    "not-matched source summary requires not-matched reconciliation"
                )
        elif (
            self.reconciliation_status != "ambiguous"
            or self.relationship is not None
        ):
            raise ValueError(
                "ambiguous source summary requires ambiguous reconciliation"
            )
        return self


class ClaimVerificationResult(ProductModel):
    """Derived status for one primary Product claim."""

    claim_id: NonEmptyStr
    claim_type: ClaimType
    primary_value: ClaimValue
    unit: Optional[NonEmptyStr] = None
    context: Optional[NonEmptyStr] = None
    source_url: Optional[NonEmptyStr] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1, strict=True)
    primary_evidence: Optional[ClaimEvidence] = None
    status: ClaimVerificationStatus
    observations: List[ClaimVerificationObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_consistent_observations(self) -> "ClaimVerificationResult":
        expected_prefix = f"{self.claim_type}:"
        if not self.claim_id.startswith(expected_prefix):
            raise ValueError("claim ID must match claim type")
        if any(
            observation.claim_id != self.claim_id
            for observation in self.observations
        ):
            raise ValueError("observation claim ID must match result claim ID")
        if self.status != _status_for_observations(self.observations):
            raise ValueError("status does not match observation comparisons")
        return self


class ProductClaimVerificationReport(ProductModel):
    """Strict report derived from a multi-source research bundle."""

    schema_version: Literal[1] = 1
    product_id: NonEmptyStr
    primary_role: Literal["primary"] = "primary"
    primary_source_url: NonEmptyStr
    claim_results: List[ClaimVerificationResult] = Field(default_factory=list)
    source_summaries: List[ClaimVerificationSourceSummary] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def require_unique_claim_ids(self) -> "ProductClaimVerificationReport":
        claim_ids = [result.claim_id for result in self.claim_results]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_results must contain unique claim IDs")
        return self


def _copy_evidence(evidence: Optional[ClaimEvidence]) -> Optional[ClaimEvidence]:
    return evidence.model_copy(deep=True) if evidence is not None else None


def _final_source_url(product: Product, fallback: str) -> str:
    for source in product.sources:
        if source.source_type == "product_page":
            return source.url
    if product.sources:
        return product.sources[0].url
    return fallback


def normalize_publisher_domain(url: str) -> Optional[str]:
    """Return the conservative publisher identity used for source independence."""

    hostname = urlsplit(url).hostname
    if not hostname:
        return None
    hostname = hostname.casefold().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return hostname
    labels = [label for label in hostname.split(".") if label]
    if len(labels) <= 2:
        return hostname
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in _COMMON_COUNTRY_SECOND_LEVEL_DOMAINS
    ):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


_normalize_publisher_domain = normalize_publisher_domain


def _normalize_number_token(match: re.Match) -> str:
    token = match.group(0)
    try:
        number = Decimal(token)
    except InvalidOperation:
        return token
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _normalize_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = re.sub(r"\s+", " ", str(value).strip()).casefold()
    text = re.sub(r"(?<![\w.])[+-]?\d+(?:\.\d+)?", _normalize_number_token, text)
    return re.sub(r"(?<=\d)\s+(?=[a-z])", "", text)


def _normalized_spec_value(spec: ProductSpec) -> str:
    value = _normalize_text(spec.value)
    if not spec.unit:
        return value
    unit = _normalize_text(spec.unit)
    compact_value = re.sub(r"\s+", "", value)
    compact_unit = re.sub(r"\s+", "", unit)
    if compact_value.endswith(compact_unit):
        return compact_value
    return f"{compact_value}{compact_unit}"


def _without_qualifiers(value: str) -> str:
    normalized = value
    for pattern in _QUALIFIER_PATTERNS:
        normalized = re.sub(pattern, "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _numeric_tokens(value: str) -> List[str]:
    return re.findall(r"[+-]?\d+(?:\.\d+)?", value)


def _unit_tokens(value: str) -> List[str]:
    return re.findall(r"(?<=\d)([a-z]+)", value)


def _numeric_skeleton(value: str) -> str:
    return re.sub(r"[+-]?\d+(?:\.\d+)?", "#", value)


def _compare_specs(
    primary: ProductSpec,
    supplementary: ProductSpec,
) -> ObservationComparison:
    primary_value = _normalized_spec_value(primary)
    supplementary_value = _normalized_spec_value(supplementary)
    if primary_value == supplementary_value:
        return "agrees"

    primary_unqualified = _without_qualifiers(primary_value)
    supplementary_unqualified = _without_qualifiers(supplementary_value)
    if primary_unqualified == supplementary_unqualified:
        return "ambiguous"

    primary_units = _unit_tokens(primary_unqualified)
    supplementary_units = _unit_tokens(supplementary_unqualified)
    if primary_units and supplementary_units and primary_units != supplementary_units:
        return "ambiguous"

    primary_numbers = _numeric_tokens(primary_unqualified)
    supplementary_numbers = _numeric_tokens(supplementary_unqualified)
    if bool(primary_numbers) != bool(supplementary_numbers):
        return "ambiguous"
    if primary_numbers and supplementary_numbers:
        if _numeric_skeleton(primary_unqualified) == _numeric_skeleton(
            supplementary_unqualified
        ):
            return "conflicts"
        return "ambiguous"
    return "conflicts"


def _compare_features(
    primary: ProductFeature,
    supplementary: ProductFeature,
) -> ObservationComparison:
    if _normalize_text(primary.name) != _normalize_text(supplementary.name):
        return "ambiguous"
    if primary.description is None and supplementary.description is None:
        return "agrees"
    if primary.description is not None and supplementary.description is not None:
        if _normalize_text(primary.description) == _normalize_text(
            supplementary.description
        ):
            return "agrees"
    return "ambiguous"


def _source_summaries(
    research: MultiSourceProductResearch,
    primary_domain: Optional[str],
) -> List[ClaimVerificationSourceSummary]:
    summaries: List[ClaimVerificationSourceSummary] = []
    for source in research.supplementary_sources:
        if source.failure is not None:
            summaries.append(
                ClaimVerificationSourceSummary(
                    requested_url=source.requested_url,
                    participation="failed",
                    failure=source.failure.model_copy(deep=True),
                )
            )
            continue

        reconciliation = source.reconciliation
        product = source.product
        if reconciliation is None or product is None:
            continue
        final_url = _final_source_url(product, source.requested_url)
        normalized_domain = _normalize_publisher_domain(final_url)
        if reconciliation.status == "not_matched":
            participation: SourceParticipation = "not_matched"
        elif reconciliation.status == "ambiguous":
            participation = "ambiguous_reconciliation"
        elif normalized_domain is None or primary_domain is None:
            participation = "publisher_unknown"
        elif normalized_domain == primary_domain:
            participation = "same_publisher"
        else:
            participation = "admitted"
        summaries.append(
            ClaimVerificationSourceSummary(
                requested_url=source.requested_url,
                final_url=final_url,
                normalized_domain=normalized_domain,
                reconciliation_status=reconciliation.status,
                relationship=reconciliation.relationship,
                participation=participation,
            )
        )
    return summaries


def _observation_from_spec(
    source_url: str,
    normalized_domain: str,
    requested_url: str,
    relationship: ProductRelationship,
    primary: ProductSpec,
    supplementary: ProductSpec,
) -> ClaimVerificationObservation:
    comparison: ObservationComparison
    if relationship == "product_family" and primary.key not in _GROUP_LEVEL_SPEC_KEYS:
        comparison = "ambiguous"
    else:
        comparison = _compare_specs(primary, supplementary)
    return ClaimVerificationObservation(
        requested_url=requested_url,
        source_url=source_url,
        normalized_domain=normalized_domain,
        relationship=relationship,
        claim_id=f"spec:{supplementary.key}",
        observed_value=supplementary.value,
        unit=supplementary.unit,
        confidence=supplementary.confidence,
        evidence=_copy_evidence(supplementary.evidence),
        comparison=comparison,
    )


def _observation_from_feature(
    source_url: str,
    normalized_domain: str,
    requested_url: str,
    relationship: ProductRelationship,
    primary: ProductFeature,
    supplementary: ProductFeature,
) -> ClaimVerificationObservation:
    comparison: ObservationComparison
    if (
        relationship == "product_family"
        and primary.id not in _GROUP_LEVEL_FEATURE_IDS
    ):
        comparison = "ambiguous"
    else:
        comparison = _compare_features(primary, supplementary)
    return ClaimVerificationObservation(
        requested_url=requested_url,
        source_url=source_url,
        normalized_domain=normalized_domain,
        relationship=relationship,
        claim_id=f"feature:{supplementary.id}",
        observed_value=supplementary.name,
        context=supplementary.description,
        confidence=supplementary.confidence,
        evidence=_copy_evidence(supplementary.evidence),
        comparison=comparison,
    )


def _status_for_observations(
    observations: List[ClaimVerificationObservation],
) -> ClaimVerificationStatus:
    comparisons = {observation.comparison for observation in observations}
    if "conflicts" in comparisons:
        return "conflicting"
    if "agrees" in comparisons:
        return "corroborated"
    if "ambiguous" in comparisons:
        return "ambiguous"
    return "supported"


def generate_product_claim_verification_report(
    research: MultiSourceProductResearch,
) -> ProductClaimVerificationReport:
    """Build a report without mutating the research bundle or its Products."""

    primary = research.primary_product
    primary_source_url = _final_source_url(primary, research.primary_requested_url)
    primary_domain = _normalize_publisher_domain(primary_source_url)
    claim_results: List[ClaimVerificationResult] = []
    seen_claim_ids: Set[str] = set()
    for spec in primary.specs:
        claim_id = f"spec:{spec.key}"
        if claim_id in seen_claim_ids:
            raise DuplicatePrimaryClaimError(
                f"duplicate primary claim ID: {claim_id}"
            )
        seen_claim_ids.add(claim_id)
        claim_results.append(
            ClaimVerificationResult(
                claim_id=claim_id,
                claim_type="spec",
                primary_value=spec.value,
                unit=spec.unit,
                source_url=spec.source_url,
                confidence=spec.confidence,
                primary_evidence=_copy_evidence(spec.evidence),
                status="supported",
            )
        )
    for feature in primary.features:
        claim_id = f"feature:{feature.id}"
        if claim_id in seen_claim_ids:
            raise DuplicatePrimaryClaimError(
                f"duplicate primary claim ID: {claim_id}"
            )
        seen_claim_ids.add(claim_id)
        claim_results.append(
            ClaimVerificationResult(
                claim_id=claim_id,
                claim_type="feature",
                primary_value=feature.name,
                context=feature.description,
                source_url=feature.source_url,
                confidence=feature.confidence,
                primary_evidence=_copy_evidence(feature.evidence),
                status="supported",
            )
        )

    source_summaries = _source_summaries(research, primary_domain)
    for source, summary in zip(research.supplementary_sources, source_summaries):
        if summary.participation != "admitted":
            continue
        product = source.product
        reconciliation = source.reconciliation
        if (
            product is None
            or reconciliation is None
            or reconciliation.relationship is None
            or summary.final_url is None
            or summary.normalized_domain is None
        ):
            continue
        specs_by_key: Dict[str, List[ProductSpec]] = {}
        for spec in product.specs:
            specs_by_key.setdefault(spec.key, []).append(spec)
        features_by_id: Dict[str, List[ProductFeature]] = {}
        for feature in product.features:
            features_by_id.setdefault(feature.id, []).append(feature)
        for result, primary_spec in zip(claim_results, primary.specs):
            for supplementary_spec in specs_by_key.get(primary_spec.key, []):
                result.observations.append(
                    _observation_from_spec(
                        summary.final_url,
                        summary.normalized_domain,
                        source.requested_url,
                        reconciliation.relationship,
                        primary_spec,
                        supplementary_spec,
                    )
                )
        feature_offset = len(primary.specs)
        for index, primary_feature in enumerate(primary.features):
            for supplementary_feature in features_by_id.get(primary_feature.id, []):
                claim_results[feature_offset + index].observations.append(
                    _observation_from_feature(
                        summary.final_url,
                        summary.normalized_domain,
                        source.requested_url,
                        reconciliation.relationship,
                        primary_feature,
                        supplementary_feature,
                    )
                )

    for result in claim_results:
        result.status = _status_for_observations(result.observations)

    return ProductClaimVerificationReport(
        product_id=primary.id,
        primary_source_url=primary_source_url,
        claim_results=claim_results,
        source_summaries=source_summaries,
    )


def resolve_claim_verification_result(
    report: ProductClaimVerificationReport,
    claim_id: str,
) -> ClaimVerificationResult:
    """Resolve one existing claim result by AdClaimCandidate-compatible ID."""

    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ClaimVerificationLookupError(
            "claim verification result ID must be a non-empty string"
        )
    for result in report.claim_results:
        if result.claim_id == claim_id:
            return result.model_copy(deep=True)
    raise ClaimVerificationLookupError(
        f"unknown claim verification result: {claim_id}"
    )
