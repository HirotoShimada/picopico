"""Generate royalty-free demo assets for development and tests.

Everything here is synthesized locally (numpy + FFmpeg lavfi), so there are no
rights issues and no network access is needed.

Outputs (default: backend/var/demo/):
  demo_song.wav      60 s, 44.1 kHz stereo, 120 BPM. Structure:
                       0-8   intro   (pad only, quiet)
                       8-24  verse   (drums + bass + "vocal" melody)
                       24-40 chorus  (louder, denser, brighter)
                       40-48 bridge  (sparse, quiet)
                       48-60 chorus  (loud) with 2 s fade-out at the end
  demo_song.mp3      same song as MP3 (exercises the decode path)
  demo_lyrics_ja.txt Japanese lyrics, one line per vocal phrase
  demo_lyrics_ja.lrc same lyrics with line timestamps (LRC)
  clip_01..03.mp4    short 9:16-ish and 16:9 source clips (different sizes,
                     frame rates and durations on purpose, to exercise
                     normalisation)

Usage:
  python scripts/make_demo_assets.py [--out DIR] [--force]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
BPM = 120.0
BEAT = 60.0 / BPM
DURATION = 60.0

SECTIONS = [
    # (start, end, label, intensity)
    (0.0, 8.0, "intro", 0.25),
    (8.0, 24.0, "verse", 0.55),
    (24.0, 40.0, "chorus", 1.0),
    (40.0, 48.0, "bridge", 0.3),
    (48.0, 60.0, "chorus", 1.0),
]

# Vocal phrases: (start, end, text). Each phrase spans 2 bars (4 s) in the
# verse/chorus, leaving the intro and bridge instrumental.
LYRICS = [
    (8.0, 11.6, "夜明け前の街を ひとりで歩いた"),
    (12.0, 15.6, "消えそうな光を 両手で包んで"),
    (16.0, 19.6, "名前も知らない 風に吹かれて"),
    (20.0, 23.6, "それでも僕らは 前を向いていた"),
    (24.0, 27.6, "叫べ 今この瞬間を"),
    (28.0, 31.6, "忘れないように 胸に刻んで"),
    (32.0, 35.6, "走れ 遠く遥か先へ"),
    (36.0, 39.6, "君と見た空を 信じているから"),
    (48.0, 51.6, "叫べ 今この瞬間を"),
    (52.0, 55.6, "忘れないように 胸に刻んで"),
]


def _section_at(t: float):
    for s in SECTIONS:
        if s[0] <= t < s[1]:
            return s
    return SECTIONS[-1]


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-t / max(decay, 1e-4))


def _add(buf: np.ndarray, start_s: float, sig: np.ndarray) -> None:
    i = int(start_s * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[: j - i]


def synth_song(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(DURATION * SR)
    mix = np.zeros(n, dtype=np.float64)
    vocal = np.zeros(n, dtype=np.float64)

    # Chord progression (A minor-ish): Am F C G, one chord per bar.
    roots = [220.0, 174.61, 261.63, 196.0]
    n_beats = int(DURATION / BEAT)
    for b in range(n_beats):
        t0 = b * BEAT
        start, end, label, inten = _section_at(t0)
        bar = b // 4
        beat_in_bar = b % 4
        root = roots[bar % 4]

        # Pad: one chord per bar, always present.
        if beat_in_bar == 0:
            ln = int(4 * BEAT * SR)
            tt = np.arange(ln) / SR
            pad = sum(np.sin(2 * np.pi * root * r * tt) for r in (1.0, 1.2, 1.5))
            pad *= 0.06 * (0.6 + 0.6 * inten) * _env(ln, 0.3, 6.0)
            _add(mix, t0, pad)

        if label == "intro":
            continue

        # Kick on every beat (verse/chorus), only on 1 in the bridge.
        if label != "bridge" or beat_in_bar == 0:
            ln = int(0.35 * SR)
            tt = np.arange(ln) / SR
            freq = 50 + 90 * np.exp(-tt * 25)
            kick = np.sin(2 * np.pi * np.cumsum(freq) / SR) * _env(ln, 0.002, 0.12)
            _add(mix, t0, 0.9 * inten * kick)

        # Snare on 2 and 4.
        if beat_in_bar in (1, 3) and label != "bridge":
            ln = int(0.2 * SR)
            snare = rng.normal(0, 1, ln) * _env(ln, 0.001, 0.06)
            _add(mix, t0, 0.35 * inten * snare)

        # Hi-hats: 8ths in verse, 16ths in chorus.
        if label in ("verse", "chorus"):
            div = 4 if label == "chorus" else 2
            for k in range(div):
                ln = int(0.05 * SR)
                hat = rng.normal(0, 1, ln)
                hat = np.diff(np.concatenate([[0.0], hat]))  # crude high-pass
                hat *= _env(ln, 0.0005, 0.015)
                _add(mix, t0 + k * BEAT / div, 0.12 * inten * hat)

        # Bass on every beat.
        if label in ("verse", "chorus"):
            ln = int(BEAT * SR * 0.9)
            tt = np.arange(ln) / SR
            bass = np.tanh(2.5 * np.sin(2 * np.pi * (root / 2) * tt)) * _env(ln, 0.005, 0.4)
            _add(mix, t0, 0.22 * inten * bass)

    # "Vocal": a vibrato lead in the 300-1000 Hz band on each lyric phrase,
    # with syllable-like amplitude modulation.
    melody = [440.0, 493.88, 523.25, 587.33, 659.25, 587.33, 523.25, 493.88]
    for idx, (s, e, text) in enumerate(LYRICS):
        ln = int((e - s) * SR)
        tt = np.arange(ln) / SR
        notes = np.array(melody)[(np.arange(ln) * 8 // ln + idx) % len(melody)]
        vib = 1 + 0.012 * np.sin(2 * np.pi * 5.5 * tt)
        phase = 2 * np.pi * np.cumsum(notes * vib) / SR
        voice = np.sin(phase) + 0.4 * np.sin(2 * phase) + 0.2 * np.sin(3 * phase)
        syllables = max(4, len(text.replace(" ", "")))
        am = 0.55 + 0.45 * np.abs(np.sin(np.pi * syllables * tt / (e - s)))
        edge = np.minimum(1, np.minimum(tt / 0.05, (tt[-1] - tt) / 0.15))
        inten = _section_at(s)[3]
        _add(vocal, s, 0.22 * (0.7 + 0.3 * inten) * voice * am * edge)

    out = mix + vocal
    # Fade out the last 2 seconds.
    fade = np.ones(n)
    k = int(2.0 * SR)
    fade[-k:] = np.linspace(1, 0, k)
    out *= fade
    out /= max(1e-9, np.max(np.abs(out))) / 0.89
    # Slight stereo width: delay the right channel by 7 ms.
    d = int(0.007 * SR)
    right = np.concatenate([np.zeros(d), out[:-d]])
    return np.stack([out, right], axis=1).astype(np.float32)


def write_lyrics(out: Path) -> None:
    (out / "demo_lyrics_ja.txt").write_text("\n".join(t for _, _, t in LYRICS) + "\n", encoding="utf-8")
    lrc = ["[ti:Demo Song]", "[ar:Demo Artist]"]
    for s, _, t in LYRICS:
        m, sec = divmod(s, 60)
        lrc.append(f"[{int(m):02d}:{sec:05.2f}]{t}")
    (out / "demo_lyrics_ja.lrc").write_text("\n".join(lrc) + "\n", encoding="utf-8")


def _ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    subprocess.run(cmd, check=True)


def write_clips(out: Path) -> None:
    # Different sizes / fps / durations on purpose.
    specs = [
        ("clip_01.mp4", "gradients=s=720x1280:r=24:speed=0.02:c0=0x1d2b53:c1=0xff6f61:c2=0x0b0b0f:seed=1", 6),
        ("clip_02.mp4", "mandelbrot=s=1280x720:r=30", 8),
        ("clip_03.mp4", "gradients=s=1080x1920:r=30:speed=0.015:c0=0x0f3d3e:c1=0xe0c097:c2=0x101418:seed=3", 5),
    ]
    for name, src, dur in specs:
        _ffmpeg([
            "-f", "lavfi", "-i", f"{src},format=yuv420p",
            "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            str(out / name),
        ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "var" / "demo")
    ap.add_argument("--force", action="store_true", help="regenerate even if files exist")
    args = ap.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    wav = out / "demo_song.wav"
    if args.force or not wav.exists():
        sf.write(wav, synth_song(), SR, subtype="PCM_16")
        _ffmpeg(["-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(out / "demo_song.mp3")])
    write_lyrics(out)
    if args.force or not (out / "clip_03.mp4").exists():
        write_clips(out)
    print(f"demo assets in {out}")
    for p in sorted(out.iterdir()):
        print(f"  {p.name:22s} {p.stat().st_size:>10,d} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
