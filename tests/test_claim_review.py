import unittest
from copy import deepcopy
from datetime import datetime, timezone

from pydantic import ValidationError

from ad_claims import AdClaimCandidate, DuplicateAdClaimCandidateError
from claim_review import ClaimReviewItem, project_claim_review_items
from product import ClaimEvidence
from product_claim_verification import (
    ClaimVerificationLookupError,
    ClaimVerificationObservation,
    ClaimVerificationResult,
    ProductClaimVerificationReport,
)


FIXED_EXTRACTED_AT = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def evidence(raw_text: str, source_url: str) -> ClaimEvidence:
    return ClaimEvidence(
        raw_text=raw_text,
        source_url=source_url,
        extraction_method="test_fixture",
        extracted_at=FIXED_EXTRACTED_AT,
    )


def candidate(claim_id: str, *, verified: bool = False) -> AdClaimCandidate:
    source_url = "https://primary.example/product"
    return AdClaimCandidate(
        id=claim_id,
        display_text=f"Review {claim_id}",
        claim_type="spec" if claim_id.startswith("spec:") else "feature",
        source_id=claim_id.split(":", 1)[1],
        source_url=source_url,
        verified=verified,
        evidence=evidence(f"Evidence for {claim_id}", source_url),
    )


def observation(
    claim_id: str,
    requested_url: str,
    comparison: str,
) -> ClaimVerificationObservation:
    return ClaimVerificationObservation(
        requested_url=requested_url,
        source_url=requested_url,
        normalized_domain=requested_url.split("/")[2],
        relationship="exact_product",
        claim_id=claim_id,
        observed_value="20 hours",
        evidence=evidence("20 hours", requested_url),
        comparison=comparison,
    )


def result(
    claim_id: str,
    status: str = "supported",
    observations: list = None,
) -> ClaimVerificationResult:
    return ClaimVerificationResult(
        claim_id=claim_id,
        claim_type="spec" if claim_id.startswith("spec:") else "feature",
        primary_value="Primary value",
        status=status,
        observations=observations or [],
    )


def report(*results: ClaimVerificationResult) -> ProductClaimVerificationReport:
    return ProductClaimVerificationReport(
        product_id="primary-product",
        primary_source_url="https://primary.example/product",
        claim_results=list(results),
    )


class ClaimReviewProjectionTests(unittest.TestCase):
    def test_projection_joins_exact_claim_ids_in_candidate_order(self) -> None:
        first = candidate("spec:playback_time")
        second = candidate("feature:floating-design")
        verification = report(
            result("feature:floating-design"),
            result("spec:playback_time"),
        )

        items = project_claim_review_items([first, second], verification)

        self.assertEqual(
            [item.candidate.id for item in items],
            ["spec:playback_time", "feature:floating-design"],
        )
        self.assertEqual(
            [item.verification_status for item in items],
            ["supported", "supported"],
        )

    def test_source_counts_are_unique_per_comparison_outcome(self) -> None:
        claim_id = "spec:playback_time"
        source_a = "https://a.example/product"
        observations = [
            observation(claim_id, source_a, "agrees"),
            observation(claim_id, source_a, "agrees"),
            observation(claim_id, "https://b.example/product", "conflicts"),
            observation(claim_id, "https://c.example/product", "ambiguous"),
        ]
        verification = report(result(claim_id, "conflicting", observations))

        item = project_claim_review_items(
            [candidate(claim_id)],
            verification,
        )[0]

        self.assertEqual(item.verification_status, "conflicting")
        self.assertEqual(item.corroborating_source_count, 1)
        self.assertEqual(item.conflicting_source_count, 1)
        self.assertEqual(item.ambiguous_source_count, 1)

    def test_supported_projection_has_zero_derived_source_counts(self) -> None:
        claim_id = "feature:floating-design"

        item = project_claim_review_items(
            [candidate(claim_id)],
            report(result(claim_id)),
        )[0]

        self.assertEqual(item.verification_status, "supported")
        self.assertEqual(item.corroborating_source_count, 0)
        self.assertEqual(item.conflicting_source_count, 0)
        self.assertEqual(item.ambiguous_source_count, 0)

    def test_projection_deep_copies_candidates_without_mutating_inputs(self) -> None:
        original_candidate = candidate("spec:playback_time", verified=False)
        verification = report(result("spec:playback_time"))
        candidate_before = deepcopy(original_candidate)
        report_before = deepcopy(verification)

        item = project_claim_review_items(
            [original_candidate],
            verification,
        )[0]

        self.assertEqual(original_candidate, candidate_before)
        self.assertEqual(verification, report_before)
        self.assertIsNot(item.candidate, original_candidate)
        self.assertIsNot(item.candidate.evidence, original_candidate.evidence)
        self.assertEqual(
            item.candidate.evidence.extracted_at,
            FIXED_EXTRACTED_AT,
        )
        self.assertFalse(item.candidate.verified)
        item.candidate.evidence.raw_text = "Changed projection"
        self.assertEqual(
            original_candidate.evidence.raw_text,
            "Evidence for spec:playback_time",
        )

    def test_empty_candidate_collection_produces_empty_projection(self) -> None:
        self.assertEqual(
            project_claim_review_items(
                [],
                report(result("spec:playback_time")),
            ),
            [],
        )

    def test_unknown_claim_result_is_rejected_without_fuzzy_joining(self) -> None:
        with self.assertRaises(ClaimVerificationLookupError):
            project_claim_review_items(
                [candidate("spec:playback_time")],
                report(result("spec:playback-time")),
            )

    def test_duplicate_candidate_ids_are_rejected(self) -> None:
        duplicate = candidate("spec:playback_time")

        with self.assertRaises(DuplicateAdClaimCandidateError):
            project_claim_review_items(
                [duplicate, duplicate.model_copy(deep=True)],
                report(result(duplicate.id)),
            )

    def test_review_item_round_trip_is_strict(self) -> None:
        item = project_claim_review_items(
            [candidate("spec:playback_time")],
            report(result("spec:playback_time")),
        )[0]

        restored = ClaimReviewItem.model_validate_json(item.model_dump_json())

        self.assertEqual(restored, item)
        malformed = item.model_dump()
        malformed["approval_status"] = "approved"
        with self.assertRaises(ValidationError):
            ClaimReviewItem.model_validate(malformed)


if __name__ == "__main__":
    unittest.main()
