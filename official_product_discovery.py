"""Bounded discovery of one official product page from search-provider hints."""

from __future__ import annotations

import re
import unicodedata
from itertools import islice
from typing import Iterable, List, Literal, Optional, Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from product import NonEmptyStr, Product, ProductModel, ProductVariant
from product_identity import normalize_identity_url
from product_research import ProductResearchError, research_product
from product_source_research import reconcile_products


MAX_SEARCH_QUERIES = 3
MAX_RAW_RESULTS_PER_QUERY = 10
MAX_FETCHED_CANDIDATES = 5

DiscoveryStatus = Literal["found", "not_found", "ambiguous"]
CandidateValidationStatus = Literal[
    "accepted",
    "rejected_url",
    "unofficial_domain",
    "research_failed",
    "identity_mismatch",
    "duplicate_final_url",
    "not_fetched",
]


class OfficialProductPageSearchError(RuntimeError):
    """Raised when the configured search provider fails."""


class OfficialProductPageRequest(ProductModel):
    """Minimum product identity needed to search for an official page."""

    brand: NonEmptyStr
    product_name: NonEmptyStr
    model_number: Optional[NonEmptyStr] = None
    known_official_domain: Optional[NonEmptyStr] = None

    @field_validator("known_official_domain")
    @classmethod
    def require_bare_domain(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if any(character in value for character in "/:@?#") or re.search(
            r"\s", value
        ):
            raise ValueError("known_official_domain must be a bare domain")
        normalized = _normalized_domain(value)
        if normalized is None or "." not in normalized:
            raise ValueError("known_official_domain must be a valid domain")
        return normalized


class ProductPageSearchResult(ProductModel):
    """One provider navigation hint; title and snippet are never product facts."""

    url: NonEmptyStr
    title: Optional[NonEmptyStr] = None
    snippet: Optional[NonEmptyStr] = None


class ProductPageSearchProvider(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int,
    ) -> Iterable[ProductPageSearchResult]:
        """Return at most ``limit`` navigation hints for one query."""


class OfficialProductPageCandidate(ProductModel):
    """Discovery and deterministic validation outcome for one unique URL."""

    discovered_url: NonEmptyStr
    query: NonEmptyStr
    search_rank: int = Field(gt=0, strict=True)
    domain: Optional[NonEmptyStr] = None
    validation_status: CandidateValidationStatus
    reason: NonEmptyStr
    final_url: Optional[NonEmptyStr] = None
    identity_match_reason: Optional[NonEmptyStr] = None


class OfficialProductPageDiscovery(ProductModel):
    """Result of bounded official-page discovery and page-level validation."""

    schema_version: Literal[1] = 1
    request: OfficialProductPageRequest
    status: DiscoveryStatus
    accepted_url: Optional[NonEmptyStr] = None
    discovered_url: Optional[NonEmptyStr] = None
    product: Optional[Product] = None
    candidates: List[OfficialProductPageCandidate] = Field(default_factory=list)
    queries: List[NonEmptyStr] = Field(default_factory=list, max_length=MAX_SEARCH_QUERIES)
    reason: NonEmptyStr

    @model_validator(mode="after")
    def require_found_payload_only_for_found(self) -> "OfficialProductPageDiscovery":
        accepted = (
            self.accepted_url is not None
            and self.discovered_url is not None
            and self.product is not None
        )
        if self.status == "found" and not accepted:
            raise ValueError("found discovery requires accepted page payload")
        if self.status != "found" and (
            self.accepted_url is not None
            or self.discovered_url is not None
            or self.product is not None
        ):
            raise ValueError("only found discovery may contain accepted page payload")
        return self


def _ascii_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_text.casefold())


def _host(url: str) -> Optional[str]:
    parts = urlsplit(url)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return None
    return parts.hostname.casefold().rstrip(".")


def _registrable_label(host: str) -> Optional[str]:
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return None
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in {"ac", "co", "com", "edu", "gov", "net", "org"}
    ):
        return labels[-3]
    return labels[-2]


def _normalized_domain(value: str) -> Optional[str]:
    host = _host(value if "://" in value else f"https://{value}")
    if host is None:
        return None
    return host[4:] if host.startswith("www.") else host


def _is_plausibly_official(
    host: str,
    request: OfficialProductPageRequest,
) -> bool:
    normalized_host = host[4:] if host.startswith("www.") else host
    if request.known_official_domain:
        known = _normalized_domain(request.known_official_domain)
        return known is not None and (
            normalized_host == known or normalized_host.endswith(f".{known}")
        )
    label = _registrable_label(normalized_host)
    return label is not None and _ascii_identifier(label) == _ascii_identifier(
        request.brand
    )


def _queries(request: OfficialProductPageRequest) -> List[str]:
    base = f"{request.brand} {request.product_name}"
    if request.model_number and _ascii_identifier(
        request.model_number
    ) not in _ascii_identifier(base):
        base = f"{base} {request.model_number}"
    if request.known_official_domain:
        return [f"site:{request.known_official_domain} {base}"]
    return [base, f"{base} official"]


_SOCIAL_HOSTS = frozenset(
    {
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "pinterest.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "youtu.be",
        "youtube.com",
    }
)
_SEARCH_HOSTS = frozenset({"bing.com", "duckduckgo.com", "google.com"})
_NON_PRODUCT_PATH_SEGMENTS = frozenset(
    {
        "article",
        "articles",
        "blog",
        "blogs",
        "categories",
        "category",
        "collection",
        "collections",
        "help",
        "news",
        "review",
        "reviews",
        "search",
        "support",
    }
)


def _host_matches(host: str, domains: frozenset) -> bool:
    normalized = host[4:] if host.startswith("www.") else host
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in domains
    )


def _url_rejection_reason(url: str) -> Optional[str]:
    host = _host(url)
    if host is None:
        return "invalid_http_url"
    if _host_matches(host, _SOCIAL_HOSTS):
        return "social_media_url"
    if _host_matches(host, _SEARCH_HOSTS):
        return "search_result_url"
    path = urlsplit(url).path
    if path.casefold().endswith(".pdf"):
        return "pdf_url"
    segments = {
        segment.casefold()
        for segment in path.split("/")
        if segment
    }
    if segments & _NON_PRODUCT_PATH_SEGMENTS:
        return "clearly_non_product_path"
    return None


def _final_product_url(product: Product, fallback: str) -> str:
    for source in product.sources:
        if source.source_type == "product_page":
            return source.url
    return product.sources[0].url if product.sources else fallback


def _request_product(request: OfficialProductPageRequest) -> Product:
    variant = (
        ProductVariant(model_number=request.model_number)
        if request.model_number
        else None
    )
    return Product(
        id="official-page-discovery-request",
        name=request.product_name,
        brand=request.brand,
        category="Product",
        variant=variant,
    )


def discovery_request_from_product(
    product: Product,
    *,
    known_official_domain: Optional[str] = None,
) -> OfficialProductPageRequest:
    """Create an isolated discovery request from an existing Product identity."""

    return OfficialProductPageRequest(
        brand=product.brand,
        product_name=product.name,
        model_number=(
            product.variant.model_number
            if product.variant is not None
            else None
        ),
        known_official_domain=known_official_domain,
    )


def discover_official_product_page(
    request: OfficialProductPageRequest,
    provider: ProductPageSearchProvider,
    *,
    timeout: float = 15,
    session: object = None,
) -> OfficialProductPageDiscovery:
    """Discover and validate one official page without trusting search metadata."""

    queries = _queries(request)[:MAX_SEARCH_QUERIES]
    unique_results = []
    seen_urls = set()
    for query in queries:
        try:
            raw_results = provider.search(query, limit=MAX_RAW_RESULTS_PER_QUERY)
            bounded_results = list(
                islice(raw_results, MAX_RAW_RESULTS_PER_QUERY)
            )
        except Exception as error:
            raise OfficialProductPageSearchError(
                f"search provider failed for query {query!r}: {error}"
            ) from error
        for rank, raw_result in enumerate(bounded_results, start=1):
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
            unique_results.append((query, rank, result))

    candidates: List[OfficialProductPageCandidate] = []
    accepted = []
    accepted_final_urls = set()
    target = _request_product(request)
    fetched_candidates = 0
    for query, rank, result in unique_results:
        host = _host(result.url)
        url_rejection = _url_rejection_reason(result.url)
        if url_rejection is not None:
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="rejected_url",
                    reason=url_rejection,
                )
            )
            continue
        assert host is not None
        if not _is_plausibly_official(host, request):
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="unofficial_domain",
                    reason="domain_not_official",
                )
            )
            continue
        if fetched_candidates >= MAX_FETCHED_CANDIDATES:
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="not_fetched",
                    reason="candidate_fetch_limit",
                )
            )
            continue
        fetched_candidates += 1
        try:
            product = research_product(
                result.url,
                timeout=timeout,
                session=session,
            ).model_copy(deep=True)
        except ProductResearchError as error:
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="research_failed",
                    reason=str(error) or type(error).__name__,
                )
            )
            continue
        reconciliation = reconcile_products(target, product)
        final_url = _final_product_url(product, result.url)
        final_host = _host(final_url)
        if final_host is None:
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="rejected_url",
                    reason="invalid_final_http_url",
                    final_url=final_url,
                )
            )
            continue
        if not _is_plausibly_official(final_host, request):
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="unofficial_domain",
                    reason="final_domain_not_official",
                    final_url=final_url,
                )
            )
            continue
        if reconciliation.status != "matched":
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="identity_mismatch",
                    reason=reconciliation.reason,
                    final_url=final_url,
                )
            )
            continue
        try:
            normalized_final_url = normalize_identity_url(final_url)
        except ValueError:
            normalized_final_url = final_url
        if normalized_final_url in accepted_final_urls:
            candidates.append(
                OfficialProductPageCandidate(
                    discovered_url=result.url,
                    query=query,
                    search_rank=rank,
                    domain=host,
                    validation_status="duplicate_final_url",
                    reason="duplicate_final_product_page",
                    final_url=final_url,
                    identity_match_reason=reconciliation.reason,
                )
            )
            continue
        accepted_final_urls.add(normalized_final_url)
        candidate = OfficialProductPageCandidate(
            discovered_url=result.url,
            query=query,
            search_rank=rank,
            domain=host,
            validation_status="accepted",
            reason="official_domain_and_product_identity_match",
            final_url=final_url,
            identity_match_reason=reconciliation.reason,
        )
        candidates.append(candidate)
        strength = 2 if reconciliation.reason == "shared_model_number" else 1
        accepted.append((candidate, product, strength))

    if accepted:
        strongest = max(item[2] for item in accepted)
        accepted = [item for item in accepted if item[2] == strongest]

    if len(accepted) == 1:
        candidate, product, _ = accepted[0]
        return OfficialProductPageDiscovery(
            request=request.model_copy(deep=True),
            status="found",
            accepted_url=candidate.final_url,
            discovered_url=candidate.discovered_url,
            product=product.model_copy(deep=True),
            candidates=candidates,
            queries=queries,
            reason="one_strongest_official_product_page_accepted",
        )
    if len(accepted) > 1:
        return OfficialProductPageDiscovery(
            request=request.model_copy(deep=True),
            status="ambiguous",
            candidates=candidates,
            queries=queries,
            reason="multiple_official_product_pages_accepted",
        )
    return OfficialProductPageDiscovery(
        request=request.model_copy(deep=True),
        status="not_found",
        candidates=candidates,
        queries=queries,
        reason="no_official_product_page_accepted",
    )
