"""Bounded research and reconciliation for explicitly supplied product pages."""

from __future__ import annotations

import re
import unicodedata
from typing import List, Literal, Optional, Sequence

from pydantic import Field, model_validator

from product import NonEmptyStr, Product, ProductModel, ProductVariant
from product_research import ProductResearchError, research_product


MAX_SUPPLEMENTARY_PRODUCT_SOURCES = 3

ReconciliationStatus = Literal["matched", "not_matched", "ambiguous"]
ProductRelationship = Literal["exact_product", "product_family"]


class TooManyProductSourcesError(ValueError):
    """Raised before fetching when more than three supplementary URLs exist."""


class ProductReconciliation(ProductModel):
    """Deterministic same-product decision for one supplementary Product."""

    status: ReconciliationStatus
    relationship: Optional[ProductRelationship] = None
    reason: NonEmptyStr

    @model_validator(mode="after")
    def require_relationship_only_for_matches(self) -> "ProductReconciliation":
        if self.status == "matched" and self.relationship is None:
            raise ValueError("matched reconciliation requires a relationship")
        if self.status != "matched" and self.relationship is not None:
            raise ValueError("only matched reconciliation may have a relationship")
        return self


class ProductResearchFailure(ProductModel):
    """One explicit supplementary source that failed independent research."""

    error_type: NonEmptyStr
    message: NonEmptyStr


class SupplementaryProductSourceResult(ProductModel):
    """Independent supplementary Product research or its structured failure."""

    requested_url: str = Field(strict=True)
    source_role: Literal["supplementary"] = "supplementary"
    product: Optional[Product] = None
    reconciliation: Optional[ProductReconciliation] = None
    failure: Optional[ProductResearchFailure] = None

    @model_validator(mode="after")
    def require_success_or_failure(self) -> "SupplementaryProductSourceResult":
        if self.product is not None:
            if not self.requested_url.strip():
                raise ValueError(
                    "successful supplementary research requires a non-empty requested URL"
                )
            if self.reconciliation is None or self.failure is not None:
                raise ValueError(
                    "successful supplementary research requires reconciliation only"
                )
            return self
        if self.failure is None or self.reconciliation is not None:
            raise ValueError(
                "failed supplementary research requires a failure only"
            )
        return self


class MultiSourceProductResearch(ProductModel):
    """One authoritative primary Product plus isolated supplementary results."""

    schema_version: Literal[1] = 1
    primary_requested_url: NonEmptyStr
    primary_role: Literal["primary"] = "primary"
    primary_product: Product
    supplementary_sources: List[SupplementaryProductSourceResult] = Field(
        default_factory=list,
        max_length=MAX_SUPPLEMENTARY_PRODUCT_SOURCES,
    )


_GENERIC_PRODUCT_NAME_TOKENS = {
    "audio",
    "bluetooth",
    "card",
    "device",
    "earbud",
    "earbuds",
    "gaming",
    "graphics",
    "headphone",
    "headphones",
    "laptop",
    "portable",
    "product",
    "speaker",
    "speakers",
    "wireless",
}


def _ascii_tokens(value: str) -> List[str]:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", ascii_text.casefold())


def _normalized_identifier(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = "".join(_ascii_tokens(value))
    return normalized or None


def _distinctive_name_tokens(product: Product) -> frozenset:
    brand_tokens = set(_ascii_tokens(product.brand))
    return frozenset(
        token
        for token in _ascii_tokens(product.name)
        if token not in brand_tokens
        and token not in _GENERIC_PRODUCT_NAME_TOKENS
    )


def _model_tokens(tokens: frozenset) -> frozenset:
    return frozenset(token for token in tokens if any(char.isdigit() for char in token))


def _model_number(product: Product) -> Optional[str]:
    if product.variant is None:
        return None
    return _normalized_identifier(product.variant.model_number)


def _variant_identity_values(variant: Optional[ProductVariant]) -> tuple:
    if variant is None:
        return ()
    return tuple(
        (field_name, value)
        for field_name, value in (
            ("model_number", _normalized_identifier(variant.model_number)),
            ("color", _normalized_identifier(variant.color)),
            ("size", _normalized_identifier(variant.size)),
        )
        if value
    )


def _match_relationship(primary: Product, supplementary: Product) -> ProductRelationship:
    primary_values = _variant_identity_values(primary.variant)
    supplementary_values = _variant_identity_values(supplementary.variant)
    primary_is_group = "-group-" in f"-{primary.id.casefold()}-"
    if primary_is_group and supplementary.variant is not None:
        return "product_family"
    if bool(primary_values) != bool(supplementary_values):
        return "product_family"
    if primary_values and supplementary_values and primary_values != supplementary_values:
        return "product_family"
    return "exact_product"


def reconcile_products(
    primary: Product,
    supplementary: Product,
) -> ProductReconciliation:
    """Classify one independently researched Product without mutating either."""

    primary_brand = _normalized_identifier(primary.brand)
    supplementary_brand = _normalized_identifier(supplementary.brand)
    if primary_brand != supplementary_brand:
        return ProductReconciliation(
            status="not_matched",
            reason="brand_mismatch",
        )

    primary_model = _model_number(primary)
    supplementary_model = _model_number(supplementary)
    if primary_model and supplementary_model:
        if primary_model != supplementary_model:
            return ProductReconciliation(
                status="not_matched",
                reason="model_number_conflict",
            )
        return ProductReconciliation(
            status="matched",
            relationship=_match_relationship(primary, supplementary),
            reason="shared_model_number",
        )

    primary_tokens = _distinctive_name_tokens(primary)
    supplementary_tokens = _distinctive_name_tokens(supplementary)
    if not primary_tokens or not supplementary_tokens:
        return ProductReconciliation(
            status="not_matched",
            reason="insufficient_distinctive_identity",
        )
    if primary_tokens == supplementary_tokens:
        return ProductReconciliation(
            status="matched",
            relationship=_match_relationship(primary, supplementary),
            reason="exact_brand_and_name",
        )

    primary_models = _model_tokens(primary_tokens)
    supplementary_models = _model_tokens(supplementary_tokens)
    has_partial_family_overlap = (
        primary_models
        and primary_models == supplementary_models
        and (
            primary_tokens < supplementary_tokens
            or supplementary_tokens < primary_tokens
        )
    )
    if has_partial_family_overlap:
        return ProductReconciliation(
            status="ambiguous",
            reason="partial_model_family_overlap",
        )
    return ProductReconciliation(
        status="not_matched",
        reason="name_mismatch",
    )


def research_product_sources(
    primary_url: str,
    supplementary_urls: Sequence[str] = (),
    *,
    timeout: float = 15,
    session: object = None,
) -> MultiSourceProductResearch:
    """Research only caller-supplied URLs, sequentially and independently."""

    requested_supplementary = list(supplementary_urls)
    if len(requested_supplementary) > MAX_SUPPLEMENTARY_PRODUCT_SOURCES:
        raise TooManyProductSourcesError(
            "At most three supplementary product-page URLs are allowed."
        )

    primary_product = research_product(
        primary_url,
        timeout=timeout,
        session=session,
    ).model_copy(deep=True)
    supplementary_sources: List[SupplementaryProductSourceResult] = []
    for requested_url in requested_supplementary:
        try:
            product = research_product(
                requested_url,
                timeout=timeout,
                session=session,
            ).model_copy(deep=True)
        except ProductResearchError as error:
            supplementary_sources.append(
                SupplementaryProductSourceResult(
                    requested_url=requested_url,
                    failure=ProductResearchFailure(
                        error_type=type(error).__name__,
                        message=str(error) or type(error).__name__,
                    ),
                )
            )
            continue
        supplementary_sources.append(
            SupplementaryProductSourceResult(
                requested_url=requested_url,
                product=product,
                reconciliation=reconcile_products(primary_product, product),
            )
        )

    return MultiSourceProductResearch(
        primary_requested_url=primary_url,
        primary_product=primary_product,
        supplementary_sources=supplementary_sources,
    )
