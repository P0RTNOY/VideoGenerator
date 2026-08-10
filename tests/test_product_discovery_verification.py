import unittest
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import patch

from pydantic import ValidationError

from ad_claims import generate_ad_claim_candidates
from product import (
    ClaimEvidence,
    Product,
    ProductFeature,
    ProductSource,
    ProductSpec,
)
from product_discovery_orchestration import (
    DiscoverySelectionError,
    SelectedDiscoveryVerification,
    verify_selected_discovery_sources,
)
from product_source_research import ProductReconciliation
from supplementary_product_discovery import (
    SupplementaryProductSourceDiscovery,
    SupplementarySourceCandidate,
    SupplementarySourceProposal,
)


FIXED_EXTRACTED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def evidence(raw_text: str, source_url: str) -> ClaimEvidence:
    return ClaimEvidence(
        raw_text=raw_text,
        source_url=source_url,
        extraction_method="test_fixture",
        section_heading="Specifications",
        extracted_at=FIXED_EXTRACTED_AT,
    )


def product(
    *,
    product_id: str,
    source_url: str,
    playback_time: Optional[str] = None,
    extra_spec: bool = False,
    feature: bool = False,
    verified: bool = False,
) -> Product:
    specs = []
    if playback_time is not None:
        specs.append(
            ProductSpec(
                key="playback_time",
                label="Playback Time",
                value=playback_time,
                source_url=source_url,
                verified=verified,
                evidence=evidence(playback_time, source_url),
            )
        )
    if extra_spec:
        specs.append(
            ProductSpec(
                key="retailer_only",
                label="Retailer Only",
                value=True,
                source_url=source_url,
            )
        )
    features = []
    if feature:
        features.append(
            ProductFeature(
                id="floating-design",
                name="Floating design",
                source_url=source_url,
                verified=verified,
                evidence=evidence("Floating design", source_url),
            )
        )
    return Product(
        id=product_id,
        name="Trail 100",
        brand="Acme Audio",
        category="Portable Speakers",
        specs=specs,
        features=features,
        sources=[
            ProductSource(
                url=source_url,
                source_type="product_page",
                domain=source_url.split("/")[2],
            )
        ],
    )


def supplementary_discovery(
    primary: Product,
    *entries: tuple[str, Product],
) -> SupplementaryProductSourceDiscovery:
    reconciliation = ProductReconciliation(
        status="matched",
        relationship="exact_product",
        reason="exact_brand_and_name",
    )
    candidates = []
    proposals = []
    for rank, (discovered_url, supplementary) in enumerate(entries, start=1):
        accepted_url = supplementary.sources[0].url
        publisher = accepted_url.split("/")[2]
        candidates.append(
            SupplementarySourceCandidate(
                discovered_url=discovered_url,
                query="Acme Audio Trail 100",
                search_rank=rank,
                status="accepted",
                reason="exact_product_direct_retailer",
                final_url=accepted_url,
                publisher=publisher,
                source_classification="direct_retailer_product",
                classification_reasons=[
                    "structured_product",
                    "structured_offer",
                    "commerce_control",
                    "price_signal",
                ],
                product=supplementary,
                reconciliation=reconciliation,
            )
        )
        proposals.append(
            SupplementarySourceProposal(
                discovered_url=discovered_url,
                accepted_url=accepted_url,
                publisher=publisher,
                product=supplementary,
            )
        )
    return SupplementaryProductSourceDiscovery(
        primary_product_id=primary.id,
        primary_url=primary.sources[0].url,
        status="found" if proposals else "not_found",
        queries=["Acme Audio Trail 100"],
        proposals=proposals,
        candidates=candidates,
        reason=(
            "direct_retailer_product_sources_proposed"
            if proposals
            else "no_direct_retailer_product_sources_accepted"
        ),
    )


class ProductDiscoveryVerificationTests(unittest.TestCase):
    @patch("product_research.research_product", side_effect=AssertionError("refetch"))
    @patch(
        "product_discovery_orchestration.research_product_sources",
        side_effect=AssertionError("refetch"),
    )
    def test_empty_selection_is_primary_only_without_network_refetch(
        self,
        research_sources,
        research_product,
    ) -> None:
        primary_url = "https://acmeaudio.example/products/trail-100"
        primary = product(
            product_id="official-primary",
            source_url=primary_url,
            playback_time="up to 20 hours",
            feature=True,
        )
        primary_before = deepcopy(primary)
        discovery = supplementary_discovery(primary)
        discovery_before = deepcopy(discovery)

        result = verify_selected_discovery_sources(primary, discovery, [])

        research_sources.assert_not_called()
        research_product.assert_not_called()
        self.assertEqual(result.primary_product_id, primary.id)
        self.assertEqual(result.selected_proposal_urls, [])
        self.assertEqual(result.multi_source_research.supplementary_sources, [])
        self.assertEqual(
            [claim.status for claim in result.verification_report.claim_results],
            ["supported", "supported"],
        )
        self.assertEqual(primary, primary_before)
        self.assertEqual(discovery, discovery_before)
        self.assertIsNot(result.multi_source_research.primary_product, primary)

    @patch("product_research.research_product", side_effect=AssertionError("refetch"))
    @patch(
        "product_discovery_orchestration.research_product_sources",
        side_effect=AssertionError("refetch"),
    )
    def test_selected_discovery_product_is_reused_with_original_provenance(
        self,
        research_sources,
        research_product,
    ) -> None:
        primary_url = "https://acmeaudio.example/products/trail-100"
        discovered_url = "https://retailer.example/go/trail-100"
        final_url = "https://shop.retailer.example/products/trail-100"
        primary = product(
            product_id="official-primary",
            source_url=primary_url,
            playback_time="up to 20 hours",
        )
        supplementary = product(
            product_id="retailer-product",
            source_url=final_url,
            playback_time="up to 20 hours",
        )
        discovery = supplementary_discovery(
            primary,
            (discovered_url, supplementary),
        )
        discovery_before = deepcopy(discovery)

        result = verify_selected_discovery_sources(
            primary,
            discovery,
            [final_url],
        )

        research_sources.assert_not_called()
        research_product.assert_not_called()
        source = result.multi_source_research.supplementary_sources[0]
        self.assertEqual(source.requested_url, discovered_url)
        self.assertEqual(source.product.sources[0].url, final_url)
        self.assertEqual(source.reconciliation, discovery.candidates[0].reconciliation)
        self.assertIsNot(source.product, discovery.proposals[0].product)
        claim = result.verification_report.claim_results[0]
        self.assertEqual(claim.status, "corroborated")
        self.assertEqual(claim.observations[0].requested_url, discovered_url)
        self.assertEqual(claim.observations[0].source_url, final_url)
        self.assertEqual(
            claim.observations[0].evidence.extracted_at,
            FIXED_EXTRACTED_AT,
        )
        self.assertEqual(
            claim.observations[0].evidence,
            supplementary.specs[0].evidence,
        )
        self.assertIsNot(
            claim.observations[0].evidence,
            supplementary.specs[0].evidence,
        )
        self.assertEqual(discovery, discovery_before)

    def test_selected_sources_preserve_proposal_and_product_source_order(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/products/trail-100",
            playback_time="20 hours",
        )
        first = product(
            product_id="retailer-one",
            source_url="https://one.example/final/trail-100",
            playback_time="20 hours",
        )
        first.sources.append(
            ProductSource(
                url="https://one.example/warranty/trail-100",
                source_type="warranty",
            )
        )
        second = product(
            product_id="retailer-two",
            source_url="https://two.example/final/trail-100",
            playback_time="20 hours",
        )
        discovery = supplementary_discovery(
            primary,
            ("https://one.example/requested", first),
            ("https://two.example/requested", second),
        )

        second_only = verify_selected_discovery_sources(
            primary,
            discovery,
            [second.sources[0].url],
        )
        result = verify_selected_discovery_sources(
            primary,
            discovery,
            [second.sources[0].url, first.sources[0].url],
        )

        self.assertEqual(
            second_only.selected_proposal_urls,
            [second.sources[0].url],
        )
        self.assertEqual(
            [source.requested_url for source in second_only.multi_source_research.supplementary_sources],
            ["https://two.example/requested"],
        )
        self.assertEqual(
            result.selected_proposal_urls,
            [first.sources[0].url, second.sources[0].url],
        )
        self.assertEqual(
            [source.requested_url for source in result.multi_source_research.supplementary_sources],
            ["https://one.example/requested", "https://two.example/requested"],
        )
        self.assertEqual(
            result.verification_report.claim_results[0].status,
            "corroborated",
        )
        self.assertEqual(
            result.multi_source_research.supplementary_sources[0].product.sources,
            first.sources,
        )
        self.assertEqual(
            [observation.requested_url for observation in result.verification_report.claim_results[0].observations],
            ["https://one.example/requested", "https://two.example/requested"],
        )

    def test_conflict_dominates_one_or_more_agreements(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://acmeaudio.example/product",
            playback_time="up to 20 hours",
        )
        agreement = product(
            product_id="agreement",
            source_url="https://one.example/product",
            playback_time="up to 20 hours",
        )
        conflict = product(
            product_id="conflict",
            source_url="https://two.example/product",
            playback_time="up to 16 hours",
        )
        second_agreement = product(
            product_id="agreement-two",
            source_url="https://three.example/product",
            playback_time="up to 20 hours",
        )
        discovery = supplementary_discovery(
            primary,
            (agreement.sources[0].url, agreement),
            (conflict.sources[0].url, conflict),
            (second_agreement.sources[0].url, second_agreement),
        )

        conflict_only = verify_selected_discovery_sources(
            primary,
            discovery,
            [conflict.sources[0].url],
        )
        mixed = verify_selected_discovery_sources(
            primary,
            discovery,
            [
                agreement.sources[0].url,
                conflict.sources[0].url,
                second_agreement.sources[0].url,
            ],
        )

        self.assertEqual(conflict_only.verification_report.claim_results[0].status, "conflicting")
        self.assertEqual(mixed.verification_report.claim_results[0].status, "conflicting")
        self.assertEqual(
            [observation.comparison for observation in mixed.verification_report.claim_results[0].observations],
            ["agrees", "conflicts", "agrees"],
        )

    def test_existing_same_domain_qualifier_and_supplementary_only_rules_apply(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://www.example.com/product",
            playback_time="up to 20 hours",
        )
        same_domain = product(
            product_id="same-domain",
            source_url="https://shop.example.com/product",
            playback_time="up to 20 hours",
        )
        qualifier_difference = product(
            product_id="qualifier",
            source_url="https://retailer.test/product",
            playback_time="20 hours",
            extra_spec=True,
            feature=True,
        )
        discovery = supplementary_discovery(
            primary,
            (same_domain.sources[0].url, same_domain),
            (qualifier_difference.sources[0].url, qualifier_difference),
        )

        result = verify_selected_discovery_sources(
            primary,
            discovery,
            [same_domain.sources[0].url, qualifier_difference.sources[0].url],
        )

        self.assertEqual(
            [summary.participation for summary in result.verification_report.source_summaries],
            ["same_publisher", "admitted"],
        )
        self.assertEqual(result.verification_report.claim_results[0].status, "ambiguous")
        self.assertEqual(
            [claim.claim_id for claim in result.verification_report.claim_results],
            ["spec:playback_time"],
        )

    def test_inputs_verified_flags_ad_candidates_and_outputs_are_isolated(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://primary.example/product",
            playback_time="20 hours",
            feature=True,
        )
        supplementary = product(
            product_id="retailer",
            source_url="https://retailer.example/product",
            playback_time="20 hours",
            feature=True,
        )
        discovery = supplementary_discovery(
            primary,
            (supplementary.sources[0].url, supplementary),
        )
        primary_before = deepcopy(primary)
        discovery_before = deepcopy(discovery)
        candidates = generate_ad_claim_candidates(primary)
        candidates_before = deepcopy(candidates)

        result = verify_selected_discovery_sources(
            primary,
            discovery,
            [supplementary.sources[0].url],
        )

        self.assertEqual(primary, primary_before)
        self.assertEqual(discovery, discovery_before)
        self.assertEqual(candidates, candidates_before)
        self.assertFalse(primary.specs[0].verified)
        self.assertFalse(primary.features[0].verified)
        self.assertFalse(supplementary.specs[0].verified)
        self.assertFalse(supplementary.features[0].verified)
        result.multi_source_research.primary_product.specs[0].value = "changed"
        result.multi_source_research.supplementary_sources[0].product.specs[0].evidence.raw_text = "changed"
        self.assertEqual(primary.specs[0].value, "20 hours")
        self.assertEqual(supplementary.specs[0].evidence.raw_text, "20 hours")
        self.assertEqual(
            result.verification_report.claim_results[0].observations[0].evidence.raw_text,
            "20 hours",
        )

    def test_invalid_selection_and_cross_discovery_leakage_are_rejected(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://primary.example/product",
        )
        accepted = product(
            product_id="accepted",
            source_url="https://accepted.example/product",
        )
        discovery = supplementary_discovery(
            primary,
            (accepted.sources[0].url, accepted),
        )
        other = product(
            product_id="other",
            source_url="https://other.example/product",
        )
        other_discovery = supplementary_discovery(
            primary,
            (other.sources[0].url, other),
        )

        for selected in (
            [other_discovery.proposals[0].accepted_url],
            [accepted.sources[0].url, accepted.sources[0].url],
            accepted.sources[0].url,
            [""],
            None,
        ):
            with self.subTest(selected=selected):
                with self.assertRaises(DiscoverySelectionError):
                    verify_selected_discovery_sources(primary, discovery, selected)

    def test_rejected_candidate_and_primary_mismatches_are_rejected(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://primary.example/product",
        )
        rejected_url = "https://marketplace.example/product"
        rejected_discovery = SupplementaryProductSourceDiscovery(
            primary_product_id=primary.id,
            primary_url=primary.sources[0].url,
            status="not_found",
            queries=["Acme Audio Trail 100"],
            candidates=[
                SupplementarySourceCandidate(
                    discovered_url=rejected_url,
                    query="Acme Audio Trail 100",
                    search_rank=1,
                    status="rejected_source_type",
                    reason="aggregate_offer",
                    final_url=rejected_url,
                    publisher="marketplace.example",
                    source_classification="marketplace_or_third_party",
                    classification_reasons=["aggregate_offer"],
                )
            ],
            reason="no_direct_retailer_product_sources_accepted",
        )

        with self.assertRaises(DiscoverySelectionError):
            verify_selected_discovery_sources(
                primary,
                rejected_discovery,
                [rejected_url],
            )

        accepted = product(
            product_id="accepted",
            source_url="https://retailer.example/product",
        )
        discovery = supplementary_discovery(
            primary,
            (accepted.sources[0].url, accepted),
        )
        wrong_id = primary.model_copy(update={"id": "wrong-primary"}, deep=True)
        wrong_url = product(
            product_id=primary.id,
            source_url="https://wrong.example/product",
        )
        for supplied_primary in (wrong_id, wrong_url):
            with self.subTest(primary=supplied_primary):
                with self.assertRaises(DiscoverySelectionError):
                    verify_selected_discovery_sources(
                        supplied_primary,
                        discovery,
                        [],
                    )

    def test_tampered_cross_result_proposal_binding_is_rejected(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://primary.example/product",
        )
        accepted = product(
            product_id="accepted",
            source_url="https://retailer.example/product",
        )
        leaked = product(
            product_id="leaked",
            source_url=accepted.sources[0].url,
        )
        discovery = supplementary_discovery(
            primary,
            (accepted.sources[0].url, accepted),
        )
        discovery = discovery.model_copy(
            update={
                "proposals": [
                    discovery.proposals[0].model_copy(
                        update={"product": leaked},
                        deep=True,
                    )
                ]
            },
            deep=True,
        )

        with self.assertRaises(DiscoverySelectionError):
            verify_selected_discovery_sources(
                primary,
                discovery,
                [accepted.sources[0].url],
            )

    def test_result_model_round_trip_and_unknown_field_rejection(self) -> None:
        primary = product(
            product_id="official-primary",
            source_url="https://primary.example/product",
            playback_time="20 hours",
        )
        result = verify_selected_discovery_sources(
            primary,
            supplementary_discovery(primary),
            [],
        )

        restored = SelectedDiscoveryVerification.model_validate_json(
            result.model_dump_json()
        )

        self.assertEqual(restored, result)
        malformed = result.model_dump()
        malformed["unexpected"] = True
        with self.assertRaises(ValidationError):
            SelectedDiscoveryVerification.model_validate(malformed)


if __name__ == "__main__":
    unittest.main()
