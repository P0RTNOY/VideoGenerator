"""Explicit handoff from approved discovery proposals to Feature 2.21."""

from __future__ import annotations

from typing import List, Literal, Sequence

from pydantic import Field, model_validator

from official_product_discovery import OfficialProductPageDiscovery
from product import NonEmptyStr, Product, ProductModel
from product_claim_verification import (
    ProductClaimVerificationReport,
    generate_product_claim_verification_report,
)
from product_identity import normalize_identity_url
from product_source_research import (
    MAX_SUPPLEMENTARY_PRODUCT_SOURCES,
    MultiSourceProductResearch,
    SupplementaryProductSourceResult,
    research_product_sources,
)
from supplementary_product_discovery import (
    SupplementaryProductSourceDiscovery,
    SupplementarySourceCandidate,
    SupplementarySourceProposal,
)


class DiscoverySelectionError(ValueError):
    """Raised when explicit proposal selection cannot be resolved safely."""


class SelectedDiscoveryVerification(ProductModel):
    """Derived research and verification for explicitly selected proposals."""

    schema_version: Literal[1] = 1
    primary_product_id: NonEmptyStr
    selected_proposal_urls: List[NonEmptyStr] = Field(
        default_factory=list,
        max_length=MAX_SUPPLEMENTARY_PRODUCT_SOURCES,
    )
    multi_source_research: MultiSourceProductResearch
    verification_report: ProductClaimVerificationReport

    @model_validator(mode="after")
    def require_consistent_derived_results(self) -> "SelectedDiscoveryVerification":
        if len(self.selected_proposal_urls) != len(set(self.selected_proposal_urls)):
            raise ValueError("selected proposal URLs must be unique")
        if self.primary_product_id != self.multi_source_research.primary_product.id:
            raise ValueError("multi-source research belongs to a different Product")
        if self.primary_product_id != self.verification_report.product_id:
            raise ValueError("verification report belongs to a different Product")
        if len(self.selected_proposal_urls) != len(
            self.multi_source_research.supplementary_sources
        ):
            raise ValueError(
                "selected proposal URLs must match supplementary source count"
            )
        return self


def _normalized_url(url: str, *, field_name: str) -> str:
    try:
        return normalize_identity_url(url)
    except (TypeError, ValueError) as error:
        raise DiscoverySelectionError(
            f"{field_name} must be an absolute HTTP(S) URL"
        ) from error


def _resolve_selected_proposals(
    primary_product: Product,
    primary_url: str,
    supplementary_discovery: SupplementaryProductSourceDiscovery,
    selected_urls: Sequence[str],
) -> List[SupplementarySourceProposal]:
    if supplementary_discovery.primary_product_id != primary_product.id:
        raise DiscoverySelectionError(
            "supplementary discovery belongs to a different Product"
        )
    if _normalized_url(
        supplementary_discovery.primary_url,
        field_name="supplementary primary URL",
    ) != _normalized_url(primary_url, field_name="primary URL"):
        raise DiscoverySelectionError(
            "supplementary discovery belongs to a different primary URL"
        )
    if isinstance(selected_urls, (str, bytes)):
        raise DiscoverySelectionError("selected_urls must be a sequence of URLs")
    try:
        selected = list(selected_urls)
    except TypeError as error:
        raise DiscoverySelectionError(
            "selected_urls must be a sequence of URLs"
        ) from error
    if any(not isinstance(url, str) or not url.strip() for url in selected):
        raise DiscoverySelectionError(
            "selected_urls must contain non-empty URL strings"
        )
    if len(selected) != len(set(selected)):
        raise DiscoverySelectionError("selected_urls must not contain duplicates")
    if len(selected) > MAX_SUPPLEMENTARY_PRODUCT_SOURCES:
        raise DiscoverySelectionError("at most three proposal URLs may be selected")
    accepted_urls = [
        proposal.accepted_url for proposal in supplementary_discovery.proposals
    ]
    if any(url not in accepted_urls for url in selected):
        raise DiscoverySelectionError(
            "every selected URL must be an accepted discovery proposal"
        )
    selected_set = set(selected)
    return [
        proposal
        for proposal in supplementary_discovery.proposals
        if proposal.accepted_url in selected_set
    ]


def _product_page_url(product: Product) -> str:
    for source in product.sources:
        if source.source_type == "product_page":
            return source.url
    if product.sources:
        return product.sources[0].url
    raise DiscoverySelectionError("Product must preserve its researched source URL")


def _accepted_candidate_for_proposal(
    supplementary_discovery: SupplementaryProductSourceDiscovery,
    proposal: SupplementarySourceProposal,
) -> SupplementarySourceCandidate:
    matches = [
        candidate
        for candidate in supplementary_discovery.candidates
        if candidate.status == "accepted"
        and candidate.discovered_url == proposal.discovered_url
        and candidate.final_url == proposal.accepted_url
        and candidate.publisher == proposal.publisher
    ]
    if len(matches) != 1:
        raise DiscoverySelectionError(
            "selected proposal is not bound to one accepted discovery candidate"
        )
    candidate = matches[0]
    if candidate.product is None or candidate.product != proposal.product:
        raise DiscoverySelectionError(
            "selected proposal Product does not match its discovery candidate"
        )
    reconciliation = candidate.reconciliation
    if (
        reconciliation is None
        or reconciliation.status != "matched"
        or reconciliation.relationship != "exact_product"
    ):
        raise DiscoverySelectionError(
            "selected proposal is not an accepted exact-product match"
        )
    if _normalized_url(
        _product_page_url(proposal.product),
        field_name="proposal Product source URL",
    ) != _normalized_url(
        proposal.accepted_url,
        field_name="proposal accepted URL",
    ):
        raise DiscoverySelectionError(
            "selected proposal Product does not preserve final URL provenance"
        )
    return candidate


def research_selected_discovery_sources(
    official_discovery: OfficialProductPageDiscovery,
    supplementary_discovery: SupplementaryProductSourceDiscovery,
    selected_urls: Sequence[str],
    *,
    timeout: float = 15,
    session: object = None,
) -> MultiSourceProductResearch:
    """Run Feature 2.21 only for caller-selected accepted proposal URLs."""

    if (
        official_discovery.status != "found"
        or official_discovery.accepted_url is None
        or official_discovery.product is None
    ):
        raise DiscoverySelectionError("official discovery must be found")
    selected_proposals = _resolve_selected_proposals(
        official_discovery.product,
        official_discovery.accepted_url,
        supplementary_discovery,
        selected_urls,
    )
    ordered_selected = [proposal.accepted_url for proposal in selected_proposals]
    result = research_product_sources(
        official_discovery.accepted_url,
        ordered_selected,
        timeout=timeout,
        session=session,
    )
    return result.model_copy(deep=True)


def verify_selected_discovery_sources(
    primary_product: Product,
    supplementary_discovery: SupplementaryProductSourceDiscovery,
    selected_urls: Sequence[str],
) -> SelectedDiscoveryVerification:
    """Verify explicitly selected proposals without refetching researched pages."""

    primary_url = _product_page_url(primary_product)
    selected_proposals = _resolve_selected_proposals(
        primary_product,
        primary_url,
        supplementary_discovery,
        selected_urls,
    )
    supplementary_sources = []
    for proposal in selected_proposals:
        candidate = _accepted_candidate_for_proposal(
            supplementary_discovery,
            proposal,
        )
        supplementary_sources.append(
            SupplementaryProductSourceResult(
                requested_url=proposal.discovered_url,
                product=proposal.product.model_copy(deep=True),
                reconciliation=candidate.reconciliation.model_copy(deep=True),
            )
        )
    research = MultiSourceProductResearch(
        primary_requested_url=supplementary_discovery.primary_url,
        primary_product=primary_product.model_copy(deep=True),
        supplementary_sources=supplementary_sources,
    )
    report = generate_product_claim_verification_report(research)
    return SelectedDiscoveryVerification(
        primary_product_id=primary_product.id,
        selected_proposal_urls=[
            proposal.accepted_url for proposal in selected_proposals
        ],
        multi_source_research=research,
        verification_report=report,
    )
