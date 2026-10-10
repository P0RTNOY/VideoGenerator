#!/usr/bin/env python3
"""
build_soundcore_ad.py

Build a short vertical spec ad from three Veo clips.

Expected files in the same folder:
  soundcore_shot1.mp4
  soundcore_shot2.mp4
  soundcore_shot3.mp4

Output:
  soundcore_final_ad_v1.mp4

Dependencies:
  brew install ffmpeg
  pip install pillow
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


W, H = 720, 1280
FPS = 24

SHOT1 = Path("soundcore_shot1.mp4")
SHOT2 = Path("soundcore_shot2.mp4")
SHOT3 = Path("soundcore_shot3.mp4")

OUTPUT = Path("soundcore_final_ad_v1.mp4")

# (input, start time, duration)
CUTS = [
    (SHOT1, 0.0, 2.8),
    (SHOT2, 0.3, 2.4),
    (SHOT3, 0.2, 3.0),
]

# Text timing is relative to the final concatenated video.
TEXTS = [
    ("SMALL SPEAKER.\nBIG ENERGY.", 0.20, 2.50, 135),
    ("MADE FOR SUMMER.", 3.05, 4.85, 150),
    ("POOL. BAG. GO.", 5.55, 6.95, 150),
    ("SOUNDCORE SELECT 4 GO", 7.00, 8.10, 150),
]


def run(cmd: list[str]) -> None:
    print("\n$", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def find_font() -> str:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    raise FileNotFoundError(
        "Could not find a suitable macOS font. "
        "Edit find_font() and point it at a .ttf/.ttc font on your Mac."
    )


def make_text_overlay(text: str, out_path: Path, y: int) -> None:
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    font_path = find_font()
    font = ImageFont.truetype(font_path, 54)

    # Measure multiline text.
    bbox = draw.multiline_textbbox(
        (0, 0),
        text,
        font=font,
        spacing=8,
        align="center",
        stroke_width=1,
    )
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad_x, pad_y = 28, 20
    box_w = tw + pad_x * 2
    box_h = th + pad_y * 2

    x0 = (W - box_w) // 2
    y0 = y
    x1 = x0 + box_w
    y1 = y0 + box_h

    draw.rounded_rectangle(
        (x0, y0, x1, y1),
        radius=24,
        fill=(0, 0, 0, 145),
    )

    draw.multiline_text(
        (W // 2, y0 + pad_y),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="ma",
        align="center",
        spacing=8,
        stroke_width=1,
        stroke_fill=(0, 0, 0, 80),
    )

    canvas.save(out_path)


def ensure_inputs() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg is not installed. On macOS run: brew install ffmpeg"
        )

    missing = [str(p) for p, _, _ in CUTS if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing video file(s):\n  - " + "\n  - ".join(missing)
        )


def main() -> None:
    ensure_inputs()

    with tempfile.TemporaryDirectory(prefix="soundcore_ad_") as tmp_name:
        tmp = Path(tmp_name)
        normalized = []

        # 1) Trim + normalize all three shots.
        for i, (src, start, duration) in enumerate(CUTS, start=1):
            out = tmp / f"segment_{i}.mp4"
            normalized.append(out)

            run([
                "ffmpeg", "-y",
                "-ss", str(start),
                "-t", str(duration),
                "-i", str(src),
                "-an",
                "-vf",
                (
                    f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},fps={FPS},format=yuv420p"
                ),
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "18",
                "-movflags", "+faststart",
                str(out),
            ])

        # 2) Concatenate them.
        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in normalized),
            encoding="utf-8",
        )

        base_video = tmp / "base.mp4"
        run([
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(base_video),
        ])

        # 3) Generate transparent text cards.
        overlays = []
        for i, (text, start, end, y) in enumerate(TEXTS, start=1):
            overlay = tmp / f"text_{i}.png"
            make_text_overlay(text, overlay, y)
            overlays.append((overlay, start, end))

        # 4) Overlay the timed cards.
        cmd = ["ffmpeg", "-y", "-i", str(base_video)]

        for overlay, _, _ in overlays:
            cmd += ["-loop", "1", "-i", str(overlay)]

        filter_parts = []
        previous = "[0:v]"

        for i, (_, start, end) in enumerate(overlays, start=1):
            output_label = f"[v{i}]"
            filter_parts.append(
                f"{previous}[{i}:v]overlay=0:0:"
                f"enable='between(t,{start},{end})'{output_label}"
            )
            previous = output_label

        cmd += [
            "-filter_complex", ";".join(filter_parts),
            "-map", previous,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            "-movflags", "+faststart",
            "-t", "8.2",
            str(OUTPUT),
        ]

        run(cmd)

    print("\n============================================================")
    print("DONE")
    print("============================================================")
    print(f"Created: {OUTPUT.resolve()}")
    print("\nThis V1 is intentionally silent.")
    print("Next step: add music and/or voice-over once the visual cut feels right.")


if __name__ == "__main__":
    main()
