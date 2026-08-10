"""Deterministic research of one HTML product page."""

from __future__ import annotations

import hashlib
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
    ProductImage,
    ProductSource,
    ProductSpec,
    ProductVariant,
)
from product_identity import (
    canonicalize_product_name,
    generate_source_scoped_product_id,
    normalize_identity_url,
    slugify_identifier,
    stable_variant_identity,
)


CONFIDENCE_BY_SOURCE = {
    "json_ld_spec": 0.95,
    "json_ld_feature": 0.90,
    "structured_html_spec": 0.85,
    "feature_section": 0.75,
    "primary_product_claim_list": 0.65,
    "json_ld_product_image": 0.95,
    "json_ld_product_image_object": 0.90,
    "open_graph_product_image": 0.80,
    "html_product_gallery_image": 0.70,
}

MAX_PRODUCT_IMAGE_CANDIDATES = 24

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
class ResearchedProductPage:
    url: str
    html: str
    product: Product


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

    return research_product_page(url, timeout=timeout, session=session).product


def research_product_page(
    url: str,
    timeout: float = 15,
    session: Any = None,
) -> ResearchedProductPage:
    """Fetch once and expose both extracted Product and source HTML."""

    page = fetch_product_page(url, timeout=timeout, session=session)
    return ResearchedProductPage(
        url=page.url,
        html=page.html,
        product=research_product_from_html(page.html, page.url),
    )


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
    slug = slugify_identifier(" ".join(parts))
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


def _positive_image_dimension(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = _clean_text(value)
    if text and text.isdigit():
        dimension = int(text)
        return dimension if dimension > 0 else None
    return None


def build_product_image(
    image_url: Any,
    source_url: str,
    source_type: str,
    confidence: float,
    width: Any = None,
    height: Any = None,
) -> Optional[ProductImage]:
    """Build source-linked image metadata for one valid HTTP(S) asset."""

    clean_url = _clean_text(image_url)
    if not clean_url:
        return None
    absolute_url = urljoin(source_url, clean_url)
    parts = urlsplit(absolute_url)
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    absolute_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, "")
    )
    try:
        normalized_url = normalize_identity_url(absolute_url)
    except ValueError:
        return None
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return ProductImage(
        id=f"image-{digest}",
        url=absolute_url,
        source_url=source_url,
        source_type=source_type,
        width=_positive_image_dimension(width),
        height=_positive_image_dimension(height),
        confidence=confidence,
    )


def _structured_image_url(image_data: Dict[str, Any]) -> Optional[str]:
    return _clean_text(image_data.get("contentUrl")) or _clean_text(
        image_data.get("url")
    )


def _extract_jsonld_images(
    product_data: Dict[str, Any],
    source_url: str,
    image_objects_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[ProductImage]:
    images: List[ProductImage] = []
    for item in _as_list(product_data.get("image")):
        source_type = "json_ld_product_image"
        if isinstance(item, dict):
            image_data = item
            image_url = _structured_image_url(item)
            if not image_url:
                reference_id = _clean_text(item.get("@id"))
                resolved = (
                    image_objects_by_id.get(urljoin(source_url, reference_id))
                    if image_objects_by_id is not None and reference_id
                    else None
                )
                if resolved is not None:
                    image_data = resolved
                    image_url = _structured_image_url(resolved)
                    source_type = "json_ld_product_image_object"
            width = image_data.get("width")
            height = image_data.get("height")
        else:
            image_url = item
            width = None
            height = None
        image = build_product_image(
            image_url,
            source_url,
            source_type,
            CONFIDENCE_BY_SOURCE[source_type],
            width=width,
            height=height,
        )
        if image:
            images.append(image)
    return images


def _image_objects_by_id(
    documents: Sequence[Any],
    source_url: str,
) -> Dict[str, Dict[str, Any]]:
    """Index same-document ImageObjects without following graph relationships."""

    indexed: Dict[str, Dict[str, Any]] = {}
    for document in documents:
        for node in _walk_json(document):
            if not _schema_type_matches(node.get("@type"), "ImageObject"):
                continue
            reference_id = _clean_text(node.get("@id"))
            if reference_id and _structured_image_url(node):
                indexed.setdefault(urljoin(source_url, reference_id), node)
    return indexed


def _open_graph_image_entries(soup: BeautifulSoup) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for meta in soup.find_all("meta"):
        property_name = _clean_text(meta.get("property"))
        if not property_name:
            continue
        normalized_property = property_name.casefold()
        if normalized_property in {"og:image", "og:image:url", "og:image:secure_url"}:
            content = _clean_text(meta.get("content"))
            if content:
                current = {"url": content}
                entries.append(current)
            continue
        if current is None:
            continue
        key_by_property = {
            "og:image:alt": "alt",
            "og:image:width": "width",
            "og:image:height": "height",
        }
        entry_key = key_by_property.get(normalized_property)
        content = _clean_text(meta.get("content"))
        if entry_key and content:
            current[entry_key] = content
    return entries


def _extract_open_graph_images(
    soup: BeautifulSoup,
    source_url: str,
    source_name: str,
    product_name: str,
    brand: str,
) -> List[ProductImage]:
    accepted_names = {
        value.casefold()
        for value in (
            _clean_text(source_name),
            _clean_text(product_name),
            _clean_text(f"{brand} {product_name}"),
        )
        if value
    }
    images: List[ProductImage] = []
    for entry in _open_graph_image_entries(soup):
        alt = _clean_text(entry.get("alt"))
        if not alt or alt.casefold() not in accepted_names:
            continue
        image = build_product_image(
            entry.get("url"),
            source_url,
            "open_graph_product_image",
            CONFIDENCE_BY_SOURCE["open_graph_product_image"],
            width=entry.get("width"),
            height=entry.get("height"),
        )
        if image:
            images.append(image)
    return images


def _image_metadata_quality(image: ProductImage) -> Tuple[float, int, int]:
    dimension_count = int(image.width is not None) + int(image.height is not None)
    area = (
        image.width * image.height
        if image.width is not None and image.height is not None
        else 0
    )
    return image.confidence or 0, dimension_count, area


def _deduplicate_images(images: Sequence[ProductImage]) -> List[ProductImage]:
    deduplicated: List[ProductImage] = []
    indices: Dict[str, int] = {}
    for image in images:
        existing_index = indices.get(image.id)
        if existing_index is None:
            indices[image.id] = len(deduplicated)
            deduplicated.append(image)
            continue
        existing = deduplicated[existing_index]
        preferred = (
            image
            if _image_metadata_quality(image) > _image_metadata_quality(existing)
            else existing
        )
        alternate = existing if preferred is image else image
        deduplicated[existing_index] = preferred.model_copy(
            update={
                "width": preferred.width or alternate.width,
                "height": preferred.height or alternate.height,
            }
        )
    return deduplicated


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
    feature_id = slugify_identifier(clean_name)
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


_IDENTITY_STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "official",
    "the",
    "with",
}

_EXCLUDED_PRIMARY_CONTEXT = {
    "aside",
    "cart",
    "checkout",
    "comparison",
    "coupon",
    "delivery",
    "dialog",
    "faq",
    "footer",
    "modal",
    "navigation",
    "offer",
    "order",
    "overlay",
    "promotion",
    "purchase",
    "question",
    "rating",
    "recommendation",
    "related",
    "return",
    "review",
    "shipping",
    "setup",
    "testimonial",
    "warranty",
}

_EXCLUDED_PRIMARY_CONTEXT_PREFIXES = {
    "compar",
    "coupon",
    "deliver",
    "footer",
    "navigat",
    "offer",
    "order",
    "promot",
    "purchas",
    "question",
    "rating",
    "recommend",
    "return",
    "review",
    "ship",
    "testimon",
    "warrant",
}

_EXCLUDED_GALLERY_CONTEXT = _EXCLUDED_PRIMARY_CONTEXT | {
    "badge",
    "badges",
    "customer",
    "payment",
    "social",
    "swatch",
    "swatches",
    "trust",
    "variant",
    "variants",
}

_EXCLUDED_GALLERY_CONTEXT_PREFIXES = _EXCLUDED_PRIMARY_CONTEXT_PREFIXES | {
    "badge",
    "customer",
    "payment",
    "social",
    "swatch",
    "trust",
    "variant",
}

_GALLERY_CONTEXT_TOKENS = {
    "carousel",
    "gallery",
    "image",
    "images",
    "media",
    "photo",
    "photos",
    "slider",
    "slideshow",
    "thumbnail",
    "thumbnails",
    "visual",
}

_PRODUCT_DETAIL_CONTEXT_TOKENS = {
    "commerce",
    "detail",
    "details",
    "hero",
    "information",
    "overview",
    "product",
}

_IP_RATING_PATTERN = r"\bIP[0-6X][0-9X]\b"


def _meaningful_identity_tokens(value: str, brand: str) -> Set[str]:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    brand_normalized = unicodedata.normalize("NFKD", brand)
    brand_ascii = brand_normalized.encode("ascii", "ignore").decode("ascii")
    brand_tokens = set(re.findall(r"[a-z0-9]+", brand_ascii.casefold()))
    return {
        token
        for token in re.findall(r"[a-z0-9]+", ascii_text.casefold())
        if token not in brand_tokens and token not in _IDENTITY_STOPWORDS
    }


def _find_primary_product_h1(
    soup: BeautifulSoup,
    product_name: str,
    brand: str,
) -> Optional[Tag]:
    main = soup.find("main")
    if not isinstance(main, Tag):
        return None
    product_tokens = _meaningful_identity_tokens(product_name, brand)
    if not product_tokens:
        return None
    required_overlap = 1 if len(product_tokens) == 1 else 2
    matches: List[Tag] = []
    for heading in main.find_all("h1"):
        heading_text = _clean_text(heading.get_text(" ", strip=True))
        if not heading_text:
            continue
        heading_tokens = _meaningful_identity_tokens(heading_text, brand)
        overlap = product_tokens & heading_tokens
        if (
            len(overlap) >= required_overlap
            and len(overlap) / len(product_tokens) >= 0.5
        ):
            matches.append(heading)
    return matches[0] if len(matches) == 1 else None


def _direct_list_item_text(item: Tag, candidate_list: Tag) -> Optional[str]:
    parts = [
        str(text)
        for text in item.find_all(string=True)
        if text.find_parent(["ul", "ol"]) is candidate_list
    ]
    return _clean_text(" ".join(parts))


def _close_common_ancestor(first: Tag, second: Tag) -> bool:
    first_parents = list(first.parents)
    second_parents = list(second.parents)
    for first_distance, ancestor in enumerate(first_parents, start=1):
        if ancestor not in second_parents:
            continue
        if not isinstance(ancestor, Tag) or ancestor.name in {"main", "body", "html"}:
            return False
        second_distance = second_parents.index(ancestor) + 1
        return first_distance + second_distance <= 8
    return False


def _has_excluded_primary_context(tag: Tag, stop_at: Tag) -> bool:
    current: Optional[Tag] = tag
    while current is not None and current is not stop_at:
        if current.name in {"header", "footer", "nav", "aside", "dialog"}:
            return True
        identifiers = _identifier_values(current)
        identifier_tokens = {
            token
            for identifier in identifiers
            for token in identifier.split("-")
        }
        if identifier_tokens & _EXCLUDED_PRIMARY_CONTEXT:
            return True
        if any(
            token.startswith(prefix)
            for token in identifier_tokens
            for prefix in _EXCLUDED_PRIMARY_CONTEXT_PREFIXES
        ):
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _has_claim_label_shape(item: Tag, raw_text: str) -> bool:
    heading = item.find(["strong", "b"])
    if (
        isinstance(heading, Tag)
        and heading.find_parent("li") is item
        and _clean_text(heading.get_text(" ", strip=True))
    ):
        return True
    label, separator, _ = raw_text.partition(":")
    return bool(separator and 2 <= len(label.strip()) <= 80)


def _is_mostly_links(items: Sequence[Tag], raw_texts: Sequence[str]) -> bool:
    linked_items = 0
    for item, raw_text in zip(items, raw_texts):
        link_text = _clean_text(
            " ".join(link.get_text(" ", strip=True) for link in item.find_all("a"))
        )
        if link_text and len(link_text) / len(raw_text) >= 0.8:
            linked_items += 1
    return linked_items * 2 > len(items)


def _has_primary_fact_signal(raw_text: str) -> bool:
    has_ip_rating = bool(
        re.search(_IP_RATING_PATTERN, raw_text, re.IGNORECASE)
        and re.search(
            r"\b(?:dust|immers|protection|rated|rating|resistan|water)",
            raw_text,
            re.IGNORECASE,
        )
    )
    has_playback_duration = bool(
        re.search(
            r"\b(?:battery\s+life|playback|playtime|runtime)\b",
            raw_text,
            re.IGNORECASE,
        )
        and re.search(r"\b\d+(?:\.\d+)?\s*[- ]?\s*(?:hours?|hrs?)\b", raw_text, re.IGNORECASE)
    )
    has_output_power = bool(
        re.search(r"\b(?:output|power)\b", raw_text, re.IGNORECASE)
        and re.search(r"\b\d+(?:\.\d+)?\s*(?:W|watts?)\b", raw_text, re.IGNORECASE)
    )
    has_floating = bool(
        re.search(
            r"\b(?:can\s+(?:even\s+)?float|floats|floating\s+"
            r"(?:design|capability)|stays?\s+afloat)\b",
            raw_text,
            re.IGNORECASE,
        )
    )
    has_included_strap = bool(
        re.search(
            r"\b(?:includes?|included|comes?\s+with|provides?|provided\s+with)\b"
            r".{0,50}\b(?:carrying\s+)?strap\b",
            raw_text,
            re.IGNORECASE,
        )
    )
    has_tws = bool(
        re.search(
            r"\b(?:True\s+Wireless\s+Stereo|TWS)\b",
            raw_text,
            re.IGNORECASE,
        )
        and re.search(
            r"\b(?:compatib|connect|enable|pair|support)",
            raw_text,
            re.IGNORECASE,
        )
    )
    has_immersion = bool(
        re.search(r"\b(?:immers|submerg|water)", raw_text, re.IGNORECASE)
        and re.search(
            r"\b\d+(?:\.\d+)?\s*(?:ft|foot|feet|m|meter|meters|metre|metres)\b"
            r".{0,80}?\bfor\s+(?:up\s+to\s+|approximately\s+|"
            r"about\s+|at\s+least\s+)?\d+(?:\.\d+)?\s*"
            r"(?:min|mins|minute|minutes|hour|hours)\b",
            raw_text,
            re.IGNORECASE,
        )
    )
    return any(
        (
            has_ip_rating,
            has_playback_duration,
            has_output_power,
            has_floating,
            has_included_strap,
            has_tws,
            has_immersion,
        )
    )


def _is_primary_claim_list(candidate_list: Tag, product_h1: Tag) -> bool:
    main = candidate_list.find_parent("main")
    if not isinstance(main, Tag) or product_h1.find_parent("main") is not main:
        return False
    nearest_heading = candidate_list.find_previous(["h1", "h2", "h3", "h4"])
    if nearest_heading is not product_h1:
        return False
    if not _close_common_ancestor(candidate_list, product_h1):
        return False
    if _has_excluded_primary_context(candidate_list, main):
        return False

    items = candidate_list.find_all("li", recursive=False)
    if not 2 <= len(items) <= 10:
        return False
    raw_texts = [_direct_list_item_text(item, candidate_list) for item in items]
    if any(not text or len(text) > 600 for text in raw_texts):
        return False
    present_texts = [text for text in raw_texts if text]
    if _is_mostly_links(items, present_texts):
        return False
    label_count = sum(
        _has_claim_label_shape(item, raw_text)
        for item, raw_text in zip(items, present_texts)
    )
    fact_count = sum(_has_primary_fact_signal(raw_text) for raw_text in present_texts)
    return (
        label_count * 3 >= len(items) * 2
        and fact_count * 2 >= len(items)
    )


def _primary_claim_evidence(
    raw_text: str,
    source_url: str,
    product_h1: Tag,
) -> ClaimEvidence:
    return ClaimEvidence(
        raw_text=raw_text,
        source_url=source_url,
        extraction_method="primary_product_claim_list",
        section_heading=_clean_text(product_h1.get_text(" ", strip=True)),
    )


def _extract_ip_claim(
    raw_text: str,
    source_url: str,
    product_h1: Tag,
) -> Optional[ProductSpec]:
    rating_match = re.search(_IP_RATING_PATTERN, raw_text, re.IGNORECASE)
    if not rating_match or not re.search(
        r"\b(?:dust|immers|protection|rated|rating|resistan|water)",
        raw_text,
        re.IGNORECASE,
    ):
        return None
    return ProductSpec(
        key="water_resistance",
        label="Water Resistance",
        value=rating_match.group(0).upper(),
        source_url=source_url,
        confidence=CONFIDENCE_BY_SOURCE["primary_product_claim_list"],
        verified=False,
        evidence=_primary_claim_evidence(raw_text, source_url, product_h1),
    )


def _primary_spec(
    key: str,
    label: str,
    value: object,
    source_url: str,
    product_h1: Tag,
    raw_text: str,
    unit: Optional[str] = None,
) -> ProductSpec:
    return ProductSpec(
        key=key,
        label=label,
        value=value,
        unit=unit,
        source_url=source_url,
        confidence=CONFIDENCE_BY_SOURCE["primary_product_claim_list"],
        verified=False,
        evidence=_primary_claim_evidence(raw_text, source_url, product_h1),
    )


def _primary_feature(
    feature_id: str,
    name: str,
    source_url: str,
    product_h1: Tag,
    raw_text: str,
    description: Optional[str] = None,
) -> ProductFeature:
    return ProductFeature(
        id=feature_id,
        name=name,
        description=description,
        source_url=source_url,
        confidence=CONFIDENCE_BY_SOURCE["primary_product_claim_list"],
        verified=False,
        evidence=_primary_claim_evidence(raw_text, source_url, product_h1),
    )


def _extract_playback_claim(
    raw_text: str,
    source_url: str,
    product_h1: Tag,
) -> Optional[ProductSpec]:
    if not re.search(
        r"\b(?:battery\s+life|playback|playtime|runtime)\b",
        raw_text,
        re.IGNORECASE,
    ):
        return None
    qualified = re.search(
        r"\b(?P<qualifier>up\s+to|approximately|about|at\s+least)\s+"
        r"(?P<number>\d+(?:\.\d+)?)\s*[- ]?\s*(?:hours?|hrs?)\b",
        raw_text,
        re.IGNORECASE,
    )
    duration = qualified or re.search(
        r"\b(?P<number>\d+(?:\.\d+)?)\s*[- ]?\s*(?:hours?|hrs?)\b",
        raw_text,
        re.IGNORECASE,
    )
    if not duration:
        return None
    qualifier = duration.groupdict().get("qualifier")
    value = " ".join(
        part
        for part in (
            qualifier.casefold() if qualifier else None,
            duration.group("number"),
            "hours",
        )
        if part
    )
    return _primary_spec(
        "playback_time",
        "Playback Time",
        value,
        source_url,
        product_h1,
        raw_text,
    )


def _extract_output_power_claim(
    raw_text: str,
    source_url: str,
    product_h1: Tag,
) -> Optional[ProductSpec]:
    if not re.search(r"\b(?:output|power)\b", raw_text, re.IGNORECASE):
        return None
    power = re.search(
        r"\b(?P<value>\d+(?:\.\d+)?)\s*(?:W|watts?)\b",
        raw_text,
        re.IGNORECASE,
    )
    if not power:
        return None
    numeric_value = _numeric_value(power.group("value"))
    if numeric_value is None:
        return None
    return _primary_spec(
        "output_power",
        "Output Power",
        numeric_value,
        source_url,
        product_h1,
        raw_text,
        unit="W",
    )


def _qualified_measurement(
    qualifier: Optional[str],
    number: str,
    unit: str,
) -> str:
    normalized_units = {
        "ft": "feet",
        "foot": "feet",
        "feet": "feet",
        "m": "meters",
        "meter": "meters",
        "meters": "meters",
        "metre": "meters",
        "metres": "meters",
        "min": "minutes",
        "mins": "minutes",
        "minute": "minutes",
        "minutes": "minutes",
        "hour": "hours",
        "hours": "hours",
    }
    parts = [
        qualifier.casefold() if qualifier else None,
        number,
        normalized_units[unit.casefold()],
    ]
    return " ".join(part for part in parts if part)


def _extract_immersion_claim(
    raw_text: str,
    source_url: str,
    product_h1: Tag,
) -> Optional[ProductSpec]:
    if not re.search(r"\b(?:immers|submerg|water)", raw_text, re.IGNORECASE):
        return None
    measurement = re.search(
        r"(?P<depth_qualifier>up\s+to|approximately|about|at\s+least)?\s*"
        r"(?P<depth>\d+(?:\.\d+)?)\s*"
        r"(?P<depth_unit>ft|foot|feet|m|meter|meters|metre|metres)\b"
        r".{0,80}?\bfor\s+"
        r"(?P<duration_qualifier>up\s+to|approximately|about|at\s+least)?\s*"
        r"(?P<duration>\d+(?:\.\d+)?)\s*"
        r"(?P<duration_unit>min|mins|minute|minutes|hour|hours)\b",
        raw_text,
        re.IGNORECASE,
    )
    if not measurement:
        return None
    depth = _qualified_measurement(
        measurement.group("depth_qualifier"),
        measurement.group("depth"),
        measurement.group("depth_unit"),
    )
    duration = _qualified_measurement(
        measurement.group("duration_qualifier"),
        measurement.group("duration"),
        measurement.group("duration_unit"),
    )
    return _primary_spec(
        "immersion_protection",
        "Immersion Protection",
        f"{depth} for {duration}",
        source_url,
        product_h1,
        raw_text,
    )


def _extract_primary_features(
    raw_text: str,
    source_url: str,
    product_h1: Tag,
) -> List[ProductFeature]:
    features: List[ProductFeature] = []
    if re.search(
        r"\b(?:can\s+(?:even\s+)?float|floats|floating\s+(?:design|capability)|"
        r"stays?\s+afloat)\b",
        raw_text,
        re.IGNORECASE,
    ):
        features.append(
            _primary_feature(
                "floating-design",
                "Floating design",
                source_url,
                product_h1,
                raw_text,
            )
        )
    if re.search(
        r"\b(?:includes?|included|comes?\s+with|provides?|provided\s+with)\b"
        r".{0,50}\b(?:carrying\s+)?strap\b",
        raw_text,
        re.IGNORECASE,
    ) or re.search(
        r"\b(?:carrying\s+)?strap\b.{0,30}\b(?:included|provided)\b",
        raw_text,
        re.IGNORECASE,
    ):
        features.append(
            _primary_feature(
                "included-carrying-strap",
                "Included carrying strap",
                source_url,
                product_h1,
                raw_text,
            )
        )
    if re.search(r"\b(?:True\s+Wireless\s+Stereo|TWS)\b", raw_text, re.IGNORECASE) and re.search(
        r"\b(?:compatib|connect|enable|pair|support)",
        raw_text,
        re.IGNORECASE,
    ):
        _, separator, explanation = raw_text.partition(":")
        description = (
            explanation.strip()
            if separator
            and re.search(
                r"\b(?:compatib|connect|hold|press|require|to\s+enable|"
                r"two\s+speakers)",
                explanation,
                re.IGNORECASE,
            )
            else None
        )
        features.append(
            _primary_feature(
                "true-wireless-stereo",
                "True Wireless Stereo",
                source_url,
                product_h1,
                raw_text,
                description=description,
            )
        )
    return features


def _extract_primary_product_claims(
    soup: BeautifulSoup,
    source_url: str,
    product_name: str,
    brand: str,
) -> Tuple[List[ProductSpec], List[ProductFeature]]:
    product_h1 = _find_primary_product_h1(soup, product_name, brand)
    if product_h1 is None:
        return [], []
    main = product_h1.find_parent("main")
    if not isinstance(main, Tag):
        return [], []

    specs: List[ProductSpec] = []
    features: List[ProductFeature] = []
    for candidate_list in main.find_all(["ul", "ol"]):
        if not _is_primary_claim_list(candidate_list, product_h1):
            continue
        for item in candidate_list.find_all("li", recursive=False):
            raw_text = _direct_list_item_text(item, candidate_list)
            if not raw_text:
                continue
            spec = _extract_ip_claim(raw_text, source_url, product_h1)
            if spec:
                specs.append(spec)
            playback = _extract_playback_claim(raw_text, source_url, product_h1)
            if playback:
                specs.append(playback)
            output_power = _extract_output_power_claim(
                raw_text,
                source_url,
                product_h1,
            )
            if output_power:
                specs.append(output_power)
            immersion = _extract_immersion_claim(raw_text, source_url, product_h1)
            if immersion:
                specs.append(immersion)
            features.extend(
                _extract_primary_features(raw_text, source_url, product_h1)
            )
    return specs, features


def _nearest_local_common_ancestor(first: Tag, second: Tag) -> Optional[Tag]:
    first_parents = list(first.parents)
    second_parents = list(second.parents)
    for first_distance, ancestor in enumerate(first_parents, start=1):
        if ancestor not in second_parents:
            continue
        if not isinstance(ancestor, Tag) or ancestor.name in {"main", "body", "html"}:
            return None
        second_distance = second_parents.index(ancestor) + 1
        return ancestor if first_distance + second_distance <= 8 else None
    return None


def _context_tokens(tag: Tag, stop_at: Tag) -> Set[str]:
    tokens: Set[str] = set()
    current: Optional[Tag] = tag
    while current is not None:
        tokens.update(
            token
            for identifier in _identifier_values(current)
            for token in identifier.split("-")
        )
        if current is stop_at:
            break
        current = current.parent if isinstance(current.parent, Tag) else None
    return tokens


def _has_excluded_gallery_context(tag: Tag, stop_at: Tag) -> bool:
    current: Optional[Tag] = tag
    while current is not None and current is not stop_at:
        if current.name in {"header", "footer", "nav", "aside", "dialog"}:
            return True
        tokens = {
            token
            for identifier in _identifier_values(current)
            for token in identifier.split("-")
        }
        if tokens & _EXCLUDED_GALLERY_CONTEXT:
            return True
        if any(
            token.startswith(prefix)
            for token in tokens
            for prefix in _EXCLUDED_GALLERY_CONTEXT_PREFIXES
        ):
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _is_product_gallery_image(image: Tag, product_h1: Tag, main: Tag) -> bool:
    if image.find_parent("main") is not main:
        return False
    common_ancestor = _nearest_local_common_ancestor(image, product_h1)
    if common_ancestor is None or _has_excluded_gallery_context(image, main):
        return False
    image_context = _context_tokens(image, common_ancestor)
    product_context = _context_tokens(product_h1, common_ancestor)
    combined_context = image_context | product_context
    return bool(
        image_context & _GALLERY_CONTEXT_TOKENS
        and combined_context & _PRODUCT_DETAIL_CONTEXT_TOKENS
        and "product" in combined_context
    )


def _valid_srcset_url(value: str, source_url: str) -> bool:
    parts = urlsplit(urljoin(source_url, value))
    return bool(
        parts.scheme.casefold() in {"http", "https"}
        and parts.hostname
        and parts.username is None
        and parts.password is None
    )


def _largest_srcset_candidate(
    value: Any,
    source_url: str,
) -> Optional[Tuple[str, int]]:
    text = _clean_text(value)
    if not text:
        return None
    candidates: List[Tuple[str, int]] = []
    for entry in text.split(","):
        parts = entry.split()
        if len(parts) != 2 or not re.fullmatch(r"[1-9]\d*w", parts[1]):
            return None
        if not _valid_srcset_url(parts[0], source_url):
            return None
        candidates.append((parts[0], int(parts[1][:-1])))
    return max(candidates, key=lambda candidate: candidate[1], default=None)


def _html_gallery_image(
    image: Tag,
    source_url: str,
) -> Optional[ProductImage]:
    srcsets: List[Tuple[str, int]] = []
    picture = image.find_parent("picture")
    if isinstance(picture, Tag):
        for source in picture.find_all("source"):
            for attribute in ("srcset", "data-srcset"):
                candidate = _largest_srcset_candidate(
                    source.get(attribute),
                    source_url,
                )
                if candidate:
                    srcsets.append(candidate)
    for attribute in ("srcset", "data-srcset"):
        candidate = _largest_srcset_candidate(image.get(attribute), source_url)
        if candidate:
            srcsets.append(candidate)
    if srcsets:
        image_url, declared_width = max(
            srcsets,
            key=lambda candidate: candidate[1],
        )
        return build_product_image(
            image_url,
            source_url,
            "html_product_gallery_image",
            CONFIDENCE_BY_SOURCE["html_product_gallery_image"],
            width=declared_width,
        )

    for attribute in ("data-src", "src"):
        candidate = build_product_image(
            image.get(attribute),
            source_url,
            "html_product_gallery_image",
            CONFIDENCE_BY_SOURCE["html_product_gallery_image"],
            width=image.get("width"),
            height=image.get("height"),
        )
        if candidate:
            return candidate
    return None


def _extract_html_gallery_images(
    soup: BeautifulSoup,
    source_url: str,
    product_name: str,
    brand: str,
) -> List[ProductImage]:
    product_h1 = _find_primary_product_h1(soup, product_name, brand)
    if product_h1 is None:
        return []
    main = product_h1.find_parent("main")
    if not isinstance(main, Tag):
        return []
    images: List[ProductImage] = []
    for image in main.find_all("img"):
        if not _is_product_gallery_image(image, product_h1, main):
            continue
        candidate = _html_gallery_image(image, source_url)
        if candidate:
            images.append(candidate)
    return images


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
    image_objects_by_id = _image_objects_by_id(documents, source_url)
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

    source_name = (
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
    name = (
        canonicalize_product_name(
            source_name,
            brand,
            corroborating_names=tuple(
                value
                for value in (
                    _clean_text(heading.get_text(" ", strip=True))
                    for heading in soup.find_all("h1")
                )
                if value
            ),
        )
        if source_name and brand
        else source_name
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
    primary_specs, primary_features = _extract_primary_product_claims(
        soup,
        source_url,
        name,
        brand,
    )
    specs = _deduplicate_specs(
        [
            *(
                spec
                for entity in jsonld_entities
                for spec in _extract_jsonld_specs(entity, source_url)
            ),
            *_extract_table_specs(soup, source_url),
            *_extract_definition_specs(soup, source_url),
            *primary_specs,
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
            *primary_features,
        ]
    )
    images = _deduplicate_images(
        [
            *(
                image
                for entity in jsonld_entities
                for image in _extract_jsonld_images(
                    entity,
                    source_url,
                    image_objects_by_id,
                )
            ),
            *_extract_open_graph_images(
                soup,
                source_url,
                source_name,
                name,
                brand,
            ),
            *_extract_html_gallery_images(
                soup,
                source_url,
                name,
                brand,
            ),
        ]
    )[:MAX_PRODUCT_IMAGE_CANDIDATES]

    canonical_url = _canonical_url(soup, source_url)
    entity_scope = "group" if is_product_group else "product"
    variant_signal = None
    if is_product_group and selection and selection.variant:
        group_url = _first_url_from_value(
            page_entity_data.get("url"),
            source_url,
        )
        variant_signal = stable_variant_identity(
            selection.variant,
            source_url,
            group_url=group_url or canonical_url or source_url,
        )
        if variant_signal:
            entity_scope = "variant"

    try:
        product_id = generate_source_scoped_product_id(
            source_url,
            entity_scope,
            canonical_url=canonical_url,
            variant_signal=variant_signal,
        )
    except ValueError as error:
        raise ProductExtractionError(str(error)) from error

    return Product(
        id=product_id,
        source_name=source_name,
        name=name,
        brand=brand,
        category=category,
        description=description,
        variant=variant,
        images=images,
        specs=specs,
        features=features,
        sources=_deduplicate_sources([source]),
    )
