"""Deterministic research of one HTML product page."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from product import (
    ClaimEvidence,
    Product,
    ProductFeature,
    ProductSource,
    ProductSpec,
    ProductVariant,
)


CONFIDENCE_BY_SOURCE = {
    "json_ld_spec": 0.95,
    "json_ld_feature": 0.90,
    "structured_html_spec": 0.85,
    "feature_section": 0.75,
}

REQUEST_HEADERS = {
    "User-Agent": (
        "VideoGeneratorProductResearch/1.0 "
        "(+https://github.com/P0RTNOY/VideoGenerator)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


class ProductResearchError(RuntimeError):
    """Base error for deterministic product research."""


class InvalidProductUrlError(ProductResearchError):
    """Raised when a supplied product URL is not fetchable HTTP(S)."""


class ProductFetchError(ProductResearchError):
    """Raised when a product page cannot be fetched safely."""


class ProductExtractionError(ProductResearchError):
    """Raised when the page cannot support a valid Product identity."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    html: str


@dataclass(frozen=True)
class PageProductSelection:
    entity: Dict[str, Any]
    variant: Optional[Dict[str, Any]] = None


def validate_product_url(url: str) -> str:
    """Return a clean HTTP(S) URL or raise a clear validation error."""

    cleaned = _clean_text(url)
    if not cleaned:
        raise InvalidProductUrlError("Product URL is required.")
    parts = urlsplit(cleaned)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise InvalidProductUrlError(
            "Product URL must be an absolute http:// or https:// URL."
        )
    if parts.username is not None or parts.password is not None:
        raise InvalidProductUrlError("Product URL must not contain credentials.")
    return cleaned


def fetch_product_page(
    url: str,
    timeout: float = 15,
    session: Any = None,
) -> FetchedPage:
    """Fetch one product page and require a successful HTML response."""

    validated_url = validate_product_url(url)
    client = session or requests
    response = None
    try:
        response = client.get(
            validated_url,
            headers=REQUEST_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.Timeout as error:
        raise ProductFetchError(
            f"Product page request timed out after {timeout} seconds."
        ) from error
    except requests.ConnectionError as error:
        raise ProductFetchError("Could not connect to the product page.") from error
    except requests.HTTPError as error:
        error_response = getattr(error, "response", None)
        status_source = error_response if error_response is not None else response
        status_code = getattr(status_source, "status_code", "unknown")
        raise ProductFetchError(
            f"Product page returned HTTP status {status_code}."
        ) from error
    except requests.RequestException as error:
        raise ProductFetchError(f"Product page request failed: {error}") from error

    content_type = str(response.headers.get("Content-Type", ""))
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type not in {"text/html", "application/xhtml+xml"}:
        displayed_type = content_type or "missing Content-Type"
        raise ProductFetchError(
            f"Product page did not return HTML ({displayed_type})."
        )

    final_url = _clean_text(getattr(response, "url", None)) or validated_url
    return FetchedPage(url=final_url, html=response.text)


def research_product(
    url: str,
    timeout: float = 15,
    session: Any = None,
) -> Product:
    """Fetch and deterministically extract one product page."""

    page = fetch_product_page(url, timeout=timeout, session=session)
    return research_product_from_html(page.html, page.url)


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _schema_type_matches(value: Any, expected: str) -> bool:
    for schema_type in _as_list(value):
        if not isinstance(schema_type, str):
            continue
        normalized = schema_type.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if normalized.casefold() == expected.casefold():
            return True
    return False


def _walk_json(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_json(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_json(nested)


def _load_jsonld_documents(soup: BeautifulSoup) -> List[Any]:
    documents: List[Any] = []
    for script in soup.find_all("script"):
        script_type = str(script.get("type", "")).casefold()
        if "ld+json" not in script_type:
            continue
        raw_json = script.string if script.string is not None else script.get_text()
        if not raw_json or not raw_json.strip():
            continue
        try:
            documents.append(json.loads(raw_json))
        except (json.JSONDecodeError, TypeError):
            continue
    return documents


def _normalize_url_for_match(url: str, base_url: str) -> Optional[str]:
    cleaned = _clean_text(url)
    if not cleaned:
        return None
    absolute = urljoin(base_url, cleaned)
    parts = urlsplit(absolute)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            path,
            parts.query,
            "",
        )
    )


def _urls_from_value(value: Any, base_url: str) -> Set[str]:
    urls: Set[str] = set()
    for item in _as_list(value):
        candidates: Sequence[Any]
        if isinstance(item, dict):
            candidates = (item.get("url"), item.get("@id"))
        else:
            candidates = (item,)
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            normalized = _normalize_url_for_match(candidate, base_url)
            if normalized:
                urls.add(normalized)
    return urls


def _canonical_url(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    canonical = soup.find(
        "link",
        rel=lambda value: value
        and "canonical" in {str(part).casefold() for part in _as_list(value)},
    )
    if not isinstance(canonical, Tag):
        return None
    href = canonical.get("href")
    if not isinstance(href, str):
        return None
    return _normalize_url_for_match(href, page_url)


def _product_completeness(product_data: Dict[str, Any]) -> int:
    useful_fields = (
        "name",
        "brand",
        "description",
        "category",
        "sku",
        "model",
        "mpn",
        "color",
        "size",
        "additionalProperty",
        "featureList",
    )
    return sum(product_data.get(field) not in (None, "", [], {}) for field in useful_fields)


def _jsonld_reference_ids(value: Any, base_url: str) -> Set[str]:
    reference_ids: Set[str] = set()
    for item in _as_list(value):
        candidates: Sequence[Any]
        if isinstance(item, dict):
            candidates = (item.get("@id"), item.get("url"))
        else:
            candidates = (item,)
        for candidate in candidates:
            cleaned = _clean_text(candidate)
            if cleaned:
                reference_ids.add(urljoin(base_url, cleaned))
    return reference_ids


def _product_group_variants(
    group: Dict[str, Any],
    products: Sequence[Dict[str, Any]],
    page_url: str,
) -> List[Dict[str, Any]]:
    products_by_reference: Dict[str, List[Dict[str, Any]]] = {}
    for product in products:
        for reference in _jsonld_reference_ids(product, page_url):
            products_by_reference.setdefault(reference, []).append(product)

    variants: List[Dict[str, Any]] = []
    seen_object_ids: Set[int] = set()

    def add_variant(candidate: Dict[str, Any]) -> None:
        object_id = id(candidate)
        if object_id in seen_object_ids:
            return
        seen_object_ids.add(object_id)
        variants.append(candidate)

    for item in _as_list(group.get("hasVariant")):
        if isinstance(item, dict) and _schema_type_matches(item.get("@type"), "Product"):
            add_variant(item)
        for reference in _jsonld_reference_ids(item, page_url):
            for product in products_by_reference.get(reference, []):
                add_variant(product)
    return variants


def _page_identity_score(
    candidate: Dict[str, Any],
    page_url: str,
    page_urls: Set[str],
    main_entity_refs: Set[str],
    embedded_main_entity_ids: Set[int],
    match_id_to_page: bool = True,
) -> int:
    identity_score = 0
    candidate_urls = set(_urls_from_value(candidate.get("url"), page_url))
    if match_id_to_page:
        candidate_urls.update(_urls_from_value(candidate.get("@id"), page_url))
    if candidate_urls & page_urls:
        identity_score += 4

    main_entity_page_urls = _urls_from_value(
        candidate.get("mainEntityOfPage"),
        page_url,
    )
    if main_entity_page_urls & page_urls:
        identity_score += 4
    if _jsonld_reference_ids(candidate, page_url) & main_entity_refs:
        identity_score += 3
    if id(candidate) in embedded_main_entity_ids:
        identity_score += 3
    return identity_score


def _select_group_variant(
    variants: Sequence[Dict[str, Any]],
    page_url: str,
    page_urls: Set[str],
    main_entity_refs: Set[str],
    embedded_main_entity_ids: Set[int],
) -> Optional[Dict[str, Any]]:
    if len(variants) == 1:
        return variants[0]
    if len(variants) < 2:
        return None

    identified_variants = [
        variant
        for variant in variants
        if _page_identity_score(
            variant,
            page_url,
            page_urls,
            main_entity_refs,
            embedded_main_entity_ids,
            match_id_to_page=False,
        )
        > 0
    ]
    return identified_variants[0] if len(identified_variants) == 1 else None


def _select_page_product(
    documents: Sequence[Any],
    soup: BeautifulSoup,
    page_url: str,
) -> Optional[PageProductSelection]:
    products: List[Dict[str, Any]] = []
    product_groups: List[Dict[str, Any]] = []
    main_entity_refs: Set[str] = set()
    embedded_main_entity_ids: Set[int] = set()

    for document in documents:
        for node in _walk_json(document):
            if _schema_type_matches(node.get("@type"), "Product"):
                products.append(node)
            if _schema_type_matches(node.get("@type"), "ProductGroup"):
                product_groups.append(node)
            for main_entity in _as_list(node.get("mainEntity")):
                main_entity_refs.update(
                    _jsonld_reference_ids(main_entity, page_url)
                )
                if isinstance(main_entity, dict):
                    embedded_main_entity_ids.add(id(main_entity))

    if not products and not product_groups:
        return None

    page_urls = {
        normalized
        for normalized in (
            _normalize_url_for_match(page_url, page_url),
            _canonical_url(soup, page_url),
        )
        if normalized
    }

    variants_by_group_id = {
        id(group): _product_group_variants(group, products, page_url)
        for group in product_groups
    }
    linked_variant_ids = {
        id(variant)
        for variants in variants_by_group_id.values()
        for variant in variants
    }
    candidates = [
        *product_groups,
        *(product for product in products if id(product) not in linked_variant_ids),
    ]

    def score(candidate: Dict[str, Any]) -> Tuple[int, int]:
        identity_score = _page_identity_score(
            candidate,
            page_url,
            page_urls,
            main_entity_refs,
            embedded_main_entity_ids,
        )
        if _schema_type_matches(candidate.get("@type"), "ProductGroup"):
            variant_scores = [
                _page_identity_score(
                    variant,
                    page_url,
                    page_urls,
                    main_entity_refs,
                    embedded_main_entity_ids,
                    match_id_to_page=False,
                )
                for variant in variants_by_group_id[id(candidate)]
            ]
            if variant_scores:
                identity_score = max(identity_score, max(variant_scores))
        return identity_score, _product_completeness(candidate)

    selected_entity = max(candidates, key=score)
    selected_variant = None
    if _schema_type_matches(selected_entity.get("@type"), "ProductGroup"):
        selected_variant = _select_group_variant(
            variants_by_group_id[id(selected_entity)],
            page_url,
            page_urls,
            main_entity_refs,
            embedded_main_entity_ids,
        )
    return PageProductSelection(entity=selected_entity, variant=selected_variant)


def _text_from_structured_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("name", "value", "@value"):
            text = _clean_text(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, list):
        texts = [_text_from_structured_value(item) for item in value]
        cleaned = [text for text in texts if text]
        return cleaned[-1] if cleaned else None
    return _clean_text(value)


def _meta_content(soup: BeautifulSoup, keys: Sequence[str]) -> Optional[str]:
    wanted = {key.casefold() for key in keys}
    for meta in soup.find_all("meta"):
        identifiers = {
            str(meta.get(attribute, "")).casefold()
            for attribute in ("property", "name", "itemprop")
        }
        if not identifiers & wanted:
            continue
        content = meta.get("content")
        text = _clean_text(content)
        if text:
            return text
    return None


def _breadcrumb_entries_from_jsonld(
    documents: Sequence[Any],
    page_url: str,
) -> List[Tuple[str, Optional[str]]]:
    for document in documents:
        for node in _walk_json(document):
            if not _schema_type_matches(node.get("@type"), "BreadcrumbList"):
                continue
            raw_items = [
                item
                for item in _as_list(node.get("itemListElement"))
                if isinstance(item, dict)
            ]

            def position_key(indexed_item: Tuple[int, Dict[str, Any]]) -> Tuple[int, float, int]:
                index, item = indexed_item
                try:
                    position = float(item.get("position"))
                except (TypeError, ValueError):
                    return 1, float(index), index
                return 0, position, index

            indexed_items = sorted(enumerate(raw_items), key=position_key)
            entries: List[Tuple[str, Optional[str]]] = []
            for _, item in indexed_items:
                name = _clean_text(item.get("name"))
                nested_item = item.get("item")
                if not name and isinstance(nested_item, dict):
                    name = _clean_text(nested_item.get("name"))
                if not name:
                    continue
                entries.append((name, _first_url_from_value(nested_item, page_url)))
            if entries:
                return entries
    return []


def _breadcrumb_entries_from_html(
    soup: BeautifulSoup,
    page_url: str,
) -> List[Tuple[str, Optional[str]]]:
    containers: List[Tag] = []
    for candidate in soup.find_all(["nav", "ol", "ul", "div"]):
        label = str(candidate.get("aria-label", "")).casefold()
        identifiers = " ".join(
            [
                str(candidate.get("id", "")),
                " ".join(str(value) for value in _as_list(candidate.get("class"))),
            ]
        ).casefold()
        if "breadcrumb" in label or "breadcrumb" in identifiers:
            containers.append(candidate)

    for container in containers:
        item_tags = container.find_all("li")
        if not item_tags:
            item_tags = container.find_all(["a", "span"], recursive=True)
        entries: List[Tuple[str, Optional[str]]] = []
        for item in item_tags:
            name = _clean_text(item.get_text(" ", strip=True))
            if not name:
                continue
            link = item if item.name == "a" else item.find("a")
            href = link.get("href") if isinstance(link, Tag) else None
            normalized_url = (
                _normalize_url_for_match(href, page_url)
                if isinstance(href, str)
                else None
            )
            entries.append((name, normalized_url))
        if entries:
            return entries
    return []


def _category_from_breadcrumbs(
    entries: Sequence[Tuple[str, Optional[str]]],
    product_name: Optional[str],
    page_urls: Set[str],
) -> Optional[str]:
    if not entries:
        return None
    remaining = list(entries)
    last_name, last_url = remaining[-1]
    if (
        product_name
        and last_name.casefold() == product_name.casefold()
        or last_url in page_urls
    ):
        remaining.pop()
    for name, _ in reversed(remaining):
        if name.casefold() not in {"home", "homepage"}:
            return name
    return None


def generate_product_id(
    brand: str,
    name: str,
    variant: Optional[ProductVariant] = None,
) -> str:
    """Generate a deterministic product slug without mutating Product."""

    parts = [brand, name]
    if variant:
        parts.extend(value for value in (variant.color, variant.size) if value)
    normalized = unicodedata.normalize("NFKD", " ".join(parts))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-")
    if not slug:
        raise ProductExtractionError("Could not generate a product ID from page identity.")
    return slug


def _first_url_from_value(value: Any, base_url: str) -> Optional[str]:
    for item in _as_list(value):
        candidates: Sequence[Any]
        if isinstance(item, dict):
            candidates = (item.get("url"), item.get("@id"))
        else:
            candidates = (item,)
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            normalized = _normalize_url_for_match(candidate, base_url)
            if normalized:
                return normalized
    return None


def _normalized_key(label: str) -> Optional[str]:
    normalized = unicodedata.normalize("NFKD", label)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    key = re.sub(r"[^a-z0-9]+", "_", ascii_text.casefold()).strip("_")
    return key or None


def _numeric_value(text: str) -> Optional[object]:
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", text):
        value = float(text)
        return value if math.isfinite(value) else None
    return None


def _normalized_spec_value(
    label: str,
    raw_value: Any,
    explicit_unit: Any = None,
) -> Optional[Tuple[object, Optional[str]]]:
    unit = _clean_text(explicit_unit)
    if isinstance(raw_value, bool):
        return raw_value, unit
    if isinstance(raw_value, int):
        return raw_value, unit
    if isinstance(raw_value, float):
        return (raw_value, unit) if math.isfinite(raw_value) else None
    text = _clean_text(raw_value)
    if not text:
        return None

    lowered = text.casefold()
    if lowered in {"yes", "true"}:
        return True, unit
    if lowered in {"no", "false"}:
        return False, unit

    if "version" not in label.casefold():
        if unit:
            numeric = _numeric_value(text)
            if numeric is not None:
                return numeric, unit
        else:
            number_and_unit = re.fullmatch(
                r"([-+]?(?:\d+\.\d*|\d*\.\d+|\d+))\s+"
                r"([A-Za-zµμ°%][A-Za-z0-9µμ°%/ .-]{0,20})",
                text,
            )
            if number_and_unit:
                numeric = _numeric_value(number_and_unit.group(1))
                parsed_unit = _clean_text(number_and_unit.group(2))
                if numeric is not None and parsed_unit:
                    return numeric, parsed_unit
            numeric = _numeric_value(text)
            if numeric is not None:
                return numeric, None

    return text, unit


def _build_spec(
    label: Any,
    raw_value: Any,
    source_url: str,
    confidence: float,
    unit: Any = None,
    evidence: Optional[ClaimEvidence] = None,
) -> Optional[ProductSpec]:
    clean_label = _clean_text(label)
    if not clean_label:
        return None
    key = _normalized_key(clean_label)
    normalized = _normalized_spec_value(clean_label, raw_value, unit)
    if not key or not normalized:
        return None
    value, normalized_unit = normalized
    return ProductSpec(
        key=key,
        label=clean_label,
        value=value,
        unit=normalized_unit,
        source_url=source_url,
        confidence=confidence,
        verified=False,
        evidence=evidence,
    )


def _evidence_value_text(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text:
        return text
    if value is None:
        return None
    try:
        return _clean_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return _clean_text(str(value))


def _jsonld_property_evidence_text(item: Dict[str, Any]) -> Optional[str]:
    label = _clean_text(item.get("name") or item.get("propertyID"))
    value = _evidence_value_text(item.get("value"))
    unit = _clean_text(item.get("unitText") or item.get("unitCode"))
    if not label or not value:
        return None
    if unit and not value.casefold().endswith(unit.casefold()):
        value = f"{value} {unit}"
    return f"{label}: {value}"


def _extract_jsonld_specs(
    product_data: Dict[str, Any],
    source_url: str,
) -> List[ProductSpec]:
    specs: List[ProductSpec] = []
    for item in _as_list(product_data.get("additionalProperty")):
        if not isinstance(item, dict):
            continue
        raw_text = _jsonld_property_evidence_text(item)
        spec = _build_spec(
            item.get("name") or item.get("propertyID"),
            item.get("value"),
            source_url,
            CONFIDENCE_BY_SOURCE["json_ld_spec"],
            item.get("unitText") or item.get("unitCode"),
            ClaimEvidence(
                raw_text=raw_text,
                source_url=source_url,
                extraction_method="jsonld_additional_property",
            )
            if raw_text
            else None,
        )
        if spec:
            specs.append(spec)
    return specs


def _tag_identifiers(tag: Tag) -> str:
    return " ".join(
        [
            str(tag.get("id", "")),
            " ".join(str(value) for value in _as_list(tag.get("class"))),
            str(tag.get("aria-label", "")),
        ]
    ).casefold()


def _identifier_values(tag: Tag) -> List[str]:
    raw_values = [tag.get("id"), tag.get("aria-label"), *_as_list(tag.get("class"))]
    normalized: List[str] = []
    for value in raw_values:
        text = _clean_text(value)
        if not text:
            continue
        normalized.append(re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-"))
    return normalized


def _has_spec_context(tag: Tag) -> bool:
    spec_tokens = {"spec", "specs", "specification", "specifications"}
    recognized_headings = {
        "specifications",
        "product specifications",
        "technical details",
        "technical specifications",
    }
    current: Optional[Tag] = tag
    for _ in range(7):
        if current is None:
            break
        tokens = {
            token
            for identifier in _identifier_values(current)
            for token in identifier.split("-")
        }
        if tokens & spec_tokens or (
            "technical" in tokens and tokens & {"detail", "details"}
        ):
            return True
        heading = current.find(["h1", "h2", "h3", "h4"], recursive=False)
        if isinstance(heading, Tag):
            heading_text = _clean_text(heading.get_text(" ", strip=True))
            if heading_text and heading_text.casefold() in recognized_headings:
                return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _section_heading(tag: Tag) -> Optional[str]:
    current: Optional[Tag] = tag
    for _ in range(7):
        if current is None:
            break
        heading = current.find(["h1", "h2", "h3", "h4"], recursive=False)
        if isinstance(heading, Tag):
            heading_text = _clean_text(heading.get_text(" ", strip=True))
            if heading_text:
                return heading_text
        current = current.parent if isinstance(current.parent, Tag) else None
    return None


def _extract_table_specs(soup: BeautifulSoup, source_url: str) -> List[ProductSpec]:
    specs: List[ProductSpec] = []
    confidence = CONFIDENCE_BY_SOURCE["structured_html_spec"]
    for row in soup.find_all("tr"):
        if not _has_spec_context(row):
            continue
        headers = row.find_all("th", recursive=False)
        cells = row.find_all("td", recursive=False)
        if len(headers) == 1 and len(cells) == 1:
            label_tag, value_tag = headers[0], cells[0]
        elif not headers and len(cells) == 2:
            label_tag, value_tag = cells
        else:
            continue
        spec = _build_spec(
            label_tag.get_text(" ", strip=True),
            value_tag.get_text(" ", strip=True),
            source_url,
            confidence,
            evidence=ClaimEvidence(
                raw_text=row.get_text(" ", strip=True),
                source_url=source_url,
                extraction_method="html_spec_table",
                section_heading=_section_heading(row),
            ),
        )
        if spec:
            specs.append(spec)
    return specs


def _extract_definition_specs(
    soup: BeautifulSoup,
    source_url: str,
) -> List[ProductSpec]:
    specs: List[ProductSpec] = []
    confidence = CONFIDENCE_BY_SOURCE["structured_html_spec"]
    for definition_list in soup.find_all("dl"):
        if not _has_spec_context(definition_list):
            continue
        for term in definition_list.find_all("dt"):
            definition = term.find_next_sibling("dd")
            if not isinstance(definition, Tag):
                continue
            spec = _build_spec(
                term.get_text(" ", strip=True),
                definition.get_text(" ", strip=True),
                source_url,
                confidence,
                evidence=ClaimEvidence(
                    raw_text=f"{term.get_text(' ', strip=True)} "
                    f"{definition.get_text(' ', strip=True)}",
                    source_url=source_url,
                    extraction_method="html_definition_list",
                    section_heading=_section_heading(definition_list),
                ),
            )
            if spec:
                specs.append(spec)
    return specs


def _deduplicate_specs(specs: Sequence[ProductSpec]) -> List[ProductSpec]:
    deduplicated: List[ProductSpec] = []
    indices: Dict[str, int] = {}
    for spec in specs:
        existing_index = indices.get(spec.key)
        if existing_index is None:
            indices[spec.key] = len(deduplicated)
            deduplicated.append(spec)
            continue
        existing = deduplicated[existing_index]
        if (spec.confidence or 0) > (existing.confidence or 0):
            deduplicated[existing_index] = spec
    return deduplicated


def _feature_values(
    value: Any,
) -> Iterable[Tuple[str, Optional[str], str]]:
    for item in _as_list(value):
        if isinstance(item, dict):
            name = _clean_text(item.get("name") or item.get("headline"))
            description = _clean_text(item.get("description"))
            if name:
                raw_text = f"{name}: {description}" if description else name
                yield name, description, raw_text
            continue
        text = _clean_text(item)
        if not text:
            continue
        parts = [
            _clean_text(part)
            for part in re.split(r"[;\n•]+", text)
        ]
        for part in parts:
            if part:
                yield part, None, part


def _build_feature(
    name: Any,
    description: Any,
    source_url: str,
    confidence: float,
    evidence: Optional[ClaimEvidence] = None,
) -> Optional[ProductFeature]:
    clean_name = _clean_text(name)
    if not clean_name:
        return None
    feature_id = generate_product_id("", clean_name)
    return ProductFeature(
        id=feature_id,
        name=clean_name,
        description=_clean_text(description),
        source_url=source_url,
        confidence=confidence,
        verified=False,
        evidence=evidence,
    )


def _extract_jsonld_features(
    product_data: Dict[str, Any],
    source_url: str,
) -> List[ProductFeature]:
    features: List[ProductFeature] = []
    confidence = CONFIDENCE_BY_SOURCE["json_ld_feature"]
    for name, description, raw_text in _feature_values(
        product_data.get("featureList")
    ):
        feature = _build_feature(
            name,
            description,
            source_url,
            confidence,
            ClaimEvidence(
                raw_text=raw_text,
                source_url=source_url,
                extraction_method="jsonld_feature_list",
            ),
        )
        if feature:
            features.append(feature)
    return features


def _is_feature_container(tag: Tag) -> bool:
    recognized_identifiers = {
        "feature",
        "features",
        "feature-list",
        "features-list",
        "key-features",
        "product-features",
        "highlight",
        "highlights",
        "product-highlights",
    }
    if set(_identifier_values(tag)) & recognized_identifiers:
        return True
    heading = tag.find(["h1", "h2", "h3", "h4"], recursive=False)
    if not isinstance(heading, Tag):
        return False
    heading_text = _clean_text(heading.get_text(" ", strip=True))
    if not heading_text:
        return False
    return heading_text.casefold() in {
        "features",
        "key features",
        "highlights",
        "product highlights",
    }


def _extract_html_features(
    soup: BeautifulSoup,
    source_url: str,
) -> List[ProductFeature]:
    features: List[ProductFeature] = []
    confidence = CONFIDENCE_BY_SOURCE["feature_section"]
    for container in soup.find_all(["section", "article", "div"]):
        if not _is_feature_container(container):
            continue
        for item in container.find_all("li"):
            full_text = _clean_text(item.get_text(" ", strip=True))
            if not full_text:
                continue
            heading = item.find(["strong", "b", "h3", "h4"])
            name = (
                _clean_text(heading.get_text(" ", strip=True))
                if isinstance(heading, Tag)
                else full_text
            )
            if not name:
                continue
            description = full_text[len(name) :].lstrip(" :-–—") or None
            feature = _build_feature(
                name,
                description,
                source_url,
                confidence,
                ClaimEvidence(
                    raw_text=full_text,
                    source_url=source_url,
                    extraction_method="explicit_feature_section",
                    section_heading=_section_heading(container),
                ),
            )
            if feature:
                features.append(feature)
    return features


def _deduplicate_features(
    features: Sequence[ProductFeature],
) -> List[ProductFeature]:
    deduplicated: List[ProductFeature] = []
    indices: Dict[str, int] = {}
    for feature in features:
        key = _normalized_key(feature.name) or feature.name.casefold()
        existing_index = indices.get(key)
        if existing_index is None:
            indices[key] = len(deduplicated)
            deduplicated.append(feature)
            continue
        existing = deduplicated[existing_index]
        if (feature.confidence or 0) > (existing.confidence or 0):
            deduplicated[existing_index] = feature
    return deduplicated


def _deduplicate_sources(sources: Sequence[ProductSource]) -> List[ProductSource]:
    deduplicated: List[ProductSource] = []
    seen_urls: Set[str] = set()
    for source in sources:
        normalized = _normalize_url_for_match(source.url, source.url) or source.url
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        deduplicated.append(source)
    return deduplicated


def research_product_from_html(html: str, source_url: str) -> Product:
    """Extract a validated Product from one already-fetched HTML page."""

    soup = BeautifulSoup(html, "html.parser")
    documents = _load_jsonld_documents(soup)
    selection = _select_page_product(documents, soup, source_url)
    page_entity_data = selection.entity if selection else {}
    is_product_group = _schema_type_matches(
        page_entity_data.get("@type"),
        "ProductGroup",
    )
    selected_variant_data = (
        selection.variant or {}
        if selection and is_product_group
        else page_entity_data
    )
    identity_fallback_data = selected_variant_data if is_product_group else {}

    name = (
        _text_from_structured_value(page_entity_data.get("name"))
        or _text_from_structured_value(identity_fallback_data.get("name"))
        or _meta_content(
            soup,
            ("product:name", "og:title", "twitter:title"),
        )
    )
    brand = (
        _text_from_structured_value(page_entity_data.get("brand"))
        or _text_from_structured_value(identity_fallback_data.get("brand"))
        or _meta_content(
            soup,
            ("product:brand", "og:brand", "brand"),
        )
    )
    description = (
        _text_from_structured_value(page_entity_data.get("description"))
        or _text_from_structured_value(identity_fallback_data.get("description"))
        or _meta_content(
            soup,
            ("og:description", "twitter:description", "description"),
        )
    )
    category = _text_from_structured_value(
        page_entity_data.get("category")
    ) or _meta_content(
        soup,
        ("product:category", "og:category", "category"),
    )

    page_urls = {
        normalized
        for normalized in (
            _normalize_url_for_match(source_url, source_url),
            _canonical_url(soup, source_url),
        )
        if normalized
    }
    if not category:
        category = _category_from_breadcrumbs(
            _breadcrumb_entries_from_jsonld(documents, source_url),
            name,
            page_urls,
        )
    if not category:
        category = _category_from_breadcrumbs(
            _breadcrumb_entries_from_html(soup, source_url),
            name,
            page_urls,
        )

    missing = [
        field_name
        for field_name, value in (
            ("name", name),
            ("brand", brand),
            ("category", category),
        )
        if not value
    ]
    if missing:
        raise ProductExtractionError(
            "Page does not provide required product identity fields: "
            + ", ".join(missing)
        )

    model_number = _text_from_structured_value(selected_variant_data.get("model"))
    if not model_number:
        model_number = _text_from_structured_value(selected_variant_data.get("mpn"))
    variant_values = {
        "color": _text_from_structured_value(selected_variant_data.get("color")),
        "size": _text_from_structured_value(selected_variant_data.get("size")),
        "model_number": model_number,
        "sku": _text_from_structured_value(selected_variant_data.get("sku")),
    }
    variant = (
        ProductVariant(**variant_values)
        if any(variant_values.values())
        else None
    )

    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else None
    domain = urlsplit(source_url).hostname
    source = ProductSource(
        url=source_url,
        source_type="product_page",
        domain=domain,
        title=title,
    )

    jsonld_entities = [page_entity_data]
    if selected_variant_data and selected_variant_data is not page_entity_data:
        jsonld_entities.append(selected_variant_data)
    specs = _deduplicate_specs(
        [
            *(
                spec
                for entity in jsonld_entities
                for spec in _extract_jsonld_specs(entity, source_url)
            ),
            *_extract_table_specs(soup, source_url),
            *_extract_definition_specs(soup, source_url),
        ]
    )
    features = _deduplicate_features(
        [
            *(
                feature
                for entity in jsonld_entities
                for feature in _extract_jsonld_features(entity, source_url)
            ),
            *_extract_html_features(soup, source_url),
        ]
    )

    return Product(
        id=generate_product_id(brand, name, variant),
        name=name,
        brand=brand,
        category=category,
        description=description,
        variant=variant,
        specs=specs,
        features=features,
        sources=_deduplicate_sources([source]),
    )
