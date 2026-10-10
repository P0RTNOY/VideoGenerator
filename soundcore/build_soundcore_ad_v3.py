#!/usr/bin/env python3
"""
build_soundcore_ad_v3.py

Polished V3 builder for the Soundcore Select 4 Go spec ad.

Expected files in the same folder:
  soundcore_shot1.mp4
  soundcore_shot2.mp4
  soundcore_shot3.mp4

Optional:
  music.mp3

Output:
  soundcore_final_ad_v3.mp4

What V3 adds:
  - tighter cuts
  - subtle crossfades between shots
  - cleaner typography with fade-in / fade-out
  - generated pool ambience
  - subtle whoosh sounds on transitions
  - tiny UI-style pops on text appearances
  - optional background music if music.mp3 exists
  - final audio mastering / limiter

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
OUTPUT = Path("soundcore_final_ad_v3.mp4")

# Source cuts.
# (input_file, source_start, duration)
CUTS = [
    (SHOT1, 0.00, 2.65),
    (SHOT2, 0.30, 2.20),
    (SHOT3, 0.15, 3.35),
]

# Subtle crossfade length between clips.
TRANSITION = 0.12

# Because crossfades overlap clips:
AD_DURATION = sum(duration for _, _, duration in CUTS) - (TRANSITION * 2)

# Final text timing.
# text, start, end, y, font_size, accent
TEXTS = [
    ("SMALL SPEAKER.\nBIG ENERGY.", 0.18, 2.25, 135, 58, True),
    ("MADE FOR SUMMER.", 2.78, 4.42, 150, 54, False),
    ("TAKE IT ANYWHERE.", 4.95, 6.45, 150, 52, False),
    ("SOUNDCORE\nSELECT 4 GO", 6.62, AD_DURATION - 0.04, 130, 50, True),
]

# Text fade duration in seconds.
TEXT_FADE = 0.16

# Brand-ish color accent.
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
        "Could not find a suitable macOS font.\n"
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
    Creates a transparent PNG overlay:
      - bold white text
      - soft black shadow
      - optional yellow accent line
    """
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    font = ImageFont.truetype(find_font(), font_size)
    spacing = 8

    bbox = draw.multiline_textbbox(
        (0, 0),
        text,
        font=font,
        spacing=spacing,
        align="center",
    )

    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = W // 2

    # Soft shadow.
    draw.multiline_text(
        (x + 4, y + 4),
        text,
        font=font,
        fill=(0, 0, 0, 155),
        anchor="ma",
        align="center",
        spacing=spacing,
    )

    # Main text.
    draw.multiline_text(
        (x, y),
        text,
        font=font,
        fill=WHITE,
        anchor="ma",
        align="center",
        spacing=spacing,
    )

    # Yellow accent line.
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

    print("\n============================================================")
    print("SOUNDCORE AD V3")
    print("============================================================")
    print(f"Target duration: {AD_DURATION:.2f}s")
    print(f"Output: {OUTPUT.resolve()}")

    with tempfile.TemporaryDirectory(prefix="soundcore_ad_v3_") as tmp_name:
        tmp = Path(tmp_name)
        segments: list[Path] = []

        # ----------------------------------------------------
        # 1) Trim + normalize source clips
        # ----------------------------------------------------

        for i, (src, start, duration) in enumerate(CUTS, start=1):
            out = tmp / f"segment_{i}.mp4"
            segments.append(out)

            run([
                "ffmpeg", "-y",
                "-ss", str(start),
                "-t", str(duration),
                "-i", str(src),
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
        # 2) Crossfade the three clips
        # ----------------------------------------------------

        # xfade offset #1 = end of shot 1 minus transition.
        offset1 = CUTS[0][2] - TRANSITION

        # After the first overlap, timeline length is:
        len12 = CUTS[0][2] + CUTS[1][2] - TRANSITION

        # xfade offset #2 = end of combined timeline minus transition.
        offset2 = len12 - TRANSITION

        base_video = tmp / "base_crossfaded.mp4"

        filter_complex = (
            f"[0:v][1:v]"
            f"xfade=transition=fade:duration={TRANSITION}:offset={offset1}"
            f"[v12];"
            f"[v12][2:v]"
            f"xfade=transition=fade:duration={TRANSITION}:offset={offset2}"
            f"[vout]"
        )

        run([
            "ffmpeg", "-y",
            "-i", str(segments[0]),
            "-i", str(segments[1]),
            "-i", str(segments[2]),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            "-movflags", "+faststart",
            str(base_video),
        ])

        # ----------------------------------------------------
        # 3) Create text overlays
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
        # 4) Add text with fade-in / fade-out
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
            faded_overlay = f"[txt{i}]"
            output_label = f"[v{i}]"

            fade_out_start = max(0.0, end - start - TEXT_FADE)

            filter_parts.append(
                f"[{i}:v]"
                f"format=rgba,"
                f"fade=t=in:st=0:d={TEXT_FADE}:alpha=1,"
                f"fade=t=out:st={fade_out_start}:d={TEXT_FADE}:alpha=1"
                f"{faded_overlay}"
            )

            filter_parts.append(
                f"{previous}{faded_overlay}"
                f"overlay=0:0:"
                f"enable='between(t,{start},{end})':"
                f"eof_action=pass"
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
            "-t", f"{AD_DURATION:.3f}",
            str(text_video),
        ]

        run(cmd)

        # ----------------------------------------------------
        # 5) Build sound design
        # ----------------------------------------------------

        # Transition timestamps on final timeline.
        transition1_t = CUTS[0][2] - TRANSITION / 2
        transition2_t = (
            CUTS[0][2]
            + CUTS[1][2]
            - TRANSITION
            - TRANSITION / 2
        )

        # Text-pop timestamps.
        pop_times = [t[1] for t in TEXTS]

        soundbed = tmp / "soundbed.m4a"

        # ffmpeg-generated sound design:
        # - low-volume filtered noise as subtle pool ambience
        # - 2 short whooshes at transitions
        # - 4 short pops at text appearance
        #
        # No external SFX files required.

        audio_inputs = [
            "-f", "lavfi",
            "-t", f"{AD_DURATION:.3f}",
            "-i", "anoisesrc=color=pink:amplitude=0.08:sample_rate=48000",
        ]

        # Two whoosh sources.
        for _ in range(2):
            audio_inputs += [
                "-f", "lavfi",
                "-t", "0.28",
                "-i", "anoisesrc=color=white:amplitude=0.16:sample_rate=48000",
            ]

        # Four pop sources.
        for _ in pop_times:
            audio_inputs += [
                "-f", "lavfi",
                "-t", "0.10",
                "-i", "sine=frequency=880:sample_rate=48000",
            ]

        afilters = []

        # Pool ambience from pink noise:
        # remove lows/highs and keep it very quiet.
        afilters.append(
            f"[0:a]"
            f"highpass=f=220,"
            f"lowpass=f=1900,"
            f"volume=0.075,"
            f"afade=t=in:st=0:d=0.35,"
            f"afade=t=out:st={max(0, AD_DURATION - 0.45):.3f}:d=0.45"
            f"[amb]"
        )

        # Whoosh #1
        delay1_ms = int(transition1_t * 1000)
        afilters.append(
            f"[1:a]"
            f"highpass=f=700,"
            f"lowpass=f=6500,"
            f"volume=0.18,"
            f"afade=t=in:st=0:d=0.04,"
            f"afade=t=out:st=0.09:d=0.19,"
            f"adelay={delay1_ms}|{delay1_ms}"
            f"[wh1]"
        )

        # Whoosh #2
        delay2_ms = int(transition2_t * 1000)
        afilters.append(
            f"[2:a]"
            f"highpass=f=700,"
            f"lowpass=f=6500,"
            f"volume=0.18,"
            f"afade=t=in:st=0:d=0.04,"
            f"afade=t=out:st=0.09:d=0.19,"
            f"adelay={delay2_ms}|{delay2_ms}"
            f"[wh2]"
        )

        # Text pops.
        pop_labels = []
        input_index = 3

        for idx, pop_time in enumerate(pop_times, start=1):
            delay_ms = int(pop_time * 1000)
            label = f"[pop{idx}]"
            pop_labels.append(label)

            afilters.append(
                f"[{input_index}:a]"
                f"volume=0.055,"
                f"afade=t=out:st=0.02:d=0.08,"
                f"adelay={delay_ms}|{delay_ms}"
                f"{label}"
            )

            input_index += 1

        mix_inputs = "[amb][wh1][wh2]" + "".join(pop_labels)

        afilters.append(
            f"{mix_inputs}"
            f"amix=inputs={3 + len(pop_labels)}:"
            f"duration=longest:"
            f"normalize=0,"
            f"alimiter=limit=0.90"
            f"[sfx]"
        )

        sound_cmd = [
            "ffmpeg", "-y",
            *audio_inputs,
            "-filter_complex", ";".join(afilters),
            "-map", "[sfx]",
            "-c:a", "aac",
            "-b:a", "192k",
            "-t", f"{AD_DURATION:.3f}",
            str(soundbed),
        ]

        run(sound_cmd)

        # ----------------------------------------------------
        # 6) Optional music + final audio master
        # ----------------------------------------------------

        if MUSIC.exists():
            print(f"\nMusic found: {MUSIC.resolve()}")
            print("Mixing music + ambience + transition SFX...")

            run([
                "ffmpeg", "-y",
                "-i", str(text_video),
                "-i", str(soundbed),
                "-stream_loop", "-1",
                "-i", str(MUSIC),

                "-filter_complex",
                (
                    f"[2:a]"
                    f"atrim=0:{AD_DURATION:.3f},"
                    f"asetpts=N/SR/TB,"
                    f"volume=0.20,"
                    f"afade=t=in:st=0:d=0.30,"
                    f"afade=t=out:st={max(0, AD_DURATION - 0.55):.3f}:d=0.55"
                    f"[music];"
                    f"[1:a][music]"
                    f"amix=inputs=2:duration=longest:normalize=0,"
                    f"alimiter=limit=0.92"
                    f"[finalaudio]"
                ),

                "-map", "0:v",
                "-map", "[finalaudio]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(OUTPUT),
            ])

        else:
            print("\nNo music.mp3 found.")
            print("Using generated ambience + whooshes + text pops only.")

            run([
                "ffmpeg", "-y",
                "-i", str(text_video),
                "-i", str(soundbed),
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(OUTPUT),
            ])

    # --------------------------------------------------------
    # DONE
    # --------------------------------------------------------

    print("\n============================================================")
    print("DONE")
    print("============================================================")
    print(f"Created: {OUTPUT.resolve()}")
    print(f"Duration: ~{AD_DURATION:.2f}s")

    if MUSIC.exists():
        print("Audio: music + ambience + whooshes + text pops")
    else:
        print("Audio: ambience + whooshes + text pops")
        print(
            "\nOptional: add a file named music.mp3 to this folder "
            "and run the script again."
        )


if __name__ == "__main__":
    main()
