# First controlled Soundcore production run

This runbook records the owner-approved operating policy for the local project
`soundcore-select-4-go-first-run`. Runtime documents and media live under the
ignored `projects/` workspace and are not source-controlled.

## Approved creative context

- Product: Soundcore Select 4 Go
- Official source: `https://www.soundcore.com/products/a31x1021`
- Product identity: `soundcore-com-group-products-a31x1021`
- Campaign: `soundcore-select-4-go-summer`
- Storyboard: `soundcore-select-4-go-summer-board`
- Format: vertical social, 9:16, 8 seconds, UGC
- Environment: sunny outdoor poolside setting in summer
- Language: `en`
- Reviewer actor for future manual reviews: `owner:omer-portnoy`

The selected factual claims are:

- `spec:water_resistance` — `IP67 water resistance`
- `spec:playback_time` — `Up to 20 hours playback`
- `feature:floating-design` — `Floating design`

The three stable shots are:

| Shot | Edit duration | Selected claims | Overlay intent |
| --- | ---: | --- | --- |
| `hook` | 2.5 s | none | `SMALL SPEAKER. BIG ENERGY.` |
| `water-feature` | 2.5 s | water resistance; floating design | `MADE FOR SUMMER.` |
| `poolside-lifestyle` | 3.0 s | playback time | claim text; `TAKE IT ANYWHERE.`; Product name |

## Non-paid preparation completed

The project was initialized and researched afresh from the exact official URL.
The current Product has the expected identity, seven primary-only supported
claim candidates, and exactly one accepted product-bound image candidate.

The explicit image was downloaded and integrity-checked as a 2000×2000 WebP.
A one-image 2048×2048 PNG reference sheet was prepared and verified. All three
shots use that original as primary and that sheet as their supporting reference.
The Campaign, Storyboard, original and derived manifests, editing-audio
manifest, and StoryboardAssetPlan are attached to revision 7 of the run.

The existing local `music.mp3` passed ingestion as a ten-second stereo MP3 and
is attached through the content-addressed editing-audio manifest. This is only
technical POC authorization; the engine does not infer licensing or commercial
rights. No SFX are selected.

Exact eight-second ShotVideoIntent documents are prepared for all three shots
with empty `motion_relevant_claim_ids`. They are intentionally not attached yet:
the reviewed run lifecycle requires an approved Hero review before a Video
intent can be attached, and attaching a new Hero artifact invalidates any older
Video intent reference. Attach each prepared intent only after that shot's Hero
is manually approved.

## Budget and attempt policy

- Total owner-approved paid budget: USD 4.00
- Hero ceiling: USD 0.20 per preview, at most one initial attempt per shot
- Video ceiling: USD 1.00 per preview, at most one initial attempt per shot
- Maximum initial paid attempts: three Hero plus three Video
- Automatic retries and provider fallback: forbidden

The ceilings are owner acknowledgements. When the provider cannot quote cost,
they do not guarantee or technically enforce final billing. Track the aggregate
USD 4.00 ceiling outside the per-attempt preview documents.

## Resume at the first paid boundary

First inspect the unchanged project:

```sh
.venv312/bin/python ad_production_cli.py status soundcore-select-4-go-first-run
.venv312/bin/python ad_production_cli.py validate soundcore-select-4-go-first-run
.venv312/bin/python ad_production_cli.py next-actions soundcore-select-4-go-first-run
```

For exactly one shot at a time, create a short-lived Hero preview only when the
owner is present to execute it:

```sh
.venv312/bin/python ad_production_cli.py preview-generate-hero \
  soundcore-select-4-go-first-run <shot-id> \
  --provider google-gemini \
  --model gemini-3.1-flash-image \
  --cost-quote-status unavailable \
  --owner-cost-ceiling-usd 0.20 \
  --allow-unquoted-cost
```

Review the preview bindings, then use its exact full token once:

```sh
.venv312/bin/python ad_production_cli.py generate-hero \
  soundcore-select-4-go-first-run <shot-id> \
  --provider google-gemini \
  --model gemini-3.1-flash-image \
  --confirm-preview 'action-preview:<sha256>' \
  --attempt-id '<unique-hero-attempt-id>'
```

Do not retry automatically. Manually inspect the generated Hero, author a
strict review JSON using actor `owner:omer-portnoy`, and append it:

```sh
.venv312/bin/python ad_production_cli.py record-hero-review \
  soundcore-select-4-go-first-run <shot-id> \
  --review-file <workspace-relative-review-input.json> \
  --attempt-id '<unique-hero-review-attempt-id>'
```

After an approved Hero review, attach the corresponding prepared motion intent:

```sh
.venv312/bin/python ad_production_cli.py attach-shot-video-intent \
  soundcore-select-4-go-first-run <shot-id> \
  projects/soundcore-select-4-go-first-run/documents/shot-video-intent-<shot-id>.json
```

Then create and, only after inspecting it, execute one Video preview:

```sh
.venv312/bin/python ad_production_cli.py preview-generate-video \
  soundcore-select-4-go-first-run <shot-id> \
  --provider google-gemini-api \
  --model veo-3.1-fast-generate-preview \
  --cost-quote-status unavailable \
  --owner-cost-ceiling-usd 1.00 \
  --allow-unquoted-cost

.venv312/bin/python ad_production_cli.py generate-video \
  soundcore-select-4-go-first-run <shot-id> \
  --provider google-gemini-api \
  --model veo-3.1-fast-generate-preview \
  --confirm-preview 'action-preview:<sha256>' \
  --attempt-id '<unique-video-attempt-id>'
```

The raw requested Video duration is eight seconds per shot; this is separate
from the 2.5/2.5/3.0-second edit durations. Provider-generated audio must be
discarded before editing. Only the verified local music asset may be selected
for the final edit, and no SFX are required.

Every Video requires a manual review and an explicitly approved usable range.
After all shots are approved, author and build the EditingPlan, create and
confirm a local render preview, inspect the exact final artifact, and record a
manual Final Ad review. A deliverable is not complete or authorized without the
current exact final approval and all transitive Hero/Video review authority.

No cleanup is automatic. Preserve successful, rejected, failed, and unattached
artifacts; attempt receipts; review histories; and durable domain documents.

## Non-paid readiness audit

The revision-7 project was reloaded through the status resolver and every
currently attached JSON reference was content-checked. The original WebP,
reference-sheet PNG, and editing-music MP3 were reread, decoded, and verified
against their stored sizes, hashes, dimensions, formats, and Product binding.
No Hero artifact, Hero review, Video intent reference, Video artifact, Video
review, EditingPlan, edited artifact, Final review, or paid ActionPreview exists
in the real run.

All three prepared Video intent documents were separately revalidated against
the current Product, Campaign, Storyboard, and AssetPlan. Each retains an
eight-second requested generation duration and no motion-relevant claim IDs.

Every in-memory Hero request passed current-asset reads, instruction
compilation, and the Gemini adapter capability preflight with two mandatory
references against capacity ten and fixed 9:16 / 1K / PNG output:

| Shot | Hero request fingerprint |
| --- | --- |
| `hook` | `33b7eb013e2b463140fc7413f68bab43e36b67b7cc75664a1e8b2f5a297299b3` |
| `water-feature` | `42dbef6946c6fada2ba47b013c163b48ca259926b0f8a8d7e1f08f9e64ceed2c` |
| `poolside-lifestyle` | `2c724e0e6fda4f16597bfef95afdc6fd8190c595b4a54a485a00379cfe30f171` |

A separate temporary three-shot fixture project exercised ActionPreview
creation and exact one-time confirmation, fake Hero generation, Hero review,
fake Video generation, Video review with usable ranges, EditingPlan creation,
a real local FFmpeg render, Final review, and verified deliverable handoff. It
reached derived `complete` status, made zero live provider calls, and its
temporary workspace was removed after validation.
