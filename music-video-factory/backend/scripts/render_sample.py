"""Render 15 s sample videos (one per lyric style) from the demo assets.

Uses the synthesized demo song (start 24 s = chorus), the demo clips and the
demo LRC timings converted to absolute LyricLine dicts, renders with
:func:`app.services.render.render_video`, validates each output with
:func:`app.services.quality.validate_output`, and extracts PNG frames at
chosen times (plus a contact sheet per style) for visual inspection.

Usage (from backend/):
  .venv/bin/python scripts/render_sample.py [--styles simple,karaoke] [--out var/samples]
         [--times 2.0,6.0,10.0,13.5] [--preset veryfast] [--start 24] [--duration 15] [--mirror]

Shots are not mirrored unless ``--mirror`` is given (like ``GenerationSettings.allow_mirror``).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.quality import validate_output  # noqa: E402
from app.services.render import DEFAULT_PRESET, RenderShot, RenderSpec, render_video  # noqa: E402
from app.services.subtitles import LYRIC_STYLE_IDS  # noqa: E402

DEMO_DIR = BACKEND_DIR / "var" / "demo"
DEFAULT_OUT = BACKEND_DIR / "var" / "samples"
DEMO_BPM = 120.0  # the demo song is synthesized at exactly 120 BPM from t=0

# Per style: (colour grade, transition) so the samples also cover grades and both joins.
STYLE_LOOKS: dict[str, tuple[str, str]] = {
    "simple": ("natural", "cut"),
    "cinematic": ("teal_orange", "crossfade"),
    "minimal": ("faded_pastel", "crossfade"),
    "karaoke": ("vivid", "cut"),
    "fade": ("warm_film", "crossfade"),
}

_LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def ensure_demo_assets() -> None:
    needed = ["demo_song.wav", "demo_lyrics_ja.lrc", "clip_01.mp4", "clip_02.mp4", "clip_03.mp4"]
    if not all((DEMO_DIR / name).is_file() for name in needed):
        subprocess.run([sys.executable, str(BACKEND_DIR / "scripts" / "make_demo_assets.py")], check=True)


def load_demo_lines() -> list[dict[str, Any]]:
    """Demo LRC → absolute LyricLine dicts (end = next line start, last = start + 4 s)."""
    text = (DEMO_DIR / "demo_lyrics_ja.lrc").read_text(encoding="utf-8")
    try:
        from app.services.lyrics_parse import parse_lyrics  # type: ignore[import-not-found]

        lines, _warnings = parse_lyrics(text, "lrc")
        if lines:
            return [dict(line) for line in lines]
    except ImportError:
        pass
    stamped: list[tuple[float, str]] = []
    for raw in text.splitlines():
        tags = _LRC_TAG.findall(raw)
        lyric = _LRC_TAG.sub("", raw).strip()
        if not tags or not lyric:
            continue
        for minutes, seconds in tags:
            stamped.append((int(minutes) * 60 + float(seconds), lyric))
    stamped.sort()
    result: list[dict[str, Any]] = []
    for index, (start, lyric) in enumerate(stamped):
        end = stamped[index + 1][0] if index + 1 < len(stamped) else start + 4.0
        result.append({"id": f"l{index + 1:02d}", "text": lyric, "start": start, "end": end,
                       "words": None, "source": "lrc", "confidence": None})  # fmt: skip
    return result


def demo_shots(duration: float, variant: int, *, mirror: bool = False) -> list[RenderShot]:
    """One-bar shots (2 s at 120 BPM) cycling the demo clips with varied motion (flips only with ``mirror``)."""
    clips = [DEMO_DIR / "clip_01.mp4", DEMO_DIR / "clip_02.mp4", DEMO_DIR / "clip_03.mp4"]
    pans = ["center", "left", "up", "right", "down"]
    bar = 4 * 60.0 / DEMO_BPM
    shots: list[RenderShot] = []
    remaining, index = duration, 0
    while remaining > 1e-6:
        length = min(bar, remaining)
        k = index + variant
        zoom_in = k % 2 == 0
        shots.append(
            RenderShot(
                clip_path=str(clips[k % len(clips)]),
                src_in=0.4 * (k % 4),
                duration=round(length, 6),
                hflip=mirror and k % 3 == 1,
                zoom_from=1.0 if zoom_in else 1.08,
                zoom_to=1.08 if zoom_in else 1.0,
                pan=pans[k % len(pans)],
            )
        )
        remaining -= length
        index += 1
    return shots


def extract_frames(video: Path, times: list[float], out_dir: Path, stem: str) -> list[Path]:
    paths: list[Path] = []
    for t in times:
        out = out_dir / f"{stem}_t{t:05.2f}.png"
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
               "-frames:v", "1", str(out)]  # fmt: skip
        subprocess.run(cmd, check=True)
        paths.append(out)
    return paths


def contact_sheet(frames: list[Path], out: Path, tile_width: int = 360) -> None:
    inputs: list[str] = []
    for frame in frames:
        inputs += ["-i", str(frame)]
    scaled = "".join(f"[{i}:v]scale={tile_width}:-2[s{i}];" for i in range(len(frames)))
    stacked = "".join(f"[s{i}]" for i in range(len(frames))) + f"hstack=inputs={len(frames)}"
    if len(frames) == 1:
        stacked = "[s0]null"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", scaled + stacked, str(out)]
    subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--styles", default=",".join(LYRIC_STYLE_IDS))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--start", type=float, default=24.0)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--times", default="2.0,6.0,10.0,13.5", help="frame times (s, relative) to extract")
    parser.add_argument("--preset", default=DEFAULT_PRESET)
    parser.add_argument("--audio", default="demo_song.wav", help="demo audio file name (wav or mp3)")
    parser.add_argument("--mirror", action="store_true", help="flip every third shot horizontally")
    args = parser.parse_args(argv)

    ensure_demo_assets()
    out_dir: Path = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = load_demo_lines()
    beats = [i * 60.0 / DEMO_BPM for i in range(int(70 * DEMO_BPM / 60))]
    times = [float(t) for t in args.times.split(",") if t.strip()]
    styles = [s.strip() for s in args.styles.split(",") if s.strip()]
    summary: list[dict[str, Any]] = []

    for variant, style in enumerate(styles):
        grade, transition = STYLE_LOOKS.get(style, ("natural", "cut"))
        output = out_dir / f"sample_{style}.mp4"
        spec = RenderSpec(
            shots=demo_shots(args.duration, variant, mirror=args.mirror),
            audio_path=str(DEMO_DIR / args.audio),
            audio_start=args.start,
            duration=args.duration,
            lyric_lines=lines,
            lyric_style=style,
            text_style={},
            color_grade=grade,
            output_path=str(output),
            work_dir=str(out_dir / "work" / style),
            beats=beats,
            preset=args.preset,
            transition=transition,
        )
        started = time.monotonic()
        result = render_video(spec)
        render_seconds = time.monotonic() - started
        report = validate_output(output, expected_duration=args.duration)
        frames = extract_frames(output, times, out_dir, f"{style}")
        contact_sheet(frames, out_dir / f"sheet_{style}.png")
        failed = [c["name"] for c in report["checks"] if not c["passed"]]
        summary.append(
            {
                "style": style,
                "grade": grade,
                "transition": transition,
                "seconds": round(render_seconds, 2),
                "timings": result.timings,
                "size_mb": round(result.size_bytes / 1e6, 2),
                "mbps": round(result.size_bytes * 8 / max(result.duration, 0.001) / 1e6, 2),
                "duration": result.duration,
                "passed": report["passed"],
                "failed_checks": failed,
                "lufs": report["loudness_lufs"],
                "audio_gain_db": result.audio_gain_db,
            }
        )
        print(
            f"{style:10s} {grade:13s} {transition:9s} render {render_seconds:5.1f}s  "
            f"{result.size_bytes / 1e6:5.1f} MB  dur {result.duration:.3f}s  gain {result.audio_gain_db:+.2f} dB  "
            f"quality {'OK' if report['passed'] else 'NG ' + ','.join(failed)}",
            flush=True,
        )
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(item["passed"] for item in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
