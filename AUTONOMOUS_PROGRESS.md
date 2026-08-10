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
