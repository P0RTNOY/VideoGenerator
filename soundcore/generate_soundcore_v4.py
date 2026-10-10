import os
import time
from pathlib import Path

from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================

MODEL = "veo-3.1-fast-generate-preview"

REFERENCE_FILES = [
    "primary.jpg",
    "product_reference_sheet.jpg",
    "back.jpg",
]

OUTPUT_PATH = "soundcore_ugc_v4.mp4"

MAX_ATTEMPTS = 3
POLL_INTERVAL_SECONDS = 10


# ============================================================
# CLIENT
# ============================================================

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY is not defined.\n"
        "Run:\n"
        "export GEMINI_API_KEY='YOUR_API_KEY'"
    )

client = genai.Client(api_key=api_key)


# ============================================================
# LOAD REFERENCES
# ============================================================

def load_reference(path: str) -> types.VideoGenerationReferenceImage:
    file_path = Path(path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Reference image not found: {file_path.resolve()}"
        )

    # Google Gen AI SDK detects MIME type automatically.
    image = types.Image.from_file(
        location=str(file_path)
    )

    return types.VideoGenerationReferenceImage(
        image=image,
        reference_type="asset",
    )


print("\nReference images:")

for filename in REFERENCE_FILES:
    print(f"  - {Path(filename).resolve()}")


references = [
    load_reference(filename)
    for filename in REFERENCE_FILES
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
# GENERATE
# ============================================================

video = None

last_filter_reasons = None


for attempt in range(1, MAX_ATTEMPTS + 1):

    print(
        f"\n============================================================"
        f"\nVeo generation attempt {attempt}/{MAX_ATTEMPTS}"
        f"\n============================================================"
    )

    operation = client.models.generate_videos(
        model=MODEL,
        prompt=prompt,
        config=types.GenerateVideosConfig(
            reference_images=references,
            aspect_ratio="9:16",
            duration_seconds=8,
            resolution="720p",
            person_generation="allow_adult",
            number_of_videos=1,
        ),
    )

    # --------------------------------------------------------
    # POLL
    # --------------------------------------------------------

    while not operation.done:
        print("Still generating...")

        time.sleep(POLL_INTERVAL_SECONDS)

        operation = client.operations.get(operation)


    # --------------------------------------------------------
    # INSPECT RESULT
    # --------------------------------------------------------

    print("\n=== OPERATION FINISHED ===")
    print("Done:", operation.done)
    print("Error:", operation.error)
    print("Response:", operation.response)
    print("Metadata:", operation.metadata)


    # API-level failure
    if operation.error:
        raise RuntimeError(
            f"Veo generation failed:\n{operation.error}"
        )


    response = operation.response


    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    if (
        response is not None
        and response.generated_videos
        and len(response.generated_videos) > 0
    ):
        video = response.generated_videos[0]

        print("\nVideo successfully generated.")

        break


    # --------------------------------------------------------
    # FILTER / PROCESSING FAILURE
    # --------------------------------------------------------

    if response is not None:
        last_filter_reasons = getattr(
            response,
            "rai_media_filtered_reasons",
            None,
        )

        filtered_count = getattr(
            response,
            "rai_media_filtered_count",
            None,
        )

        print("\nNo video returned.")
        print("Filtered count:", filtered_count)
        print("Filter reasons:", last_filter_reasons)

    else:
        print("\nVeo finished but returned no response.")


    # --------------------------------------------------------
    # RETRY
    # --------------------------------------------------------

    if attempt < MAX_ATTEMPTS:

        print(
            "\nGeneration did not return a video."
            "\nRetrying automatically..."
        )

        time.sleep(3)


# ============================================================
# FINAL FAILURE CHECK
# ============================================================

if video is None:

    raise RuntimeError(
        "\nVeo failed to generate a video after "
        f"{MAX_ATTEMPTS} attempts.\n"
        f"Last filter reasons: {last_filter_reasons}"
    )


# ============================================================
# DOWNLOAD
# ============================================================

print("\nDownloading generated video...")

client.files.download(
    file=video.video
)


output_path = Path(OUTPUT_PATH)

video.video.save(
    str(output_path)
)


print(
    "\n============================================================"
    "\nDONE!"
    "\n============================================================"
)

print(
    f"Saved video to:\n"
    f"{output_path.resolve()}"
)