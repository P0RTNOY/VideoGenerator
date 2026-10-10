#!/usr/bin/env python3
"""
build_soundcore_ad_v4.py

Final-ish V4 builder for the Soundcore Select 4 Go spec ad.

Expected files in the same folder:
  soundcore_shot1.mp4
  soundcore_shot2.mp4
  soundcore_shot3.mp4
  music.mp3

Output:
  soundcore_final_ad_v4.mp4

V4 improvements:
  - tighter edit
  - subtle video crossfades
  - text fade-in / fade-out
  - stronger, cleaner music mix
  - stronger transition whooshes
  - subtle text pops
  - light poolside ambience
  - final loudness normalization around -14 LUFS
  - true peak ceiling around -1.5 dBTP

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
MUSIC = Path("music.mp3")

OUTPUT = Path("soundcore_final_ad_v4.mp4")

# input, source start, duration
CUTS = [
    (SHOT1, 0.00, 2.60),
    (SHOT2, 0.30, 2.15),
    (SHOT3, 0.15, 3.45),
]

TRANSITION = 0.12
AD_DURATION = sum(d for _, _, d in CUTS) - (2 * TRANSITION)

# text, start, end, y, font size, yellow accent
TEXTS = [
    ("SMALL SPEAKER.\nBIG ENERGY.", 0.16, 2.20, 135, 58, True),
    ("MADE FOR SUMMER.", 2.72, 4.30, 150, 54, False),
    ("TAKE IT ANYWHERE.", 4.80, 6.34, 150, 52, False),
    ("SOUNDCORE\nSELECT 4 GO", 6.50, AD_DURATION - 0.03, 130, 50, True),
]

TEXT_FADE = 0.14

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
        "Could not find a bold macOS font. "
        "Edit find_font() and provide a .ttf/.ttc path."
    )


def ensure_inputs() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg is not installed.\n"
            "Install with:\n"
            "brew install ffmpeg"
        )

    missing = [str(p) for p, _, _ in CUTS if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing video file(s):\n  - " + "\n  - ".join(missing)
        )

    if not MUSIC.exists():
        raise FileNotFoundError(
            f"Missing {MUSIC}.\n"
            "Place music.mp3 in the same folder as this script."
        )


def make_text_overlay(
    text: str,
    out_path: Path,
    y: int,
    font_size: int,
    accent: bool,
) -> None:
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

    # Soft shadow
    draw.multiline_text(
        (x + 4, y + 4),
        text,
        font=font,
        fill=(0, 0, 0, 155),
        anchor="ma",
        align="center",
        spacing=spacing,
    )

    # White type
    draw.multiline_text(
        (x, y),
        text,
        font=font,
        fill=WHITE,
        anchor="ma",
        align="center",
        spacing=spacing,
    )

    # Soundcore-ish yellow accent
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
    print("SOUNDCORE SPEC AD — V4")
    print("============================================================")
    print(f"Duration: ~{AD_DURATION:.2f}s")
    print(f"Music: {MUSIC.resolve()}")
    print(f"Output: {OUTPUT.resolve()}")

    with tempfile.TemporaryDirectory(prefix="soundcore_ad_v4_") as tmp_name:
        tmp = Path(tmp_name)
        segments: list[Path] = []

        # ----------------------------------------------------
        # 1) Trim and normalize source clips
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
        # 2) Subtle crossfades
        # ----------------------------------------------------

        offset1 = CUTS[0][2] - TRANSITION
        len12 = CUTS[0][2] + CUTS[1][2] - TRANSITION
        offset2 = len12 - TRANSITION

        base_video = tmp / "base_crossfaded.mp4"

        xfade_filter = (
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
            "-filter_complex", xfade_filter,
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
        # 3) Typography overlays
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

            overlays.append((overlay, start, end))

        text_video = tmp / "text_video.mp4"

        cmd = ["ffmpeg", "-y", "-i", str(base_video)]

        for overlay, _, _ in overlays:
            cmd += ["-loop", "1", "-i", str(overlay)]

        vf_parts = []
        previous = "[0:v]"

        for i, (_, start, end) in enumerate(overlays, start=1):
            faded = f"[txt{i}]"
            out_label = f"[v{i}]"
            local_fade_out = max(0.0, end - start - TEXT_FADE)

            vf_parts.append(
                f"[{i}:v]"
                f"format=rgba,"
                f"fade=t=in:st=0:d={TEXT_FADE}:alpha=1,"
                f"fade=t=out:st={local_fade_out}:d={TEXT_FADE}:alpha=1"
                f"{faded}"
            )

            vf_parts.append(
                f"{previous}{faded}"
                f"overlay=0:0:"
                f"enable='between(t,{start},{end})':"
                f"eof_action=pass"
                f"{out_label}"
            )

            previous = out_label

        cmd += [
            "-filter_complex", ";".join(vf_parts),
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
        # 4) Stronger, cleaner sound design
        # ----------------------------------------------------

        transition1_t = CUTS[0][2] - TRANSITION / 2
        transition2_t = (
            CUTS[0][2]
            + CUTS[1][2]
            - TRANSITION
            - TRANSITION / 2
        )

        text_pop_times = [entry[1] for entry in TEXTS]

        # Inputs:
        # 0 = video
        # 1 = music
        # 2 = pool ambience noise
        # 3,4 = transition whooshes
        # 5..8 = text pops

        audio_inputs = [
            "ffmpeg", "-y",
            "-i", str(text_video),
            "-stream_loop", "-1",
            "-i", str(MUSIC),

            "-f", "lavfi",
            "-t", f"{AD_DURATION:.3f}",
            "-i", "anoisesrc=color=pink:amplitude=0.10:sample_rate=48000",

            "-f", "lavfi",
            "-t", "0.32",
            "-i", "anoisesrc=color=white:amplitude=0.20:sample_rate=48000",

            "-f", "lavfi",
            "-t", "0.32",
            "-i", "anoisesrc=color=white:amplitude=0.20:sample_rate=48000",
        ]

        for _ in text_pop_times:
            audio_inputs += [
                "-f", "lavfi",
                "-t", "0.10",
                "-i", "sine=frequency=950:sample_rate=48000",
            ]

        af = []

        # Music — the main sonic layer.
        af.append(
            f"[1:a]"
            f"atrim=0:{AD_DURATION:.3f},"
            f"asetpts=N/SR/TB,"
            f"highpass=f=35,"
            f"volume=0.62,"
            f"afade=t=in:st=0:d=0.25,"
            f"afade=t=out:st={max(0, AD_DURATION - 0.50):.3f}:d=0.50"
            f"[music]"
        )

        # Subtle pool ambience.
        af.append(
            f"[2:a]"
            f"highpass=f=250,"
            f"lowpass=f=2400,"
            f"volume=0.17,"
            f"afade=t=in:st=0:d=0.35,"
            f"afade=t=out:st={max(0, AD_DURATION - 0.40):.3f}:d=0.40"
            f"[amb]"
        )

        # Whoosh 1
        d1 = int(transition1_t * 1000)
        af.append(
            f"[3:a]"
            f"highpass=f=550,"
            f"lowpass=f=7000,"
            f"volume=0.50,"
            f"afade=t=in:st=0:d=0.035,"
            f"afade=t=out:st=0.08:d=0.24,"
            f"adelay={d1}|{d1}"
            f"[wh1]"
        )

        # Whoosh 2
        d2 = int(transition2_t * 1000)
        af.append(
            f"[4:a]"
            f"highpass=f=550,"
            f"lowpass=f=7000,"
            f"volume=0.50,"
            f"afade=t=in:st=0:d=0.035,"
            f"afade=t=out:st=0.08:d=0.24,"
            f"adelay={d2}|{d2}"
            f"[wh2]"
        )

        # Text pops
        pop_labels = []
        input_index = 5

        for idx, pop_time in enumerate(text_pop_times, start=1):
            delay_ms = int(pop_time * 1000)
            label = f"[pop{idx}]"
            pop_labels.append(label)

            af.append(
                f"[{input_index}:a]"
                f"highpass=f=650,"
                f"volume=0.16,"
                f"afade=t=out:st=0.015:d=0.085,"
                f"adelay={delay_ms}|{delay_ms}"
                f"{label}"
            )

            input_index += 1

        mix_inputs = "[music][amb][wh1][wh2]" + "".join(pop_labels)
        mix_count = 4 + len(pop_labels)

        # Master:
        # First mix naturally, then loudness normalize for mobile/social playback.
        af.append(
            f"{mix_inputs}"
            f"amix=inputs={mix_count}:duration=longest:normalize=0,"
            f"highpass=f=28,"
            f"alimiter=limit=0.95,"
            f"loudnorm=I=-14:LRA=7:TP=-1.5"
            f"[finalaudio]"
        )

        audio_inputs += [
            "-filter_complex", ";".join(af),
            "-map", "0:v",
            "-map", "[finalaudio]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            "-ar", "48000",
            "-shortest",
            "-movflags", "+faststart",
            str(OUTPUT),
        ]

        run(audio_inputs)

    print("\n============================================================")
    print("DONE")
    print("============================================================")
    print(f"Created: {OUTPUT.resolve()}")
    print(f"Duration: ~{AD_DURATION:.2f}s")
    print("Audio target: ~-14 LUFS, true peak <= -1.5 dBTP")
    print("\nPlay it with:")
    print(f"open '{OUTPUT}'")


if __name__ == "__main__":
    main()
