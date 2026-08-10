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
