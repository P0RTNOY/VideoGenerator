import os
import random
import time
from pathlib import Path

from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================

REFERENCE_FILES = [
    "primary.jpg",
    "product_reference_sheet.jpg",
    "back.jpg",
]

OUTPUT_PATH = Path("soundcore_ugc_v5.mp4")

MODELS = [
    "veo-3.1-fast-generate-preview",
    "veo-3.1-fast-generate-preview",
    "veo-3.1-generate-preview",
]

POLL_INTERVAL_SECONDS = 10


# ============================================================
# CLIENT
# ============================================================

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY is not set.\n"
        "Run:\n"
        "export GEMINI_API_KEY='YOUR_API_KEY'"
    )

client = genai.Client(api_key=api_key)


# ============================================================
# IMAGE LOADING
# ============================================================

def load_image(path: str) -> types.Image:
    file_path = Path(path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Missing reference image: {file_path.resolve()}"
        )

    suffix = file_path.suffix.lower()

    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }

    mime_type = mime_types.get(suffix)

    if not mime_type:
        raise ValueError(
            f"Unsupported image format for {file_path.name}: {suffix}"
        )

    with file_path.open("rb") as f:
        return types.Image(
            image_bytes=f.read(),
            mime_type=mime_type,
        )


references = [
    types.VideoGenerationReferenceImage(
        image=load_image(path),
        reference_type="asset",
    )
    for path in REFERENCE_FILES
]


# ============================================================
# PROMPT
# ============================================================

prompt = """
Create a photorealistic 8-second vertical smartphone UGC video featuring
the exact portable Bluetooth speaker shown in the supplied references.

All reference images show the SAME physical speaker.
The multi-view sheet shows that same speaker from several angles.

PRODUCT ACCURACY IS THE HIGHEST PRIORITY.

Preserve exactly:
- dark green woven fabric body
- localized yellow control panel
- yellow physical buttons
- yellow braided carrying loop
- yellow logo
- exact body proportions
- exact panel boundaries
- exact button positions
- exact material transitions

CRITICAL:
The yellow control panel is a short localized section only.
It must remain the same size and shape throughout the shot.
It must not extend or wrap around the speaker.

Do not redesign or add any product details.

SCENE:
An adult casually holds the exact speaker beside a bright modern
outdoor swimming pool.

Maintain approximately the same three-quarter product angle shown
in the primary reference image throughout the entire video.

The hand makes small natural movements.
The speaker moves gently with the hand but does not rotate.
The camera has subtle natural handheld smartphone movement.

Do not orbit around the product or reveal substantially new sides.

Natural summer daylight, realistic pool water and natural shadows.

Authentic TikTok / Instagram UGC smartphone footage.
Photorealistic, casual and natural rather than cinematic.

No on-screen text or captions.
"""


# ============================================================
# PRE-FLIGHT
# ============================================================

print("\nReference images:")
for path in REFERENCE_FILES:
    print(f"  - {Path(path).resolve()}")

print("\nOutput:")
print(f"  - {OUTPUT_PATH.resolve()}")


# ============================================================
# GENERATION
# ============================================================

video = None
last_filter_reasons = None

for attempt, model in enumerate(MODELS, start=1):
    seed = random.randint(1, 2_000_000_000)

    print(
        "\n============================================================"
        f"\nAttempt {attempt}/{len(MODELS)}"
        f"\nModel: {model}"
        f"\nSeed: {seed}"
        "\n============================================================"
    )

    operation = client.models.generate_videos(
        model=model,
        prompt=prompt,
        config=types.GenerateVideosConfig(
            reference_images=references,
            aspect_ratio="9:16",
            duration_seconds=8,
            resolution="720p",
            person_generation="allow_adult",
            number_of_videos=1,
            seed=seed,
        ),
    )

    while not operation.done:
        print("Still generating...")
        time.sleep(POLL_INTERVAL_SECONDS)
        operation = client.operations.get(operation)

    print("\n=== OPERATION FINISHED ===")
    print("Done:", operation.done)
    print("Error:", operation.error)
    print("Response:", operation.response)
    print("Metadata:", operation.metadata)

    if operation.error:
        raise RuntimeError(
            f"Veo API error: {operation.error}"
        )

    response = operation.response

    if response and response.generated_videos:
        video = response.generated_videos[0]
        print("\nGeneration successful.")
        break

    if response:
        last_filter_reasons = getattr(
            response,
            "rai_media_filtered_reasons",
            None,
        )

    print("\nNo video returned.")
    print("Reason:", last_filter_reasons)

    if attempt < len(MODELS):
        print("Trying another generation configuration...")


# ============================================================
# FINAL CHECK
# ============================================================

if video is None:
    raise RuntimeError(
        "All Veo attempts were filtered or returned no video.\n"
        f"Last reason: {last_filter_reasons}"
    )


# ============================================================
# DOWNLOAD
# ============================================================

print("\nDownloading generated video...")

client.files.download(file=video.video)
video.video.save(str(OUTPUT_PATH))

print("\n============================================================")
print("DONE!")
print("============================================================")
print(f"Saved to: {OUTPUT_PATH.resolve()}")
