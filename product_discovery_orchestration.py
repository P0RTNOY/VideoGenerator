"""Explicit handoff from approved discovery proposals to Feature 2.21."""

from __future__ import annotations

from typing import Sequence

from official_product_discovery import OfficialProductPageDiscovery
from product_identity import normalize_identity_url
from product_source_research import (
    MAX_SUPPLEMENTARY_PRODUCT_SOURCES,
    MultiSourceProductResearch,
    research_product_sources,
)
from supplementary_product_discovery import SupplementaryProductSourceDiscovery


class DiscoverySelectionError(ValueError):
    """Raised when explicit proposal selection cannot be resolved safely."""


def _normalized_url(url: str, *, field_name: str) -> str:
    try:
        return normalize_identity_url(url)
    except (TypeError, ValueError) as error:
        raise DiscoverySelectionError(
            f"{field_name} must be an absolute HTTP(S) URL"
        ) from error


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
    if supplementary_discovery.primary_product_id != official_discovery.product.id:
        raise DiscoverySelectionError(
            "supplementary discovery belongs to a different Product"
        )
    if _normalized_url(
        supplementary_discovery.primary_url,
        field_name="supplementary primary URL",
    ) != _normalized_url(
        official_discovery.accepted_url,
        field_name="official accepted URL",
    ):
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
    unknown = [url for url in selected if url not in accepted_urls]
    if unknown:
        raise DiscoverySelectionError(
            "every selected URL must be an accepted discovery proposal"
        )
    selected_set = set(selected)
    ordered_selected = [url for url in accepted_urls if url in selected_set]
    result = research_product_sources(
        official_discovery.accepted_url,
        ordered_selected,
        timeout=timeout,
        session=session,
    )
    return result.model_copy(deep=True)
