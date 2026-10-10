import os
import time
from pathlib import Path

from google import genai
from google.genai import types

MODEL = "veo-3.1-fast-generate-preview"
INPUT_IMAGE = Path("sunnny_poolside_speaker_setup.png")
OUTPUT_PATH = Path("soundcore_shot3.mp4")
POLL_INTERVAL_SECONDS = 10

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY is not set.\n"
        "Run:\n"
        "export GEMINI_API_KEY='YOUR_API_KEY'"
    )

if not INPUT_IMAGE.exists():
    raise FileNotFoundError(
        f"Missing input image: {INPUT_IMAGE.resolve()}"
    )

client = genai.Client(api_key=api_key)

suffix = INPUT_IMAGE.suffix.lower()
mime_types = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

mime_type = mime_types.get(suffix)
if not mime_type:
    raise ValueError(
        f"Unsupported image format: {suffix}"
    )

with INPUT_IMAGE.open("rb") as f:
    start_image = types.Image(
        image_bytes=f.read(),
        mime_type=mime_type,
    )

prompt = """
Animate this image into a subtle photorealistic smartphone product video.

Preserve the speaker exactly as it appears in the input image:
same shape, proportions, colors, materials, buttons, logo, control panel,
carrying loop, and visible construction.

This is a casual poolside product lifestyle shot.

The speaker is resting near a swimming pool beside a towel and sunglasses.

Use only subtle natural movement:
- slight handheld smartphone camera motion
- gentle pool water movement in the background
- small natural lighting shimmer
- very subtle movement in the towel if visible

Do not rotate, redesign, morph, or reconstruct the product.
Do not reveal substantially new sides of the product.

Natural realistic daylight.
Authentic casual smartphone footage.
No on-screen text or captions.
"""

print(f"Model: {MODEL}")
print(f"Input image: {INPUT_IMAGE.resolve()}")
print(f"Output: {OUTPUT_PATH.resolve()}")
print("\nStarting IMAGE-TO-VIDEO diagnostic generation...")

operation = client.models.generate_videos(
    model=MODEL,
    prompt=prompt,
    image=start_image,
    config=types.GenerateVideosConfig(
        aspect_ratio="9:16",
        duration_seconds=8,
        resolution="720p",
        person_generation="allow_adult",
        number_of_videos=1,
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
    raise RuntimeError(f"Veo API error: {operation.error}")

response = operation.response

if not response or not response.generated_videos:
    reasons = (
        getattr(response, "rai_media_filtered_reasons", None)
        if response
        else None
    )
    raise RuntimeError(
        "Veo returned no generated video.\n"
        f"Filter reasons: {reasons}"
    )

video = response.generated_videos[0]

print("\nDownloading generated video...")
client.files.download(file=video.video)
video.video.save(str(OUTPUT_PATH))

print("\nDONE!")
print(f"Saved to: {OUTPUT_PATH.resolve()}")
