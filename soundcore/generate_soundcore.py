import os
import time

from google import genai
from google.genai import types


client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"]
)


def load_image(path: str) -> types.Image:
    with open(path, "rb") as f:
        return types.Image(
            image_bytes=f.read(),
            mime_type="image/jpeg",
        )


references = [
    # Primary reference:
    # use the strongest 3/4 view of the real speaker here
    types.VideoGenerationReferenceImage(
        image=load_image("front.jpg"),
        reference_type="asset",
    ),

    # Multi-view sheet of the same physical speaker
    types.VideoGenerationReferenceImage(
        image=load_image("product_reference_sheet.jpg"),
        reference_type="asset",
    ),

    # Secondary angle showing where the yellow panel ends
    types.VideoGenerationReferenceImage(
        image=load_image("side.jpg"),
        reference_type="asset",
    ),
]


prompt = """
Create an 8-second photorealistic vertical UGC smartphone video featuring
the EXACT portable Bluetooth speaker shown in the reference images.

PRODUCT IDENTITY IS THE HIGHEST PRIORITY.

All reference images show the SAME physical speaker.
One reference is a multi-view sheet showing that same speaker from
multiple angles.

The primary reference image defines the viewing angle that should be
maintained throughout the shot.

The speaker has:
- a dark green woven fabric body
- a localized yellow control panel
- yellow physical controls
- a yellow carrying loop
- a small yellow logo
- the exact proportions and construction shown in the references

CRITICAL PRODUCT CONSTRAINT:

The yellow control panel is ONLY the localized panel visible in the
reference photographs.

It must NEVER:
- extend
- stretch
- grow
- wrap around the speaker
- move to another surface
- become a continuous yellow border

Preserve the exact:
- control panel dimensions
- button positions
- logo position
- carrying loop
- body shape
- proportions
- material transitions

Do not redesign any part of the product.
Do not invent additional buttons, ports, seams, logos, lights or controls.
Do not move existing controls.
Do not change the shape or proportions of the speaker.

SHOT:

An adult casually holds this exact speaker beside a sunny outdoor
swimming pool.

IMPORTANT:
Keep the speaker at approximately the SAME three-quarter orientation
as the primary reference image throughout the entire video.

The hand may make small natural movements.

The camera may move slightly closer or farther away.

However:

DO NOT rotate the speaker.
DO NOT orbit around the speaker.
DO NOT reveal substantially new sides.
DO NOT change the product viewing angle enough to require reconstruction
of unseen geometry.

The product should appear almost like the primary reference photograph
naturally coming to life.

CAMERA:

Authentic handheld smartphone UGC footage.
Very subtle handheld motion.
Small natural forward/backward movement.
No orbit.
No dramatic pan.
No dramatic zoom.

ENVIRONMENT:

Sunny modern outdoor swimming pool.
Natural blue moving water in the background.
Realistic summer daylight.
Natural shadows.
Casual summer atmosphere.

STYLE:

Photorealistic.
Authentic TikTok / Instagram UGC.
Modern smartphone footage.
Natural exposure.
Not cinematic.
Not CGI.
Not a polished studio commercial.

No text.
No captions.
No dialogue.
No additional products.
"""


print("Starting Veo 3.1 Fast generation...")

operation = client.models.generate_videos(
    model="veo-3.1-fast-generate-preview",
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


while not operation.done:
    print("Still generating...")
    time.sleep(10)
    operation = client.operations.get(operation)


print("\n=== OPERATION FINISHED ===")
print("Done:", operation.done)
print("Error:", operation.error)
print("Response:", operation.response)
print("Metadata:", operation.metadata)


if operation.error:
    raise RuntimeError(
        f"Veo generation failed: {operation.error}"
    )


if operation.response is None:
    raise RuntimeError(
        "Veo finished but returned no video."
    )


if not operation.response.generated_videos:
    raise RuntimeError(
        f"Veo returned no generated videos: {operation.response}"
    )


video = operation.response.generated_videos[0]

client.files.download(file=video.video)

output_path = "soundcore_ugc_v3.mp4"
video.video.save(output_path)

print("\nDONE!")
print(f"Saved to {output_path}")