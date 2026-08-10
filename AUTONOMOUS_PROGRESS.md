# Autonomous Progress

## Feature 2.4 — Ad claim candidates

- Branch: `feature/primary-product-claims`
- Commit: `3d3cf61a5624e499c7fa13c6d5c081d51f5ec7a4`
- Goal: Deterministically project researched specs and features into selectable, source-linked ad claims.
- Main files: `ad_claims.py`, `tests/test_ad_claims.py`
- Tests added: 15 focused candidate-model, formatting, provenance, ordering, compatibility, and collision tests.
- Total tests: 72 passing.
- Real-world regression: The seven Soundcore candidates matched expected IDs and display text; all retained confidence `0.65`, `verified=false`, and TWS context.
- Design decision: Candidates are transient typed projections with product-relative IDs and deep-copied evidence; `Product` remains unchanged.
- Known limitation: Candidate IDs track semantic slots, not historical values; future audited campaigns may need snapshots.
- Next selected feature: Deterministic ad claim selection resolution without Campaign persistence.

## Feature 2.5 — Ad claim selection resolution

- Branch: `feature/primary-product-claims`
- Commit: `0a864059033e9de4e877ae45fef14979568a9dd8`
- Goal: Resolve selected candidate IDs safely without introducing Campaign persistence.
- Main files: `ad_claims.py`, `tests/test_ad_claims.py`
- Tests added: 7 focused selection-order, validation, collision, and mutation-isolation tests.
- Total tests: 79 passing.
- Real-world regression: A reversed Soundcore selection resolved to stable product source order while retaining TWS context, confidence, and verification state.
- Design decision: Selection resolution rejects unknown or duplicate IDs and returns deep copies in candidate source order.
- Known limitation: The selected ID list is not yet bound to a product identity.
- Next selected feature: A minimal typed product-bound claim selection, without a Campaign model or persistence.

## Feature 2.6 — Product-bound ad claim selection

- Branch: `feature/primary-product-claims`
- Commit: `3e5173c9f98ca1642e6a4ba92772ce304f9ec64d`
- Goal: Bind selected candidate IDs to one Product without creating Campaign persistence.
- Main files: `ad_claims.py`, `tests/test_ad_claims.py`
- Tests added: 6 focused model-validation, product-binding, resolution, and mutation-safety tests.
- Total tests: 85 passing.
- Real-world regression: A JSON-round-tripped Soundcore selection resolved the expected three candidates with confidence, verification state, and TWS context intact.
- Design decision: Store only `product_id` and unique candidate IDs; regenerate selected claims from current Product research truth.
- Known limitation: Selections do not snapshot historical claim wording or evidence revisions.
- Next selected feature: None autonomously; remaining roadmap options require a material research-policy or broader domain-architecture decision.

## Feature 2.7 — Product identity normalization

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Separate conservative product display names from deterministic source-scoped identity.
- Main files: `product_identity.py`, `product.py`, `product_research.py`, identity and compatibility tests, and one generic fixture.
- Tests added: 29 focused name-normalization, URL identity, variant-scope, persistence, selection-compatibility, claim-ID, and integration regressions.
- Total tests: 114 passing.
- Real-world regression: The Soundcore page produced `source_name` with its source SEO title, canonical name `Select 4 Go`, group ID `soundcore-com-group-products-a31x1021`, no arbitrary variant, and the same four specs, three features, evidence methods, and seven ad candidate IDs.
- Design decision: New research IDs use same-origin canonical or final URLs with explicit entity scope; legacy saved Product/selection pairs retain and resolve their existing IDs unchanged.
- Known limitation: IDs remain source-scoped and are not reconciled across canonical URL changes or different retailers.
- Next selected feature: Evaluate the smallest conservative source or asset discovery prerequisite without collapsing research, claim, selection, or campaign boundaries.

## Feature 2.8 — Structured product image metadata

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Populate existing `ProductImage` records from product-bound structured metadata without downloading or ranking assets.
- Main files: `product_research.py`, `tests/test_product_research.py`, and `tests/fixtures/product_images.html`.
- Tests added: 7 focused JSON-LD/Open Graph admission, metadata, stable-ID, deduplication, malformed-input, and ProductGroup sibling-isolation regressions.
- Total tests: 121 passing.
- Real-world regression: The Soundcore page yielded one product-bound 800×600 Open Graph image with source provenance and confidence while retaining its normalized identity, four specs, three features, and seven ad candidate IDs.
- Design decision: JSON-LD images are admitted only from the selected Product/ProductGroup and selected variant; Open Graph images require exact product-name corroboration in `og:image:alt`.
- Known limitation: Discovery does not scan arbitrary `<img>` elements, resolve separately referenced ImageObjects, inspect image bytes, download assets, or rank creative suitability.
- Next selected feature: Deterministic product-image ranking using only validated metadata, if a conservative ranking policy can be defined without image inspection.

## Feature 2.9 — Deterministic product image ranking

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Provide a stable metadata-only order and primary selection for researched Product images.
- Main files: `product_images.py`, `tests/test_product_images.py`.
- Tests added: 7 focused confidence, metadata-completeness, pixel-area, stable-order, empty-input, primary-selection, and mutation-isolation tests.
- Total tests: 128 passing.
- Real-world regression: The single researched Soundcore image ranks as the primary image without changing Product identity or any researched/ad claim.
- Design decision: Rank by explicit confidence, then dimension completeness, then pixel area; retain source order for ties and return deep copies.
- Known limitation: Ranking deliberately makes no aesthetic, angle, background, aspect-ratio, accessibility, or content-quality judgment.
- Next selected feature: Stop for an owner decision before image downloading or visual suitability analysis, because those require network/storage policy and creative-ranking criteria.

## Feature 2.10 — Controlled image downloading

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Download only accepted researched ProductImages and preserve verified originals with local provenance.
- Main files: `product_image_downloads.py`, `tests/test_product_image_downloads.py`, `.gitignore`.
- Tests added: 26 focused model, format, decoding, streaming-limit, HTTP, provenance, duplicate, collision, filesystem-integrity, symlink, and mutation-isolation tests.
- Total tests: 154 passing.
- Real-world regression: The one accepted Soundcore image downloaded as an actual 2000×2000 WebP (194,112 bytes) with SHA-256 `27f46a38d92121db356cf431d4e6a5f5eedb3e891ba76e81ba675b48f9b67820`; its `.png` URL and discovered 800×600 metadata remained preserved separately, and all seven ad claim IDs remained unchanged.
- Design decision: Each source ProductImage gets a strict downloaded-asset record, while identical bytes share one product-scoped `<sha256>.<decoded-extension>` original.
- Known limitation: The local downloader does not provide production SSRF defenses, license inference, batch failure aggregation, transformations, or remote/object storage.
- Next selected feature: A deterministic verified-asset manifest that persists downloaded metadata without embedding binary files, if it can remain a small local JSON boundary.

## Feature 2.11 — Product asset manifest

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Persist verified downloaded-image metadata as strict, human-readable product-scoped JSON without embedding binary assets.
- Main files: `product_asset_manifest.py`, `tests/test_product_asset_manifest.py`.
- Tests added: 9 focused schema, consistency, duplicate-source, content/path mapping, UTF-8 persistence, malformed JSON, and mutation-isolation tests.
- Total tests: 163 passing.
- Real-world regression: The verified Soundcore WebP metadata round-tripped in a one-image manifest while retaining Product, ProductImage, source URL, final URL, local path, SHA-256, and decoded properties.
- Design decision: Multiple ProductImage references may share one exact local binary, but a manifest rejects duplicate source IDs and contradictory SHA-256/local-path mappings.
- Known limitation: Manifests store current local paths and do not relocate files, rescan integrity, aggregate download failures, or embed rights assertions.
- Next selected feature: Re-verify persisted local originals against manifest metadata before downstream generation uses them.

## Feature 2.12 — Downloaded image integrity verification

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Fail fast when a persisted downloaded-image original no longer matches its manifest metadata before downstream use.
- Main files: `product_image_downloads.py`, `tests/test_product_image_downloads.py`.
- Tests added: 9 focused valid-copy, missing/non-file, symlink, byte-size, SHA-256, decoded-metadata, corrupt-image, absolute-size-limit, and mutation-isolation tests.
- Total tests: 172 passing.
- Real-world regression: The downloaded Soundcore WebP re-verified against its 194,112-byte size, SHA-256 `27f46a38d92121db356cf431d4e6a5f5eedb3e891ba76e81ba675b48f9b67820`, 2000×2000 dimensions, format, and MIME type while all four specs, three features, and seven ad claim IDs remained unchanged.
- Design decision: Verification opens one regular file without following a final symbolic link, applies the immutable 20 MB ceiling, and returns an isolated copy only after bytes and decoded properties match; it never repairs files or rewrites metadata.
- Known limitation: Verification is fail-fast for one local asset and does not batch results, relocate missing files, quarantine corrupt assets, or attest rights.
- Next selected feature: Construct a deterministic verified-image set from ranked ProductImages and a manifest without introducing aesthetic or creative suitability judgments.

## Feature 2.13 — Deterministic verified image sets

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Join current researched ProductImages to a product-bound asset manifest and produce a ranked, integrity-checked, exact-deduplicated local image set.
- Main files: `product_image_sets.py`, `tests/test_product_image_sets.py`.
- Tests added: 9 focused empty, product-binding, orphan-source, partial-download, ranking, exact-duplicate, integrity, duplicate-ID, and mutation-isolation tests.
- Total tests: 181 passing.
- Real-world regression: The Soundcore Product, its one-record manifest, and its downloaded WebP produced one verified selected original with source ID `image-a13f7a58e3b50532`, the established SHA-256 and decoded metadata, and unchanged ad claim IDs.
- Design decision: The result is an ordered list of isolated `DownloadedProductImage` records rather than another persisted model; existing metadata ranking determines source priority, every manifest record is re-verified, and only identical SHA-256 content is collapsed.
- Known limitation: The set does not impose minimum dimensions, aspect-ratio/transparency policy, aesthetic suitability, transforms, or structured multi-asset failure reporting.
- Next selected feature: Evaluate objective image eligibility metadata without deciding universal creative thresholds or aesthetic suitability.

## Feature 2.14 — Objective image eligibility evaluation

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Evaluate integrity-verified originals against explicit caller-provided minimum dimensions, pixel count, and decoded-format constraints without embedding creative defaults.
- Main files: `product_image_eligibility.py`, `tests/test_product_image_eligibility.py`.
- Tests added: 7 focused strict-requirement, no-default, stable-violation, boundary, serialization, integrity, and mutation-isolation tests.
- Total tests: 188 passing.
- Real-world regression: The Soundcore original measured 2000×2000, 4,000,000 pixels, 1.0 aspect ratio, and WebP format; both an empty requirement set and an explicit exact-boundary example passed while the established SHA-256 and ad claim IDs remained unchanged.
- Design decision: Eligibility requirements default to no constraints, integrity failures remain errors, and results report only measurable facts plus stable violation codes; the engine does not choose universal quality thresholds or aesthetic labels.
- Known limitation: The evaluator does not inspect transparency or visual content, choose requirements for a use case, aggregate a set, transform assets, or imply advertising/license suitability.
- Next selected feature: Stop for owner policy before derived reference-sheet generation, which requires choices for canvas/layout, resizing or padding, background, annotations, output format, and whether use-case eligibility thresholds should be standardized.

## Feature 2.15 — Verified product reference sheets

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Render up to six ranked, eligible, integrity-verified originals into a deterministic, product-bound technical PNG reference sheet with complete tile provenance.
- Main files: `product_reference_sheets.py`, `product_image_downloads.py`, `tests/test_product_reference_sheets.py`.
- Tests added: 19 focused grid, ordering, eligibility, contain-fit, no-upscale, alpha, background, format, deterministic-hash, provenance, mutation, integrity, collision, filesystem, policy, and compatibility tests.
- Total tests: 207 passing.
- Real-world regression: The sole accepted Soundcore WebP produced a 2048×2048 1×1 PNG with SHA-256 `7feb5befb9604920f0073181826ebc55d2741bfa6b05c3dca43518018b15450e`; its native 2000×2000 pixels were downscaled without crop to the centered 1920×1920 tile, source provenance remained linked, original bytes were unchanged, and product identity plus all seven ad claim IDs remained stable.
- Design decision: The standard policy uses 64-pixel margins, 32-pixel gaps, white RGB background, 512×512 input eligibility, fixed row-major grids, contain-only LANCZOS downscaling, no upscaling or annotations, and SHA-addressed PNG storage under product-scoped derived assets.
- Known limitation: Reference sheets are single-sheet outputs, do not persist a separate derived-asset manifest, inspect content semantics, select creative views, embed rights, or support alternate output formats.
- Next selected feature: Link generated reference-sheet metadata into a strict product-scoped derived-asset manifest without changing rendering or discovery policy.

## Feature 2.16 — Derived-asset manifest linkage

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Persist product reference-sheet metadata separately while validating every tile back to its exact original downloaded-asset identity.
- Main files: `product_derived_asset_manifest.py`, `tests/test_product_derived_asset_manifest.py`.
- Tests added: 10 focused schema, product-binding, duplicate-identity, source-ID, source-hash, source-path, revalidation, UTF-8 persistence, malformed-JSON, and mutation-isolation tests.
- Total tests: 217 passing.
- Real-world regression: The Soundcore original, 1×1 reference sheet, and derived manifest round-tripped through UTF-8 JSON; the tile retained the established ProductImage ID, original SHA-256/path linkage, reference-sheet SHA-256 `7feb5befb9604920f0073181826ebc55d2741bfa6b05c3dca43518018b15450e`, and product identity.
- Design decision: Derived metadata lives in a separate versioned manifest to avoid changing the original-asset schema or creating circular dependencies; builder/revalidation APIs compare tile source ID, SHA-256, and local path against `ProductAssetManifest`.
- Known limitation: Standalone structural loading cannot prove current source or derived-file integrity until the caller supplies the original manifest for linkage revalidation.
- Next selected feature: Add fail-fast local integrity verification for persisted reference-sheet PNGs before downstream use.

## Feature 2.17 — Reference-sheet integrity verification

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Fail fast when a persisted reference-sheet PNG no longer matches its recorded bytes or canonical decoded properties.
- Main files: `product_reference_sheets.py`, `tests/test_product_reference_sheets.py`.
- Tests added: 10 focused valid-copy, missing/non-file, symlink, byte-size, SHA-256, corrupt-decode, decoded-metadata, absolute-size-limit, legacy-compatibility, and mutation-isolation tests.
- Total tests: 227 passing.
- Real-world regression: The Soundcore reference sheet re-verified as a 906,405-byte RGB PNG with unchanged SHA-256 `7feb5befb9604920f0073181826ebc55d2741bfa6b05c3dca43518018b15450e` and original-tile provenance.
- Design decision: New sheet metadata records byte size while the field remains optional for backward compatibility; verification rejects symlinks/non-files, enforces a 20 MB ceiling, and checks size, SHA-256, full PNG decoding, dimensions, and RGB mode without repairing state.
- Known limitation: Verification is fail-fast for one local sheet and does not batch failures, relocate files, or compare rendered pixels back to source tiles.
- Next selected feature: Deterministically partition more than six eligible ranked originals into a complete ordered reference-sheet set.

## Feature 2.18 — Complete reference-sheet sets

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Cover every ranked eligible verified original exactly once by partitioning the complete set into deterministic six-image reference sheets.
- Main files: `product_reference_sheets.py`, `product_derived_asset_manifest.py`, and their focused test modules.
- Tests added: 8 focused single-sheet compatibility, 7/12/13-image chunking, pre-chunk eligibility, verification/linkage, empty-input, mutation-isolation, shared-derived-byte provenance, and duplicate-provenance tests.
- Total tests: 235 passing.
- Real-world regression: The one-image Soundcore set remained one verified 906,405-byte sheet with the established sheet SHA-256, one tile, complete derived-manifest linkage, and unchanged product identity.
- Design decision: Filtering and integrity validation happen before row-order chunking; chunks contain at most six images and reuse the Feature 2.15 renderer. Byte-identical derived sheets may share one SHA-addressed PNG while retaining distinct tile provenance, but contradictory SHA/path mappings and duplicate provenance records are rejected.
- Known limitation: Sheet-set creation is not transactional across filesystem failures and does not discover additional images, apply semantic view balancing, or prioritize sheets creatively.
- Next selected feature: Stop for owner policy before expanding product-page image discovery or multi-source research, especially allowed structured relationships, HTML image admission, source trust ordering, and cross-page fetch boundaries.

## Feature 2.19 — Structured ImageObject resolution

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Resolve only product-linked schema.org ImageObjects already present in the fetched HTML document while preserving selected Product/ProductGroup and variant boundaries.
- Main files: `product_research.py`, `product_image_downloads.py`, their focused test modules, and `tests/fixtures/product_image_objects.html`.
- Tests added: 10 focused inline/list ImageObject, same-graph and cross-script reference, URL priority, malformed/thumbnail exclusion, sibling/ambiguous-variant isolation, duplicate trust, no-extra-fetch, mutation-safety, and controlled-downloader integration tests.
- Total tests: 245 passing.
- Real-world regression: The Soundcore page retained one 800×600 Open Graph image at confidence 0.80, with no product-linked ImageObject exposed; product ID `soundcore-com-group-products-a31x1021`, canonical name `Select 4 Go`, four specs, three features, and all seven ad claim IDs remained unchanged.
- Design decision: A same-document `@id` index contains only typed ImageObjects with an explicit valid `contentUrl` or `url`; selected-entity references resolve one edge only, use `contentUrl` before `url`, receive confidence 0.90, and never trigger HTTP. Direct selected-entity images retain confidence 0.95 and win URL-level deduplication before explicit dimension completeness.
- Known limitation: Resolution does not use `thumbnailUrl` alone, follow recursive graph relationships, fetch JSON-LD links, infer image semantics or rights, or inspect arbitrary HTML images.
- Next selected feature: Constrained same-page HTML primary-product gallery discovery under the approved generic DOM-topology policy.

## Feature 2.20 — Constrained HTML product gallery discovery

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Discover same-page HTML product-gallery image metadata only from a tightly product-bound DOM region near one matched product H1, without crawling or downloading assets during research.
- Main files: `product_research.py`, `product_image_downloads.py`, their focused test modules, and `tests/fixtures/product_gallery.html`.
- Tests added: 10 focused product-bound `img`, `picture`, strict `srcset`, lazy `data-src`, malformed fallback, exclusion, CSS independence, ambiguous-H1, 24-candidate cap, source precedence, no-download, and caller-controlled download tests.
- Total tests: 255 passing.
- Real-world regression: The current Soundcore page remained at one accepted 800×600 Open Graph image because its HTML did not satisfy the conservative product-detail/gallery topology; product identity, four specs, three features, and all seven ad claim IDs remained unchanged.
- Design decision: Admission requires a unique matched H1, a close non-global common ancestor, independent product-detail and gallery/media context, and no excluded review/recommendation/promotion/navigation/badge/variant context. Strict width-descriptor `srcset` selects one largest representation; malformed sets fall back to `data-src`/`src`. HTML candidates use confidence 0.70, deduplicate behind structured and Open Graph sources, and the final page set is capped at 24 in deterministic source order.
- Known limitation: The extractor deliberately misses galleries without recognizable generic topology, ignores JavaScript/framework state and canvas content, does not interpret density descriptors, and neither infers visual suitability nor image rights.
- Next selected feature: Stop before cross-page or multi-source image research; the remaining high-value recall expansion requires an owner policy for additional page fetching and source reconciliation.

## Feature 2.21 — Explicit multi-source product research

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Research one caller-supplied primary page and up to three explicitly supplied supplementary pages independently, then reconcile product identity without merging source facts or images.
- Main files: `product_source_research.py` and `tests/test_product_source_research.py`.
- Tests added: 20 focused strict-model, exact/ambiguous/unmatched reconciliation, generic-name rejection, model-number conflict/match, SKU neutrality, model-token, group/variant scope, mutation isolation, source limit/order/roles, primary and supplementary failure (including malformed submitted URL provenance), no-merge, claim/evidence/image preservation, serialization, and explicit-fetch tests.
- Total tests: 275 passing.
- Real-world regression: Primary-only Soundcore research retained product ID `soundcore-com-group-products-a31x1021`, name `Select 4 Go`, brand `soundcore`, four specs, three features, one 800×600 Open Graph image, and all seven ad claim IDs, with no supplementary URL discovered or fetched.
- Design decision: The primary Product remains authoritative and isolated. Supplementary results contain either a complete independent Product plus `matched`/`not_matched`/`ambiguous` reconciliation or a structured failure. Exact normalized brand plus distinctive canonical-name tokens or a shared normalized model number can match; generic category names and SKUs cannot. Group-to-specific-variant matches retain `product_family` scope rather than replacing identity.
- Known limitation: Reconciliation has no manufacturer/retailer authority, GTIN field, fuzzy entity matching, retry/cache policy, automatic URL discovery, claim corroboration, conflict resolution, or cross-source asset aggregation.
- Next selected feature: Design multi-source claim corroboration and stop for owner policy on verification meaning, source sufficiency/authority, qualifier compatibility, and conflict handling.

## Feature 2.22 — Multi-source claim verification report

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Derive a strict, explanatory corroboration report for primary Product claims using only independent, successfully reconciled supplementary Products without mutating research or ad-claim data.
- Main files: `product_claim_verification.py` and `tests/test_product_claim_verification.py`.
- Tests added: 21 focused primary-only, exact spec/feature, publisher independence, source-admission, numeric/unit formatting, no-conversion/tolerance, qualifier, compound-condition, feature-context, product-family, conflict-precedence, missing/supplementary-only, evidence, mutation, ordering, serialization, malformed-state, and duplicate-identity tests.
- Total tests: 296 passing.
- Real-world regression: Primary-only Soundcore research retained product ID `soundcore-com-group-products-a31x1021`, name `Select 4 Go`, four specs, three features, the existing seven ad claim IDs, and one 800×600 Open Graph image; all seven report results were `supported`, with no supplementary source discovered or fetched.
- Design decision: Reports use categorical `supported`/`corroborated`/`conflicting`/`ambiguous` results and ordered `agrees`/`conflicts`/`ambiguous` observations. A distinct conservatively normalized publisher is required; conflicts dominate; missing claims are neutral; exact stable spec keys and feature IDs are the only joins; Product-family evidence is limited to an explicit safe group-level concept set.
- Known limitation: Publisher normalization intentionally uses a small no-dependency hostname heuristic rather than a Public Suffix List; comparison performs no unit conversion, tolerance, fuzzy synonyms, source weighting, manufacturer/retailer authority, or mutation of existing `verified` fields.
- Next selected feature: Stop for owner selection among a small report lookup/serialization/summary follow-up; automatic URL discovery and legal/marketing verification semantics remain deferred.

## Feature 2.23 — Claim verification result lookup

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Resolve one existing verification result by its `AdClaimCandidate`-compatible `spec:<key>` or `feature:<id>` identity without coupling or mutating either model.
- Main files: `product_claim_verification.py` and `tests/test_product_claim_verification.py`.
- Tests added: 3 focused spec/feature join, invalid/unknown identity, and deep-copy isolation tests.
- Total tests: 299 passing.
- Real-world regression: All seven primary-only Soundcore `AdClaimCandidate` IDs resolved one-to-one to the seven `supported` report results for unchanged product ID `soundcore-com-group-products-a31x1021`.
- Design decision: `resolve_claim_verification_result` accepts the established candidate ID string, performs exact deterministic lookup only, returns a deep copy, and raises a dedicated error for malformed or unknown IDs.
- Known limitation: Lookup is intentionally single-result and exact-ID only; it does not merge models, create bulk view models, persist reports, or add verification state to ad claims.
- Next selected feature: Stop at the YAGNI/policy boundary; serialization and summary APIs should be added only for a concrete consumer, while automatic discovery and authority policy remain deferred.

## Feature 3.0 — Official product page discovery

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Given a small typed brand/product identity request, use bounded provider-supplied navigation hints to discover and validate at most one official manufacturer product page through the existing single-page research pipeline.
- Main files: `official_product_discovery.py` and `tests/test_official_product_discovery.py`.
- Tests added: 25 focused request/result strictness, fake-provider, query/result/fetch limits, official-domain, URL exclusion, rank neutrality, exact identity/model conflict, strongest match, ambiguity, fetch/provider failure, redirect/final-domain, URL deduplication, search-metadata isolation, Product request derivation, mutation isolation, and JSON round-trip tests.
- Total tests: 324 passing.
- Real search adapter: Not implemented. The current environment has no appropriate mature zero-credential search interface, and Feature 3.0 deliberately does not scrape search-engine HTML or invent credentials. Consequently no live Soundcore discovery was claimed.
- Design decision: Search providers expose one minimal `search(query, limit=...)` Protocol and their titles/snippets remain navigation metadata only. Discovery uses at most three queries, consumes at most ten raw results per query, and runs at most five existing `research_product` calls. A candidate must pass conservative URL filtering, exact registrable brand-domain correspondence or an explicit known-domain constraint, final redirect-domain revalidation, and existing deterministic Product reconciliation. A unique shared-model match can outrank name-only matches; equal strongest pages remain ambiguous; normalized duplicate result/final URLs cannot create duplicate fetches or false ambiguity.
- Known limitation: The no-dependency registrable-domain heuristic intentionally covers only common host layouts and cannot prove corporate ownership. There is no Public Suffix List, brand-domain registry, retry, cache, persistence, CLI, real search provider, supplementary-source discovery, fuzzy matching, or automatic use of discovered retailer/editorial pages.
- Next selected feature: Design Feature 3.1 supplementary product source discovery and stop for owner decisions on retailer/marketplace/editorial admission, publisher diversity and authority, automatic fetching, and strict search/fetch budgets.

## Feature 3.1 — Supplementary product source discovery

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Given an already researched official Product and the existing search-provider boundary, propose up to three independently published direct-retailer pages that research and reconcile as the exact same product without merging facts or triggering later verification.
- Main files: `supplementary_product_discovery.py`, `product_research.py`, `product_claim_verification.py`, `tests/test_supplementary_product_discovery.py`, and `tests/test_product_research.py`.
- Tests added: 30 focused one-fetch research-result, strict-model, direct-retailer, exact identity, product-family, unmatched/ambiguous, primary-publisher, normalized publisher diversity, strongest same-publisher candidate, marketplace/multi-seller, seller mismatch, Article/Review/buying-guide, customer-review, URL/final-redirect, provider/candidate failure, query/result/fetch/proposal limit, URL deduplication, provenance, search-metadata isolation, mutation isolation, and no-automatic-follow-up tests.
- Total tests: 354 passing.
- Real-world regression: A bounded manual fetch of the established Soundcore page retained product ID `soundcore-com-group-products-a31x1021`, canonical name `Select 4 Go`, brand `soundcore`, four specs, three features, seven unchanged ad-claim IDs, one 800×600 Open Graph image at confidence 0.80, and seven primary-only `supported` verification results. No live search or supplementary source discovery was claimed.
- Design decision: Candidate pages are fetched once through the existing research pipeline while retaining their HTML for deterministic source classification. Admission requires exact-product reconciliation plus structured Product/ProductGroup data, structured Offer evidence, a price signal, an add-to-cart/buy control, no primary editorial semantics, no aggregate/multi-seller evidence, and no conflicting seller identity. Search work is capped at three queries and ten consumed results per query; page validation is capped at six fetches; output is capped at three publishers with one strongest deterministic proposal per normalized publisher. Search metadata remains navigation-only, and proposals are not passed automatically into multi-source research or claim verification.
- Known limitation: Classification intentionally favors false negatives and recognizes only explicit generic schema/HTML signals. It has no marketplace registry, retailer registry, trust score, semantic page classifier, retry/cache/persistence, real search adapter, source weighting, automatic claim verification, or supplementary image rights. The conservative publisher heuristic remains intentionally smaller than a Public Suffix List.
- Next selected feature: Consider one optional caller-driven orchestration helper that accepts an official discovery result plus an explicit subset of approved proposal URLs and invokes Feature 2.21; stop if any implicit selection, automatic acceptance, or product-choice policy would be required.

## Feature 3.2 — Explicit discovery-to-research orchestration

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Let a caller explicitly select accepted supplementary discovery proposal URLs and hand only that approved subset to the existing Feature 2.21 multi-source research function.
- Main files: `product_discovery_orchestration.py` and `tests/test_product_discovery_orchestration.py`.
- Tests added: 6 focused explicit-selection, deterministic proposal-order, unknown/duplicate/malformed selection, official discovery state, product/primary URL pairing, empty selection, mutation isolation, output isolation, Feature 2.21 delegation, and no-automatic-verification tests.
- Total tests: 360 passing.
- Design decision: The helper accepts only exact `accepted_url` values already present in the matching supplementary discovery result, rejects duplicates and cross-product/cross-primary combinations before research, caps selection through the existing three-source constant, and preserves proposal order rather than caller or search rank. An explicit empty selection runs Feature 2.21 in primary-only mode. The helper returns the existing `MultiSourceProductResearch` model and introduces no new aggregate or verification state.
- Known limitation: Selected pages are intentionally researched again through Feature 2.21 because that established API is URL-based; there is no cache or reuse of discovery-time HTML/Product objects. The helper does not persist selection, invoke claim verification, merge products, mutate `verified`, or infer approval.
- Next selected feature: Stop. Automatically feeding the resulting supplementary research into claim verification or assigning marketing/legal meaning to corroboration would cross the approved automatic-use and verification-policy boundary.

## Feature 3.3 — Selected discovery source verification

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Build an isolated Feature 2.21-compatible research bundle from explicitly selected, already researched supplementary discovery proposals and derive the existing Feature 2.22 verification report without refetching any selected page.
- Main files: `product_discovery_orchestration.py` and `tests/test_product_discovery_verification.py`.
- Tests added: 10 focused primary-only, one/two-source corroboration, conflict precedence, same-publisher, qualifier, supplementary-only exclusion, selection validation, cross-result binding, proposal/source order, requested/final URL provenance, no-refetch spies, evidence freshness, deep-copy/mutation isolation, strict serialization, unchanged verified flags, and unchanged ad-claim tests.
- Total tests: 370 passing.
- Real-world regression: No live search or supplementary page fetch was needed for this pure integration feature. The existing Soundcore demo, product research, claim, and image regression suites remain green; the pre-existing `soundcore_researched_23.json` evaluation artifact remains intentionally untracked and was not used as fresh evidence.
- Design decision: `verify_selected_discovery_sources` accepts only explicit accepted proposal URLs, resolves them in proposal order, binds every selected proposal back to exactly one accepted exact-product candidate, reuses deep-copied discovery-time Products and reconciliation, preserves discovered URL as requested provenance and the researched Product source as final provenance, and delegates all status semantics unchanged to Feature 2.22. The existing Feature 3.2 fetch-oriented API remains available and behavior-compatible.
- Known limitation: Reuse is intentionally limited to the supplied in-memory discovery result. There is no persistent cache, freshness policy, source weighting, legal/marketing approval, claim expansion, mutation of Product verification flags, or automatic participation by unselected proposals.
- Next selected feature: Add the approved small pure claim-review projection that joins existing ad-claim candidates to verification results without introducing UI, Campaign, or approval semantics.

## Feature 3.4 — Claim review projection

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Project existing `AdClaimCandidate` and `ClaimVerificationResult` data into a small deterministic review item suitable for a future consumer without assigning approval or mutating either source model.
- Main files: `claim_review.py` and `tests/test_claim_review.py`.
- Tests added: 8 focused exact-ID and candidate-order joins, supported defaults, distinct-source comparison counts, duplicate-observation handling, deep-copy/evidence freshness, empty input, duplicate/unknown claim rejection, and strict JSON round-trip tests.
- Total tests: 378 passing.
- Real-world regression: The existing Soundcore manual demo and all product research, ad-claim, verification, discovery, and image pipeline tests remain green. No live page or stale evaluation artifact was needed for this pure projection.
- Design decision: `ClaimReviewItem` contains a deep-copied candidate, the unchanged Feature 2.22 status, and counts of unique requested supplementary sources that agree, conflict, or remain ambiguous. `project_claim_review_items` performs exact established claim-ID lookup in candidate order; duplicate observations from one requested source do not inflate its count.
- Known limitation: The projection does not contain marketing/legal approval, source authority, publisher weighting, claim selection, persistence, UI state, or Product identity beyond the source candidate/report join already established by claim ID.
- Next selected feature: Stop at the owner-policy boundary. Any further meaningful work would require Campaign/Storyboard design, approval semantics, Product verification mutation, source weighting, persistent freshness/cache policy, or production search/generation infrastructure.

## Feature 4.0 — Ad campaign domain

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Persist durable user intent for a vertical social ad while binding selected product claims to their exact reviewed wording, context, and verification status.
- Main files: `campaign.py` and `tests/test_campaign.py`.
- Tests added: 12 focused model, product binding, selection resolution, complete review coverage, claim-status policy, exact ambiguous acknowledgement, snapshot identity/order/drift, duration, format/style/environment/language, persistence, strict-schema, mutation-isolation, and legacy-model compatibility tests.
- Total tests: 390 passing.
- Design decision: Campaign snapshots retain only selected user-facing claim ID, display text, optional context text, and selection-time verification status. Supported and corroborated claims are allowed, ambiguous claims require exact explicit acknowledgement, and conflicting claims are rejected. Context validation requires current Product, candidate, and complete review data and returns a deep copy without mutating upstream domain objects.
- Known limitation: Campaign v1 supports only `vertical_social` at `9:16`, caller-supplied IDs, a conservative BCP-47-shaped language tag, and JSON-file persistence. It contains no product copy, evidence, images, assets, provider settings, prompts, generation state, approval semantics, or timestamps.
- Next selected feature: Feature 4.1, the approved core Storyboard domain bound to Campaign with ordered shots, selected-claim subsets, and timeline-duration validation.

## Feature 4.1 — Storyboard core domain

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Represent an ordered, Campaign-bound shot timeline with explicit claim assignment and a target-duration tolerance, without adding text, media, or provider concerns.
- Main files: `storyboard.py` and `tests/test_storyboard.py`.
- Tests added: 11 focused valid-model, Campaign binding, ordered shots/claims, required and unique shots, purpose/text fields, zero/one/multiple claim assignment, Campaign claim subset, strict finite positive durations, exact/inclusive/outside duration tolerance, persistence, strict-schema, compatibility, and mutation-isolation tests.
- Total tests: 401 passing.
- Design decision: Shot-list position is authoritative order. Each shot may use any unique ordered subset of Campaign-selected claims, and Campaign claims may remain unused. Context validation rechecks the Campaign ID, non-empty/unique shots, claim subset, safe durations, and a total within ±0.5 seconds of the Campaign target before returning an isolated copy.
- Known limitation: Storyboard core has no overlays, Product data, claim wording, asset references, prompts, provider state, transitions, editing handles, or per-element timing. Duration is intended timeline time only.
- Next selected feature: Feature 4.2, the approved typed text overlays with claim-ID-only factual overlays and backward-compatible empty overlay lists.

## Feature 4.2 — Storyboard text overlays

- Branch: `feature/primary-product-claims`
- Commit: this commit
- Goal: Add typed ordered text intent to Storyboard shots while preventing factual claim wording from being rewritten or copied outside Campaign snapshots.
- Main files: `storyboard.py`, `demo_campaign_storyboard.py`, and `tests/test_storyboard_overlays.py`.
- Tests added: 10 focused creative, CTA, product-name, and claim overlay shape; same-shot/Campaign claim binding; order/cardinality; zero-overlay/backward compatibility; claim-wording isolation; snapshot/mutation safety; strict JSON; unchecked-copy bypass; and approved Soundcore domain-demo tests.
- Total tests: 411 passing.
- Soundcore domain demo: Built an in-memory 8-second `ugc` Campaign for `soundcore-com-group-products-a31x1021` with the approved water-resistance, playback-time, and floating-design claim IDs, plus ordered 2.5/2.5/3.0-second poolside shots and the approved creative, claim-ID, and product-name overlays. No external call or media persistence occurs.
- Design decision: Claim overlays contain only a claim ID that must be both Campaign-selected and assigned to the same shot; factual wording/context remains solely in the Campaign snapshot. Creative and CTA overlays require free-form text, while product-name overlays carry neither text nor claim ID. A default empty overlay list preserves Feature 4.1 JSON compatibility.
- Known limitation: Overlays have shot-level order only, with no timing, styling, localization, rendering, Product-name resolution, snapshot resolution, legal/marketing approval, or NLP policing of free-form creative copy.
- Next selected feature: Stop at the mandatory owner-review boundary before any Feature 4.3, asset selection, HeroFrameRequest, AI/prompt/media generation, voice-over, editing, or FFmpeg work.
