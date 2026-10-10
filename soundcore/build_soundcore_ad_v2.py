#!/usr/bin/env python3
"""
build_soundcore_ad_v2.py

Polished V2 builder for the Soundcore spec ad.

Expected files in the same folder:
  soundcore_shot1.mp4
  soundcore_shot2.mp4
  soundcore_shot3.mp4

Optional:
  music.mp3

Output:
  soundcore_final_ad_v2.mp4

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


# ============================================================
# CONFIG
# ============================================================

W, H = 720, 1280
FPS = 24

SHOT1 = Path("soundcore_shot1.mp4")
SHOT2 = Path("soundcore_shot2.mp4")
SHOT3 = Path("soundcore_shot3.mp4")

MUSIC = Path("music.mp3")  # optional
OUTPUT = Path("soundcore_final_ad_v2.mp4")

# (input, start_time, duration)
CUTS = [
    (SHOT1, 0.00, 2.70),
    (SHOT2, 0.25, 2.40),
    (SHOT3, 0.15, 3.10),
]

# Final timeline:
# 0.00–2.70  Shot 1
# 2.70–5.10  Shot 2
# 5.10–8.20  Shot 3

AD_DURATION = 8.20

# Text card definitions:
# text, start, end, y-position, font-size, accent
TEXTS = [
    ("SMALL SPEAKER.\nBIG ENERGY.", 0.18, 2.30, 135, 58, True),
    ("MADE FOR SUMMER.", 2.95, 4.75, 150, 54, False),
    ("TAKE IT ANYWHERE.", 5.35, 6.85, 150, 52, False),
    ("SOUNDCORE\nSELECT 4 GO", 7.00, 8.18, 130, 50, True),
]

YELLOW = (255, 196, 0, 255)
WHITE = (255, 255, 255, 255)


# ============================================================
# HELPERS
# ============================================================

def run(cmd: list[str]) -> None:
    print("\n$", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def find_font() -> str:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
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


def ensure_inputs() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg is not installed.\n"
            "Install it with:\n"
            "brew install ffmpeg"
        )

    missing = [str(p) for p, _, _ in CUTS if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing video file(s):\n  - " + "\n  - ".join(missing)
        )


def make_text_overlay(
    text: str,
    out_path: Path,
    y: int,
    font_size: int,
    accent: bool,
) -> None:
    """
    Creates a transparent overlay with:
      - bold white text
      - subtle black shadow
      - optional yellow accent line
      - no heavy black text box
    """
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    font_path = find_font()
    font = ImageFont.truetype(font_path, font_size)

    spacing = 8

    bbox = draw.multiline_textbbox(
        (0, 0),
        text,
        font=font,
        spacing=spacing,
        align="center",
        stroke_width=0,
    )

    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    x = W // 2

    # Soft shadow behind text.
    shadow_offset = 4
    draw.multiline_text(
        (x + shadow_offset, y + shadow_offset),
        text,
        font=font,
        fill=(0, 0, 0, 150),
        anchor="ma",
        align="center",
        spacing=spacing,
    )

    # Main white text.
    draw.multiline_text(
        (x, y),
        text,
        font=font,
        fill=WHITE,
        anchor="ma",
        align="center",
        spacing=spacing,
    )

    # Optional yellow accent line.
    if accent:
        line_w = min(max(int(tw * 0.55), 120), 300)
        line_y = y + th + 18
        draw.rounded_rectangle(
            (
                x - line_w // 2,
                line_y,
                x + line_w // 2,
                line_y + 8,
            ),
            radius=4,
            fill=YELLOW,
        )

    canvas.save(out_path)


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    ensure_inputs()

    with tempfile.TemporaryDirectory(prefix="soundcore_ad_v2_") as tmp_name:
        tmp = Path(tmp_name)
        segments: list[Path] = []

        # ----------------------------------------------------
        # 1) Trim + normalize each shot
        # ----------------------------------------------------

        for i, (src, start, duration) in enumerate(CUTS, start=1):
            out = tmp / f"segment_{i}.mp4"
            segments.append(out)

            run([
                "ffmpeg", "-y",
                "-ss", str(start),
                "-t", str(duration),
                "-i", str(src),

                # We intentionally remove Veo audio here so every shot
                # has a consistent sound bed later.
                "-an",

                "-vf",
                (
                    f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},"
                    f"fps={FPS},"
                    f"format=yuv420p"
                ),

                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "18",
                "-movflags", "+faststart",
                str(out),
            ])

        # ----------------------------------------------------
        # 2) Concatenate the three shots
        # ----------------------------------------------------

        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(
                f"file '{segment.as_posix()}'"
                for segment in segments
            ),
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

        # ----------------------------------------------------
        # 3) Create cleaner typography overlays
        # ----------------------------------------------------

        overlays = []

        for i, (
            text,
            start,
            end,
            y,
            font_size,
            accent,
        ) in enumerate(TEXTS, start=1):

            overlay = tmp / f"text_{i}.png"

            make_text_overlay(
                text=text,
                out_path=overlay,
                y=y,
                font_size=font_size,
                accent=accent,
            )

            overlays.append(
                (overlay, start, end)
            )

        # ----------------------------------------------------
        # 4) Overlay timed typography
        # ----------------------------------------------------

        text_video = tmp / "text_video.mp4"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(base_video),
        ]

        for overlay, _, _ in overlays:
            cmd += [
                "-loop", "1",
                "-i", str(overlay),
            ]

        filter_parts = []
        previous = "[0:v]"

        for i, (_, start, end) in enumerate(overlays, start=1):
            output_label = f"[v{i}]"

            filter_parts.append(
                f"{previous}[{i}:v]"
                f"overlay=0:0:"
                f"enable='between(t,{start},{end})'"
                f"{output_label}"
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
            "-t", str(AD_DURATION),
            str(text_video),
        ]

        run(cmd)

        # ----------------------------------------------------
        # 5) Optional music
        # ----------------------------------------------------

        if MUSIC.exists():
            print(f"\nMusic found: {MUSIC.resolve()}")
            print("Adding background music...")

            run([
                "ffmpeg", "-y",
                "-i", str(text_video),
                "-stream_loop", "-1",
                "-i", str(MUSIC),

                "-filter_complex",
                (
                    f"[1:a]"
                    f"atrim=0:{AD_DURATION},"
                    f"asetpts=N/SR/TB,"
                    f"volume=0.22,"
                    f"afade=t=in:st=0:d=0.35,"
                    f"afade=t=out:st={AD_DURATION - 0.55}:d=0.55"
                    f"[music]"
                ),

                "-map", "0:v",
                "-map", "[music]",

                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(OUTPUT),
            ])

        else:
            print("\nNo music.mp3 found.")
            print("Creating V2 without audio for now...")

            shutil.copyfile(
                text_video,
                OUTPUT,
            )

    # --------------------------------------------------------
    # DONE
    # --------------------------------------------------------

    print("\n============================================================")
    print("DONE")
    print("============================================================")
    print(f"Created: {OUTPUT.resolve()}")

    if MUSIC.exists():
        print("Audio: background music added")
    else:
        print("Audio: silent")
        print(
            "\nTip: drop a file named music.mp3 into this folder "
            "and run the script again."
        )


if __name__ == "__main__":
    main()
