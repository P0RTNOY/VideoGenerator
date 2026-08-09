"""Pure helpers for conservative product display and source identity."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit


TRACKING_QUERY_KEYS = frozenset({"gclid", "fbclid"})
ENTITY_SCOPES = frozenset({"group", "product", "variant"})
_STRUCTURAL_SEPARATOR = re.compile(r"\s[\N{EN DASH}\N{EM DASH}]\s|\|")


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def slugify_identifier(value: str) -> str:
    """Return the existing ASCII slug format used by local identifiers."""

    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-")


def _remove_one_brand_prefix(value: str, brand: str) -> Tuple[str, bool]:
    if not value.casefold().startswith(brand.casefold()):
        return value, False
    remainder = value[len(brand) :]
    if not remainder or remainder[0].isalnum():
        return value, False
    remainder = remainder.lstrip(" \t|\N{EN DASH}\N{EM DASH}-:,.()[]")
    if not remainder or not any(character.isalnum() for character in remainder):
        return value, False
    return remainder, True


def _remove_repeated_brand_prefix(value: str, brand: str) -> str:
    result = value
    while True:
        result, removed = _remove_one_brand_prefix(result, brand)
        if not removed:
            return result


def _has_model_signal(value: str) -> bool:
    return any(character.isdigit() for character in value)


def _normalized_comparison_name(value: str, brand: str) -> str:
    cleaned = _clean_text(value) or ""
    return _remove_repeated_brand_prefix(cleaned, brand).casefold()


def canonicalize_product_name(
    source_name: str,
    brand: str,
    corroborating_names: Sequence[str] = (),
) -> str:
    """Derive a conservative display name while preserving source wording."""

    clean_source = _clean_text(source_name)
    clean_brand = _clean_text(brand)
    if clean_source is None:
        raise ValueError("source_name must be a non-empty string")
    if clean_brand is None:
        raise ValueError("brand must be a non-empty string")

    separator = _STRUCTURAL_SEPARATOR.search(clean_source)
    if separator:
        left = clean_source[: separator.start()].strip()
        candidate = _remove_repeated_brand_prefix(left, clean_brand)
        corroborated = any(
            _normalized_comparison_name(name, clean_brand) == candidate.casefold()
            for name in tuple(corroborating_names)
            if _clean_text(name)
        )
        if candidate and (_has_model_signal(candidate) or corroborated):
            return candidate
        return clean_source

    return _remove_repeated_brand_prefix(clean_source, clean_brand)


def _identity_host(url: str) -> Optional[str]:
    parts = urlsplit(url)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.casefold()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        return None
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (
        parts.scheme.casefold() == "https" and port == 443
    )
    if port is not None and not default_port:
        host = f"{host}:{port}"
    return host


def _is_tracking_parameter(key: str) -> bool:
    normalized = key.casefold()
    return normalized.startswith("utm_") or normalized in TRACKING_QUERY_KEYS


def normalize_identity_url(
    final_url: str,
    canonical_url: Optional[str] = None,
) -> str:
    """Normalize a final URL, preferring only a same-origin canonical URL."""

    clean_final = _clean_text(final_url)
    if clean_final is None or _identity_host(clean_final) is None:
        raise ValueError("final_url must be an absolute HTTP(S) URL")

    identity_url = clean_final
    clean_canonical = _clean_text(canonical_url)
    if clean_canonical:
        absolute_canonical = urljoin(clean_final, clean_canonical)
        if _identity_host(absolute_canonical) == _identity_host(clean_final):
            identity_url = absolute_canonical

    parts = urlsplit(identity_url)
    host = _identity_host(identity_url)
    if host is None:
        raise ValueError("identity URL must be an absolute HTTP(S) URL")
    path = parts.path.rstrip("/") or "/"
    query_items = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_parameter(key)
    )
    query = urlencode(query_items, doseq=True)
    normalized = f"{host}{path}"
    return f"{normalized}?{query}" if query else normalized


def generate_source_scoped_product_id(
    final_url: str,
    entity_scope: str,
    canonical_url: Optional[str] = None,
    variant_signal: Optional[str] = None,
) -> str:
    """Build a display-independent source-scoped Product identifier."""

    if entity_scope not in ENTITY_SCOPES:
        raise ValueError("entity_scope must be group, product, or variant")
    clean_signal = _clean_text(variant_signal)
    if entity_scope == "variant" and clean_signal is None:
        raise ValueError("variant scope requires a stable variant signal")
    if entity_scope != "variant" and clean_signal is not None:
        raise ValueError("variant signals are only valid for variant scope")

    normalized_url = normalize_identity_url(final_url, canonical_url)
    parts = urlsplit(f"//{normalized_url}")
    identity_parts = [parts.netloc, entity_scope, parts.path.strip("/") or "root"]
    if parts.query:
        identity_parts.append(parts.query)
    if clean_signal:
        identity_parts.append(clean_signal)
    product_id = slugify_identifier(" ".join(identity_parts))
    if not product_id:
        raise ValueError("could not generate a Product ID from source identity")
    return product_id


def _first_text(value: Any) -> Optional[str]:
    values: Iterable[Any] = value if isinstance(value, list) else (value,)
    for item in values:
        if isinstance(item, Mapping):
            for key in ("url", "@id", "value", "name"):
                text = _clean_text(item.get(key))
                if text:
                    return text
        else:
            text = _clean_text(item)
            if text:
                return text
    return None


def stable_variant_identity(
    variant: Mapping[str, Any],
    base_url: str,
    group_url: Optional[str] = None,
) -> Optional[str]:
    """Return the selected variant's first stable identity signal, if any."""

    for key in ("sku", "mpn", "model"):
        value = _first_text(variant.get(key))
        if value:
            return f"{key}:{value}"

    variant_url = _first_text(variant.get("url"))
    if variant_url:
        absolute_variant_url = urljoin(base_url, variant_url)
        try:
            normalized_variant = normalize_identity_url(absolute_variant_url)
            normalized_group = (
                normalize_identity_url(urljoin(base_url, group_url))
                if _clean_text(group_url)
                else normalize_identity_url(base_url)
            )
        except ValueError:
            pass
        else:
            if normalized_variant != normalized_group:
                return f"url:{normalized_variant}"

    structured_id = _first_text(variant.get("@id"))
    if structured_id:
        if structured_id.startswith(("http://", "https://", "/")):
            absolute_id = urljoin(base_url, structured_id)
            try:
                normalized_id = normalize_identity_url(absolute_id)
            except ValueError:
                return None
            fragment = urlsplit(absolute_id).fragment
            suffix = f"#{fragment}" if fragment else ""
            return f"id:{normalized_id}{suffix}"
        return f"id:{structured_id}"
    return None
