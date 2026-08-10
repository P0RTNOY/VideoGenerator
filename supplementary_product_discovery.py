"""Bounded discovery of exact-product direct-retailer pages."""

from __future__ import annotations

import json
import re
import unicodedata
from itertools import islice
from typing import Any, Dict, Iterable, List, Literal, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pydantic import Field, model_validator

from official_product_discovery import (
    MAX_RAW_RESULTS_PER_QUERY,
    MAX_SEARCH_QUERIES,
    OfficialProductPageSearchError,
    ProductPageSearchProvider,
    ProductPageSearchResult,
    _url_rejection_reason,
)
from product import NonEmptyStr, Product, ProductModel
from product_claim_verification import normalize_publisher_domain
from product_identity import normalize_identity_url
from product_research import ProductResearchError, research_product_page
from product_source_research import ProductReconciliation, reconcile_products


MAX_FETCHED_SUPPLEMENTARY_CANDIDATES = 6
MAX_ACCEPTED_SUPPLEMENTARY_SOURCES = 3

DiscoveryStatus = Literal["found", "not_found"]
SourceClassification = Literal[
    "direct_retailer_product",
    "marketplace_or_third_party",
    "editorial_or_review",
    "unsuitable",
    "ambiguous",
]
CandidateStatus = Literal[
    "accepted",
    "excluded_primary_publisher",
    "rejected_url",
    "research_failed",
    "identity_mismatch",
    "rejected_source_type",
    "duplicate_publisher",
    "proposal_limit",
    "not_fetched",
]


class SupplementarySourceClassification(ProductModel):
    source_type: SourceClassification
    reasons: List[NonEmptyStr] = Field(min_length=1)


class SupplementarySourceCandidate(ProductModel):
    discovered_url: NonEmptyStr
    query: NonEmptyStr
    search_rank: int = Field(gt=0, strict=True)
    status: CandidateStatus
    reason: NonEmptyStr
    final_url: Optional[NonEmptyStr] = None
    publisher: Optional[NonEmptyStr] = None
    source_classification: Optional[SourceClassification] = None
    classification_reasons: List[NonEmptyStr] = Field(default_factory=list)
    product: Optional[Product] = None
    reconciliation: Optional[ProductReconciliation] = None

    @model_validator(mode="after")
    def require_complete_accepted_candidate(self) -> "SupplementarySourceCandidate":
        if self.status != "accepted":
            return self
        valid_reconciliation = (
            self.reconciliation is not None
            and self.reconciliation.status == "matched"
            and self.reconciliation.relationship == "exact_product"
        )
        if not (
            self.final_url
            and self.publisher
            and self.product is not None
            and self.source_classification == "direct_retailer_product"
            and self.classification_reasons
            and valid_reconciliation
        ):
            raise ValueError(
                "accepted candidate requires exact direct-retailer validation"
            )
        return self


class SupplementarySourceProposal(ProductModel):
    discovered_url: NonEmptyStr
    accepted_url: NonEmptyStr
    publisher: NonEmptyStr
    product: Product


class SupplementaryProductSourceDiscovery(ProductModel):
    schema_version: Literal[1] = 1
    primary_product_id: NonEmptyStr
    primary_url: NonEmptyStr
    status: DiscoveryStatus
    queries: List[NonEmptyStr] = Field(max_length=MAX_SEARCH_QUERIES)
    proposals: List[SupplementarySourceProposal] = Field(
        default_factory=list,
        max_length=MAX_ACCEPTED_SUPPLEMENTARY_SOURCES,
    )
    candidates: List[SupplementarySourceCandidate] = Field(default_factory=list)
    reason: NonEmptyStr

    @model_validator(mode="after")
    def require_proposals_only_when_found(self) -> "SupplementaryProductSourceDiscovery":
        if self.status == "found" and not self.proposals:
            raise ValueError("found discovery requires at least one proposal")
        if self.status == "not_found" and self.proposals:
            raise ValueError("not-found discovery cannot contain proposals")
        publishers = [proposal.publisher for proposal in self.proposals]
        if len(publishers) != len(set(publishers)):
            raise ValueError("proposals must contain unique publishers")
        proposal_keys = {
            (
                proposal.discovered_url,
                proposal.accepted_url,
                proposal.publisher,
            )
            for proposal in self.proposals
        }
        accepted_candidate_keys = {
            (
                candidate.discovered_url,
                candidate.final_url,
                candidate.publisher,
            )
            for candidate in self.candidates
            if candidate.status == "accepted"
        }
        if proposal_keys != accepted_candidate_keys:
            raise ValueError(
                "proposals must correspond exactly to accepted candidates"
            )
        return self


def _ascii_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_text.casefold())


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _schema_types(value: Any) -> set:
    result = set()
    for item in _as_list(value):
        if isinstance(item, str):
            result.add(item.rstrip("/").rsplit("/", 1)[-1].casefold())
    return result


def _jsonld_entities(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            document = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        roots = document if isinstance(document, list) else [document]
        for root in roots:
            if not isinstance(root, dict):
                continue
            graph = root.get("@graph")
            if isinstance(graph, list):
                entities.extend(item for item in graph if isinstance(item, dict))
            else:
                entities.append(root)
    return entities


def _seller_names(value: Any) -> set:
    names = set()
    for seller in _as_list(value):
        if isinstance(seller, dict):
            name = seller.get("name")
        else:
            name = seller
        if isinstance(name, str) and name.strip():
            names.add(_ascii_identifier(name))
    return names


def _structured_urls(value: Any) -> List[str]:
    urls = []
    for item in _as_list(value):
        if isinstance(item, dict):
            candidates = (item.get("url"), item.get("@id"))
        else:
            candidates = (item,)
        urls.extend(
            candidate.strip()
            for candidate in candidates
            if isinstance(candidate, str) and candidate.strip()
        )
    return urls


def _same_unfragmented_page(candidate_url: str, final_url: str) -> bool:
    if urlsplit(candidate_url).fragment:
        return False
    try:
        return normalize_identity_url(candidate_url) == normalize_identity_url(
            final_url
        )
    except ValueError:
        return False


def _walk_mappings(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mappings(nested)


def classify_supplementary_product_page(
    html: str,
    final_url: str,
) -> SupplementarySourceClassification:
    """Classify only strongly supported direct-retailer product pages."""

    soup = BeautifulSoup(html, "html.parser")
    entities = _jsonld_entities(soup)
    product_entities = [
        entity
        for entity in entities
        if _schema_types(entity.get("@type")) & {"product", "productgroup"}
    ]
    offers = [
        offer
        for entity in product_entities
        for offer in _walk_mappings(entity.get("offers"))
        if _schema_types(offer.get("@type")) & {"offer", "aggregateoffer"}
        or any(
            key in offer
            for key in (
                "availability",
                "highPrice",
                "lowPrice",
                "offerCount",
                "price",
                "priceCurrency",
                "seller",
            )
        )
    ]
    seller_names = {
        seller
        for offer in offers
        for seller in _seller_names(offer.get("seller"))
    }
    visible_seller_names = {
        _ascii_identifier(
            str(element.get("data-seller") or element.get_text(" ", strip=True))
        )
        for element in soup.select("[data-seller], [itemprop='seller']")
        if str(element.get("data-seller") or element.get_text(" ", strip=True)).strip()
    }
    seller_names.update(visible_seller_names)
    publisher = normalize_publisher_domain(final_url)
    publisher_label = (
        _ascii_identifier(publisher.split(".")[0]) if publisher else ""
    )
    editorial_types = {"article", "newsarticle", "review"}
    editorial_entities = [
        entity
        for entity in entities
        if _schema_types(entity.get("@type")) & editorial_types
    ]
    has_primary_editorial_schema = bool(editorial_entities) and (
        not product_entities
        or any(
            _same_unfragmented_page(candidate_url, final_url)
            for entity in editorial_entities
            for candidate_url in (
                *_structured_urls(entity.get("mainEntityOfPage")),
                *_structured_urls(entity.get("url")),
            )
        )
    )
    open_graph_type = soup.find(
        "meta",
        attrs={"property": lambda value: str(value).casefold() == "og:type"},
    )
    is_article_meta = bool(
        open_graph_type
        and str(open_graph_type.get("content", "")).casefold()
        in {"article", "review"}
    )
    article_heading = soup.select_one("article h1")
    has_editorial_heading = bool(
        article_heading
        and re.search(
            r"\b(review|comparison|buying\s+guide|versus|vs\.?)\b",
            article_heading.get_text(" ", strip=True),
            re.IGNORECASE,
        )
    )
    if has_primary_editorial_schema or is_article_meta or has_editorial_heading:
        reasons = []
        if has_primary_editorial_schema:
            reasons.append("primary_editorial_schema")
        if is_article_meta:
            reasons.append("article_page_metadata")
        if has_editorial_heading:
            reasons.append("editorial_article_heading")
        return SupplementarySourceClassification(
            source_type="editorial_or_review",
            reasons=reasons,
        )

    aggregate_offer = any(
        "aggregateoffer" in _schema_types(offer.get("@type"))
        and (
            int(offer.get("offerCount", 0) or 0) > 1
            if str(offer.get("offerCount", "")).isdigit()
            else bool(offer.get("offers"))
        )
        for offer in offers
    )
    if aggregate_offer or len(seller_names) > 1:
        reasons = []
        if aggregate_offer:
            reasons.append("aggregate_offer")
        if len(seller_names) > 1:
            reasons.append("multiple_sellers")
        return SupplementarySourceClassification(
            source_type="marketplace_or_third_party",
            reasons=reasons,
        )
    has_offer = any(
        "offer" in _schema_types(offer.get("@type"))
        or any(key in offer for key in ("price", "availability", "priceCurrency"))
        for offer in offers
    )
    commerce_control = soup.find(
        lambda tag: tag.name in {"button", "input", "a"}
        and re.search(
            r"\b(add\s+to\s+(?:cart|bag)|buy\s+now)\b",
            " ".join(
                str(value)
                for value in (
                    tag.get_text(" ", strip=True),
                    tag.get("value"),
                    tag.get("aria-label"),
                )
                if value
            ),
            re.IGNORECASE,
        )
    )
    price_signal = soup.find(
        lambda tag: (
            tag.get("itemprop") == "price"
            or str(tag.get("property", "")).casefold().startswith("product:price")
            or str(tag.get("class", "")).casefold().find("price") >= 0
        )
    )
    seller_matches_publisher = not seller_names or any(
        seller == publisher_label
        for seller in seller_names
        if seller and publisher_label
    )
    if seller_names and not seller_matches_publisher:
        return SupplementarySourceClassification(
            source_type="ambiguous",
            reasons=["seller_publisher_mismatch"],
        )
    if (
        product_entities
        and has_offer
        and commerce_control is not None
        and price_signal is not None
        and seller_matches_publisher
    ):
        return SupplementarySourceClassification(
            source_type="direct_retailer_product",
            reasons=[
                "structured_product",
                "structured_offer",
                "commerce_control",
                "price_signal",
            ],
        )
    return SupplementarySourceClassification(
        source_type="ambiguous",
        reasons=["insufficient_direct_retailer_signals"],
    )


def _primary_url(product: Product) -> str:
    for source in product.sources:
        if source.source_type == "product_page":
            return source.url
    if product.sources:
        return product.sources[0].url
    raise ValueError("primary Product requires a source URL")


def _queries(product: Product) -> List[str]:
    base = f"{product.brand} {product.name}"
    model_number = product.variant.model_number if product.variant else None
    if model_number and _ascii_identifier(model_number) not in _ascii_identifier(base):
        base = f"{base} {model_number}"
    return [base, f"{base} price", f"{base} in stock"]


def _candidate_strength(candidate: SupplementarySourceCandidate) -> tuple:
    reconciliation_strength = int(
        candidate.reconciliation is not None
        and candidate.reconciliation.reason == "shared_model_number"
    )
    try:
        direct_final_url = normalize_identity_url(
            candidate.discovered_url
        ) == normalize_identity_url(candidate.final_url or "")
    except ValueError:
        direct_final_url = False
    return (
        reconciliation_strength,
        int(direct_final_url),
        len(candidate.classification_reasons),
    )


def _search_results(
    provider: ProductPageSearchProvider,
    queries: Iterable[str],
) -> List[tuple]:
    results = []
    seen_urls = set()
    for query in list(queries)[:MAX_SEARCH_QUERIES]:
        try:
            raw_results = provider.search(query, limit=MAX_RAW_RESULTS_PER_QUERY)
            bounded = list(islice(raw_results, MAX_RAW_RESULTS_PER_QUERY))
        except Exception as error:
            raise OfficialProductPageSearchError(
                f"search provider failed for query {query!r}: {error}"
            ) from error
        for rank, raw_result in enumerate(bounded, start=1):
            try:
                result = ProductPageSearchResult.model_validate(raw_result)
            except Exception as error:
                raise OfficialProductPageSearchError(
                    f"search provider returned an invalid result for "
                    f"query {query!r} at rank {rank}: {error}"
                ) from error
            try:
                normalized_url = normalize_identity_url(result.url)
            except ValueError:
                normalized_url = result.url
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            results.append((query, rank, result))
    return results


def discover_supplementary_product_sources(
    primary_product: Product,
    provider: ProductPageSearchProvider,
    *,
    timeout: float = 15,
    session: object = None,
) -> SupplementaryProductSourceDiscovery:
    """Propose independently validated exact-product retailer pages only."""

    primary = primary_product.model_copy(deep=True)
    primary_url = _primary_url(primary)
    primary_publisher = normalize_publisher_domain(primary_url)
    queries = _queries(primary)[:MAX_SEARCH_QUERIES]
    candidates: List[SupplementarySourceCandidate] = []
    eligible = []
    fetched = 0

    for query, rank, result in _search_results(provider, queries):
        discovered_publisher = normalize_publisher_domain(result.url)
        if discovered_publisher and discovered_publisher == primary_publisher:
            candidates.append(
                SupplementarySourceCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    status="excluded_primary_publisher",
                    reason="same_publisher_as_official_primary",
                    publisher=discovered_publisher,
                )
            )
            continue
        url_rejection = _url_rejection_reason(result.url)
        if url_rejection is not None:
            candidates.append(
                SupplementarySourceCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    status="rejected_url",
                    reason=url_rejection,
                    publisher=discovered_publisher,
                )
            )
            continue
        if fetched >= MAX_FETCHED_SUPPLEMENTARY_CANDIDATES:
            candidates.append(
                SupplementarySourceCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    status="not_fetched",
                    reason="candidate_fetch_limit",
                    publisher=discovered_publisher,
                )
            )
            continue
        fetched += 1
        try:
            page = research_product_page(
                result.url,
                timeout=timeout,
                session=session,
            )
        except ProductResearchError as error:
            candidates.append(
                SupplementarySourceCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    status="research_failed",
                    reason=str(error) or type(error).__name__,
                    publisher=discovered_publisher,
                )
            )
            continue

        product = page.product.model_copy(deep=True)
        reconciliation = reconcile_products(primary, product)
        publisher = normalize_publisher_domain(page.url)
        final_url_rejection = _url_rejection_reason(page.url)
        if final_url_rejection is not None:
            candidates.append(
                SupplementarySourceCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    status="rejected_url",
                    reason=final_url_rejection,
                    final_url=page.url,
                    publisher=publisher,
                    product=product,
                    reconciliation=reconciliation,
                )
            )
            continue
        classification = classify_supplementary_product_page(page.html, page.url)
        if publisher and publisher == primary_publisher:
            candidates.append(
                SupplementarySourceCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    status="excluded_primary_publisher",
                    reason="same_publisher_as_official_primary",
                    final_url=page.url,
                    publisher=publisher,
                    source_classification=classification.source_type,
                    classification_reasons=classification.reasons,
                    product=product,
                    reconciliation=reconciliation,
                )
            )
            continue
        if reconciliation.status != "matched" or reconciliation.relationship != "exact_product":
            mismatch_reason = (
                "product_family_not_accepted"
                if reconciliation.status == "matched"
                and reconciliation.relationship == "product_family"
                else reconciliation.reason
            )
            candidate = SupplementarySourceCandidate(
                discovered_url=result.url,
                query=query,
                search_rank=rank,
                status="identity_mismatch",
                reason=mismatch_reason,
                final_url=page.url,
                publisher=publisher,
                source_classification=classification.source_type,
                classification_reasons=classification.reasons,
                product=product,
                reconciliation=reconciliation,
            )
            candidates.append(candidate)
            continue
        if classification.source_type != "direct_retailer_product" or not publisher:
            candidates.append(
                SupplementarySourceCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    status="rejected_source_type",
                    reason=classification.reasons[0],
                    final_url=page.url,
                    publisher=publisher,
                    source_classification=classification.source_type,
                    classification_reasons=classification.reasons,
                    product=product,
                    reconciliation=reconciliation,
                )
            )
            continue
        candidate = SupplementarySourceCandidate(
            discovered_url=result.url,
            query=query,
            search_rank=rank,
            status="accepted",
            reason="exact_product_direct_retailer",
            final_url=page.url,
            publisher=publisher,
            source_classification=classification.source_type,
            classification_reasons=classification.reasons,
            product=product,
            reconciliation=reconciliation,
        )
        candidates.append(candidate)
        eligible.append(
            (
                len(candidates) - 1,
                candidate,
                _candidate_strength(candidate),
            )
        )

    proposals = []
    strongest_by_publisher = {}
    for candidate_index, candidate, strength in eligible:
        assert candidate.publisher is not None
        strongest = strongest_by_publisher.get(candidate.publisher)
        if strongest is None or strength > strongest[2]:
            strongest_by_publisher[candidate.publisher] = (
                candidate_index,
                candidate,
                strength,
            )

    winning_indices = {
        candidate_index
        for candidate_index, _, _ in strongest_by_publisher.values()
    }
    for candidate_index, candidate, _ in eligible:
        if candidate_index not in winning_indices:
            candidates[candidate_index] = candidate.model_copy(
                update={
                    "status": "duplicate_publisher",
                    "reason": "publisher_already_proposed",
                },
                deep=True,
            )

    for candidate_index, candidate, _ in sorted(
        strongest_by_publisher.values(),
        key=lambda item: item[0],
    ):
        assert candidate.publisher is not None
        assert candidate.final_url is not None
        assert candidate.product is not None
        if len(proposals) >= MAX_ACCEPTED_SUPPLEMENTARY_SOURCES:
            candidates[candidate_index] = candidate.model_copy(
                update={
                    "status": "proposal_limit",
                    "reason": "accepted_source_limit",
                },
                deep=True,
            )
            continue
        proposals.append(
            SupplementarySourceProposal(
                discovered_url=candidate.discovered_url,
                accepted_url=candidate.final_url,
                publisher=candidate.publisher,
                product=candidate.product.model_copy(deep=True),
            )
        )

    status: DiscoveryStatus = "found" if proposals else "not_found"
    return SupplementaryProductSourceDiscovery(
        primary_product_id=primary.id,
        primary_url=primary_url,
        status=status,
        queries=queries,
        proposals=proposals,
        candidates=candidates,
        reason=(
            "direct_retailer_product_sources_proposed"
            if proposals
            else "no_direct_retailer_product_sources_accepted"
        ),
    )
