"""Integration tests for the FFmpeg renderer (app.services.render)."""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.services import render as render_mod
from app.services.errors import RenderError
from app.services.quality import validate_output
from app.services.render import (
    FALLBACK_COLOR_GRADES,
    RenderResult,
    RenderShot,
    RenderSpec,
    escape_filter_value,
    render_video,
    split_filter_chain,
)
from app.services.subtitles import safe_area

pytestmark = pytest.mark.slow

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
SR = 44100

DEMO_LINES = [
    {"id": "l05", "text": "叫べ 今この瞬間を", "start": 24.0, "end": 28.0, "words": None, "source": "lrc"},
    {"id": "l06", "text": "忘れないように 胸に刻んで", "start": 28.0, "end": 32.0, "words": None, "source": "lrc"},
    {"id": "l07", "text": "走れ 遠く遥か先へ", "start": 32.0, "end": 36.0, "words": None, "source": "lrc"},
    {"id": "l08", "text": "君と見た空を 信じているから", "start": 36.0, "end": 40.0, "words": None, "source": "lrc"},
]


# --------------------------------------------------------------------------- helpers


def ffprobe(path: Path, *args: str) -> dict[str, Any]:
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", *args, str(path)]
    return json.loads(subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout)


def count_frames(path: Path) -> int:
    data = ffprobe(path, "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames")
    return int(data["streams"][0]["nb_read_frames"])


def frame_rgb(path: Path, t: float, width: int, height: int) -> np.ndarray:
    cmd = [
        FFMPEG,
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-ss",
        f"{t:.3f}",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).astype(np.int16)


def decode_mono(path: Path, start: float | None = None, duration: float | None = None) -> np.ndarray:
    cmd = [FFMPEG, "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    return np.frombuffer(raw, dtype="<f4").astype(np.float64)


def best_lag(reference: np.ndarray, signal: np.ndarray, max_lag: int) -> tuple[int, float]:
    """Lag (samples, positive = ``signal`` late) maximising normalised cross-correlation."""
    n = min(len(reference), len(signal))
    ref, sig = reference[:n], signal[:n]
    size = 1 << (2 * n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(sig, size) * np.conj(np.fft.rfft(ref, size)), size)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    values = np.concatenate([corr[: max_lag + 1], corr[-max_lag:]])
    lag = int(lags[int(np.argmax(values))])
    if lag >= 0:
        a, b = ref[: n - lag], sig[lag:]
    else:
        a, b = ref[-lag:], sig[: n + lag]
    coefficient = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
    return lag, coefficient


def make_clip(
    path: Path, lavfi: str, duration: float, *, fps: int = 25, pix_fmt: str = "yuv420p", extra: list[str] | None = None
) -> Path:
    cmd = [
        FFMPEG,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-t",
        f"{duration}",
        "-r",
        str(fps),
        *(extra or []),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        pix_fmt,
        str(path),
    ]
    subprocess.run(cmd, check=True, timeout=120)
    return path


def small_spec(tmp_path: Path, demo_dir: Path, **overrides: Any) -> RenderSpec:
    values: dict[str, Any] = {
        "shots": [
            RenderShot(clip_path=str(demo_dir / "clip_03.mp4"), src_in=0.0, duration=2.0),
            RenderShot(clip_path=str(demo_dir / "clip_01.mp4"), src_in=0.5, duration=2.0, pan="left"),
        ],
        "audio_path": str(demo_dir / "demo_song.wav"),
        "audio_start": 24.0,
        "duration": 4.0,
        "lyric_lines": [],
        "lyric_style": "simple",
        "text_style": {},
        "color_grade": "natural",
        "output_path": str(tmp_path / "out.mp4"),
        "work_dir": str(tmp_path / "work"),
        "width": 540,
        "height": 960,
        "preset": "ultrafast",
    }
    values.update(overrides)
    return RenderSpec(**values)


# --------------------------------------------------------------------------- full render


def test_render_15s_passes_quality_checks(demo_dir: Path, tmp_path: Path) -> None:
    clips = [demo_dir / "clip_01.mp4", demo_dir / "clip_02.mp4", demo_dir / "clip_03.mp4"]
    pans = ["center", "left", "up", "right", "down"]
    shots = [
        RenderShot(
            clip_path=str(clips[i % 3]),
            src_in=0.3 * i,
            duration=2.0 if i < 7 else 1.0,
            hflip=i % 2 == 1,
            zoom_from=1.0 if i % 2 == 0 else 1.08,
            zoom_to=1.08 if i % 2 == 0 else 1.0,
            pan=pans[i % 5],
        )
        for i in range(8)
    ]
    events: list[tuple[float, str]] = []
    spec = RenderSpec(
        shots=shots,
        audio_path=str(demo_dir / "demo_song.wav"),
        audio_start=24.0,
        duration=15.0,
        lyric_lines=DEMO_LINES,
        lyric_style="karaoke",
        text_style={},
        color_grade="teal_orange",
        output_path=str(tmp_path / "final video.mp4"),
        work_dir=str(tmp_path / "work"),
        beats=[i * 0.5 for i in range(140)],
    )
    started = time.monotonic()
    result = render_video(spec, progress_cb=lambda f, stage: events.append((f, stage)))
    elapsed = time.monotonic() - started
    print(f"\n15 s render took {elapsed:.1f} s (timings {result.timings}, {result.size_bytes / 1e6:.1f} MB)")

    assert isinstance(result, RenderResult)
    output = Path(result.output_path)
    assert output.is_file() and result.size_bytes == output.stat().st_size
    assert result.duration == pytest.approx(15.0, abs=0.1)
    report = validate_output(output, expected_duration=15.0)
    failed = [c for c in report["checks"] if not c["passed"]]
    assert report["passed"], failed
    assert count_frames(output) == 450

    info = ffprobe(output, "-show_streams", "-show_format")
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert video["profile"] == "High" and video["codec_name"] == "h264"
    assert (video["width"], video["height"], video["pix_fmt"]) == (1080, 1920, "yuv420p")
    assert video["r_frame_rate"] == "30/1" and video["avg_frame_rate"] == "30/1"
    assert video.get("color_space") == "bt709" and video.get("color_primaries") == "bt709"
    assert audio["codec_name"] == "aac" and audio["profile"] == "LC"
    assert int(audio["sample_rate"]) == 44100 and audio["channels"] == 2
    assert 200_000 <= int(audio["bit_rate"]) <= 300_000

    keyframes = ffprobe(output, "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries", "frame=pts_time")[
        "frames"
    ]
    times = [float(f["pts_time"]) for f in keyframes]
    assert times[0] == pytest.approx(0.0, abs=0.04)
    assert max(b - a for a, b in itertools.pairwise(times)) <= 2.0 + 1e-6  # GOP ≤ 60 frames

    thumb = Path(result.thumbnail_path)
    assert thumb.suffix == ".jpg" and thumb.is_file()
    thumb_info = ffprobe(thumb, "-show_streams")["streams"][0]
    assert (thumb_info["width"], thumb_info["height"]) == (540, 960)

    fractions = [f for f, _ in events]
    assert fractions == sorted(fractions) and fractions[0] == 0.0 and fractions[-1] == 1.0
    assert {"prepare", "encode", "done"} <= {stage for _, stage in events}
    assert not list(output.parent.glob(".*.part.mp4"))


# --------------------------------------------------------------------------- lyrics timing


def test_lyrics_burned_only_while_a_line_is_active(demo_dir: Path, tmp_path: Path) -> None:
    width, height = 540, 960
    lines = [{"text": "叫べ 今この瞬間を", "start": 25.0, "end": 27.0}]
    common = {
        "shots": [
            RenderShot(clip_path=str(demo_dir / "clip_03.mp4"), src_in=0.0, duration=5.0, zoom_from=1.0, zoom_to=1.0)
        ],
        "duration": 5.0,
    }
    with_lyrics = render_video(
        small_spec(
            tmp_path,
            demo_dir,
            lyric_lines=lines,
            output_path=str(tmp_path / "a.mp4"),
            work_dir=str(tmp_path / "wa"),
            **common,
        )
    )
    without = render_video(
        small_spec(
            tmp_path,
            demo_dir,
            lyric_lines=[],
            output_path=str(tmp_path / "b.mp4"),
            work_dir=str(tmp_path / "wb"),
            **common,
        )
    )
    area = safe_area(width, height)
    region = slice(area.bottom - 220, area.bottom)  # lower third where the bottom-positioned text sits

    def changed_fraction(t: float) -> float:
        """Share of text-region pixels that differ clearly (> 40 levels) between the renders."""
        a = frame_rgb(Path(with_lyrics.output_path), t, width, height)
        b = frame_rgb(Path(without.output_path), t, width, height)
        return float((np.abs(a[region] - b[region]).max(axis=2) > 40).mean())

    inside, before, after = changed_fraction(2.0), changed_fraction(0.5), changed_fraction(4.0)
    print(f"\nchanged text-region pixels: inside {inside:.4f}, before {before:.4f}, after {after:.4f}")
    assert inside > 0.01, inside
    assert before == 0.0 and after == 0.0, (before, after)
    assert (
        float(
            np.abs(
                frame_rgb(Path(with_lyrics.output_path), 2.0, width, height)[: area.top]
                - frame_rgb(Path(without.output_path), 2.0, width, height)[: area.top]
            ).mean()
        )
        < 0.6
    )


# --------------------------------------------------------------------------- audio


@pytest.mark.parametrize("source", ["demo_song.wav", "demo_song.mp3"])
def test_audio_is_aligned_and_faithful(demo_dir: Path, tmp_path: Path, source: str) -> None:
    start, duration = 24.0, 8.0
    spec = small_spec(
        tmp_path,
        demo_dir,
        audio_path=str(demo_dir / source),
        audio_start=start,
        duration=duration,
        shots=[
            RenderShot(clip_path=str(demo_dir / "clip_03.mp4"), src_in=0.0, duration=4.0),
            RenderShot(clip_path=str(demo_dir / "clip_01.mp4"), src_in=0.0, duration=4.0),
        ],
    )
    result = render_video(spec)
    reference = decode_mono(demo_dir / "demo_song.wav", start, duration)  # always compare to the lossless master
    rendered = decode_mono(Path(result.output_path))
    assert len(rendered) == pytest.approx(duration * SR, abs=SR * 0.03)
    middle = slice(int(0.5 * SR), int((duration - 1.0) * SR))  # skip the fade-in/out
    lag, coefficient = best_lag(reference[middle], rendered[middle], max_lag=int(0.1 * SR))
    print(f"\n{source}: lag {lag / SR * 1000:.2f} ms, correlation {coefficient:.4f}")
    assert abs(lag) <= int(0.010 * SR), f"audio offset {lag / SR * 1000:.1f} ms"
    assert coefficient > 0.9
    # fades: quiet at the very start/end, full level in the middle
    rms = lambda x: float(np.sqrt(np.mean(np.square(x))))  # noqa: E731
    assert rms(rendered[: int(0.02 * SR)]) < 0.5 * rms(reference[: int(0.02 * SR)]) + 1e-4
    assert rms(rendered[-int(0.05 * SR) :]) < 0.3 * rms(reference[-int(0.05 * SR) :]) + 1e-4


def test_mono_low_rate_audio_is_upmixed_and_resampled(demo_dir: Path, tmp_path: Path) -> None:
    mono = tmp_path / "mono 22k.wav"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(demo_dir / "demo_song.wav"),
            "-ac",
            "1",
            "-ar",
            "22050",
            str(mono),
        ],
        check=True,
        timeout=120,
    )
    result = render_video(small_spec(tmp_path, demo_dir, audio_path=str(mono), audio_start=10.0))
    audio = next(s for s in ffprobe(Path(result.output_path), "-show_streams")["streams"] if s["codec_type"] == "audio")
    assert int(audio["sample_rate"]) == 48000 and audio["channels"] == 2


# --------------------------------------------------------------------------- joins


@pytest.mark.parametrize("transition", ["crossfade", "cut"])
def test_total_duration_is_exact(demo_dir: Path, tmp_path: Path, transition: str) -> None:
    clips = [demo_dir / "clip_01.mp4", demo_dir / "clip_02.mp4", demo_dir / "clip_03.mp4"]
    durations = [1.7, 2.3, 3.1, 1.9]
    shots = [RenderShot(clip_path=str(clips[i % 3]), src_in=0.2, duration=d) for i, d in enumerate(durations)]
    spec = small_spec(tmp_path, demo_dir, shots=shots, duration=9.0, transition=transition)
    result = render_video(spec)
    output = Path(result.output_path)
    assert count_frames(output) == 270
    report = validate_output(output, expected_duration=9.0, width=540, height=960)
    assert report["passed"], [c for c in report["checks"] if not c["passed"]]
    if transition == "crossfade":
        graph = (tmp_path / "work" / "filtergraph.txt").read_text(encoding="utf-8")
        assert graph.count("xfade=transition=fade") == 3
        # transitions are centred on the planned cut points (1.7, 4.0, 7.1 s)
        for cut in (1.7, 4.0, 7.1):
            assert f"offset={round(cut * 30 - 4) / 30:.6f}".rstrip("0") in graph


def test_crossfade_frames_blend_neighbouring_shots(demo_dir: Path, tmp_path: Path) -> None:
    red = make_clip(tmp_path / "red.mp4", "color=c=0xC02020:s=540x960", 3.0)
    blue = make_clip(tmp_path / "blue.mp4", "color=c=0x2020C0:s=540x960", 3.0)
    shots = [
        RenderShot(clip_path=str(red), src_in=0, duration=2.0, zoom_from=1, zoom_to=1),
        RenderShot(clip_path=str(blue), src_in=0, duration=2.0, zoom_from=1, zoom_to=1),
    ]
    result = render_video(small_spec(tmp_path, demo_dir, shots=shots, transition="crossfade", color_grade="vivid"))
    output = Path(result.output_path)
    centre = frame_rgb(output, 2.0, 540, 960)[480, 270]
    early = frame_rgb(output, 1.5, 540, 960)[480, 270]
    late = frame_rgb(output, 2.5, 540, 960)[480, 270]
    assert early[0] > early[2] + 80 and late[2] > late[0] + 80
    assert abs(int(centre[0]) - int(centre[2])) < 60  # half-way mix of red and blue


# --------------------------------------------------------------------------- robustness


def test_paths_with_spaces_colons_and_quotes(demo_dir: Path, tmp_path: Path) -> None:
    folder = tmp_path / "my dir: 'quoted' [x],y;z"
    folder.mkdir()
    clip = folder / "clip one: a.mp4"
    song = folder / "song 'demo', v1.wav"
    shutil.copy(demo_dir / "clip_03.mp4", clip)
    shutil.copy(demo_dir / "demo_song.wav", song)
    lines = [{"text": "パス: テスト", "start": 24.5, "end": 27.0}]
    spec = small_spec(
        tmp_path,
        demo_dir,
        shots=[RenderShot(clip_path=str(clip), src_in=0.0, duration=3.0)],
        duration=3.0,
        audio_path=str(song),
        lyric_lines=lines,
        output_path=str(folder / "out put: final.mp4"),
        work_dir=str(folder / "work: dir"),
    )
    result = render_video(spec)
    assert Path(result.output_path).name == "out put: final.mp4"
    assert (folder / "work: dir" / "lyrics.ass").is_file()
    report = validate_output(result.output_path, expected_duration=3.0, width=540, height=960)
    assert report["passed"], [c for c in report["checks"] if not c["passed"]]


def _bbox(mask: np.ndarray) -> tuple[int, int]:
    ys, xs = np.nonzero(mask)
    return int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


def test_short_rotated_anamorphic_and_odd_clips(demo_dir: Path, tmp_path: Path) -> None:
    # 0.8 s clip, odd size, 25 fps, 4:4:4, no audio → must loop for a 3 s shot
    short = make_clip(tmp_path / "short.mp4", "testsrc2=s=501x301:r=25", 0.8, pix_fmt="yuv444p")
    # 1.2 s clip for a 1.5 s shot → slowed down slightly
    slow = make_clip(tmp_path / "slow.mp4", "testsrc2=s=640x360:r=24", 1.2)
    # Anamorphic: 720×720 coded, SAR 16:9 → displayed 1280×720; the square is 90×160 coded = 160×160 displayed.
    anamorphic = make_clip(
        tmp_path / "sar.mp4", "color=c=black:s=720x720,drawbox=x=315:y=280:w=90:h=160:c=white:t=fill,setsar=16/9", 2.0
    )
    # Rotated: 640×360 coded with a 100×100 square, 90° display rotation → portrait.
    plain = make_clip(
        tmp_path / "plain.mp4", "color=c=black:s=640x360,drawbox=x=270:y=130:w=100:h=100:c=white:t=fill", 2.0
    )
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-display_rotation",
            "90",
            "-i",
            str(plain),
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
        timeout=60,
    )
    assert ffprobe(anamorphic, "-show_streams")["streams"][0]["sample_aspect_ratio"] == "16:9"

    shots = [
        RenderShot(clip_path=str(short), src_in=0.5, duration=3.0),
        RenderShot(clip_path=str(slow), src_in=0.4, duration=1.5),
        RenderShot(clip_path=str(anamorphic), src_in=0.0, duration=1.5, zoom_from=1.0, zoom_to=1.0),
        RenderShot(clip_path=str(rotated), src_in=0.0, duration=1.5, zoom_from=1.0, zoom_to=1.0),
    ]
    spec = small_spec(tmp_path, demo_dir, shots=shots, duration=7.5, color_grade="vivid")
    result = render_video(spec)
    output = Path(result.output_path)
    assert count_frames(output) == 225
    report = validate_output(output, expected_duration=7.5, width=540, height=960)
    assert report["passed"], [c for c in report["checks"] if not c["passed"]]
    plan_graph = (tmp_path / "work" / "filtergraph.txt").read_text(encoding="utf-8")
    assert "setpts=PTS/0.7" in plan_graph  # slowed: 1.15 s of source over 1.5 s

    # No distortion: squares stay square after cover-scaling (SAR and rotation honoured).
    for t in (5.25, 6.75):
        frame = frame_rgb(output, t, 540, 960)
        w, h = _bbox(frame.min(axis=2) > 160)
        assert w == pytest.approx(h, rel=0.06), (t, w, h)
    w_sar, _ = _bbox(frame_rgb(output, 5.25, 540, 960).min(axis=2) > 160)
    assert w_sar == pytest.approx(160 * 960 / 720, rel=0.06)  # cover factor for 1280×720 → 540×960


def test_missing_and_invalid_inputs_raise_render_error(demo_dir: Path, tmp_path: Path) -> None:
    bogus = tmp_path / "not a video.mp4"
    bogus.write_text("hello", encoding="utf-8")
    with pytest.raises(RenderError) as excinfo:
        render_video(small_spec(tmp_path, demo_dir, shots=[RenderShot(clip_path=str(bogus), src_in=0, duration=4.0)]))
    assert "読み込めません" in str(excinfo.value)
    with pytest.raises(RenderError):
        render_video(small_spec(tmp_path, demo_dir, audio_path=str(tmp_path / "missing.wav")))
    with pytest.raises(RenderError):
        render_video(small_spec(tmp_path, demo_dir, shots=[]))
    with pytest.raises(RenderError):
        render_video(small_spec(tmp_path, demo_dir, audio_start=500.0))


def test_ffmpeg_failure_reports_stderr_tail(demo_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_mod, "color_grade_filter", lambda grade_id: "nosuchfilter_xyz=1")
    with pytest.raises(RenderError) as excinfo:
        render_video(small_spec(tmp_path, demo_dir))
    error = excinfo.value
    assert "失敗" in str(error)
    assert "nosuchfilter_xyz" in error.stderr_tail or "No such filter" in error.stderr_tail
    assert not (tmp_path / "out.mp4").exists()
    assert not list(tmp_path.glob(".*.part.mp4"))


def test_progress_callback_exception_aborts_render(demo_dir: Path, tmp_path: Path) -> None:
    class Cancelled(Exception):
        pass

    def cancel(fraction: float, stage: str) -> None:
        if stage == "encode" and fraction > 0.1:
            raise Cancelled

    with pytest.raises(Cancelled):
        render_video(small_spec(tmp_path, demo_dir, duration=4.0), progress_cb=cancel)
    assert not (tmp_path / "out.mp4").exists()
    assert not list(tmp_path.glob(".*.part.mp4"))


# --------------------------------------------------------------------------- filter helpers


def test_escape_filter_value() -> None:
    assert escape_filter_value("/tmp/a b/c.ass") == "/tmp/a b/c.ass"
    # level 1 (option value) "\:" → level 2 (graph) escapes that backslash again
    assert escape_filter_value("/x:y/z.ass") == "/x\\\\:y/z.ass"
    assert escape_filter_value("/it's/[a],b;c") == "/it\\\\\\'s/\\[a\\]\\,b\\;c"


def test_split_filter_chain_respects_quotes() -> None:
    chain = "eq=contrast=1.1,curves=r='0/0 0.5/0.6 1/1':b='0/0,1/1',hue=s=0"
    assert split_filter_chain(chain) == ["eq=contrast=1.1", "curves=r='0/0 0.5/0.6 1/1':b='0/0,1/1'", "hue=s=0"]


def test_fallback_grades_are_valid_and_distinct(tmp_path: Path) -> None:
    source = tmp_path / "src.rgb"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x284",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gbrp",
            str(source),
        ],
        check=True,
        timeout=60,
    )
    outputs: dict[str, bytes] = {}
    for grade_id, grade in FALLBACK_COLOR_GRADES.items():
        assert render_mod._filter_is_valid(grade["ffmpeg_filter"]), grade_id
        cmd = [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gbrp",
            "-s",
            "160x284",
            "-i",
            str(source),
            "-vf",
            f"{grade['ffmpeg_filter']},format=gbrp",
            "-f",
            "rawvideo",
            "-",
        ]
        outputs[grade_id] = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
    assert len(set(outputs.values())) == len(outputs)
    mono = np.frombuffer(outputs["mono_noir"], dtype=np.uint8).reshape(3, -1).astype(int)
    assert np.abs(mono[0] - mono[1]).max() <= 1 and np.abs(mono[1] - mono[2]).max() <= 1


def test_expensive_grade_is_baked_into_an_equivalent_lut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain = (
        "colorbalance=rs=-0.07:bs=0.09:rh=0.08:bh=-0.07,eq=contrast=1.07:saturation=1.12,"
        "colortemperature=temperature=5600"
    )
    lut = tmp_path / "grade.cube"
    assert render_mod._bake_lut(chain, lut)
    assert lut.read_text(encoding="ascii").splitlines()[1] == "LUT_3D_SIZE 36"
    source = tmp_path / "src.gbrp"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x568",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gbrp",
            str(source),
        ],
        check=True,
        timeout=60,
    )

    def apply(vf: str) -> np.ndarray:
        cmd = [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gbrp",
            "-s",
            "320x568",
            "-i",
            str(source),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gbrp",
            "-",
        ]
        raw = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
        return np.frombuffer(raw, dtype=np.uint8).astype(int)

    direct = apply(f"{chain},format=gbrp")
    baked = apply(f"format=gbrpf32le,lut3d=file={escape_filter_value(str(lut))}:interp=tetrahedral,format=gbrp")
    original = np.fromfile(source, dtype=np.uint8).astype(int)
    assert np.abs(direct - original).mean() > 5  # the grade does something
    assert np.abs(direct - baked).mean() < 1.5

    steps = render_mod._grade_steps("custom_expensive", tmp_path)
    assert steps  # unknown id → natural (cheap) grade applied directly, no LUT needed
    assert all("lut3d" not in s.value for s in steps)

    # An expensive preset chain with a spatial filter in the middle: point runs are
    # baked, the vignette becomes a mask step, noise stays a plain filter.
    monkeypatch.setattr(render_mod, "color_grade_filter", lambda grade_id: f"{chain},vignette=PI/5,noise=alls=3")
    steps = render_mod._grade_steps("teal_orange", tmp_path)
    kinds = [(s.kind, s.value.split("=")[0]) for s in steps]
    assert kinds[0] == ("filters", "format") and "lut3d" in steps[0].value
    assert ("vignette", "vignette") in kinds
    assert any(kind == "filters" and value.startswith("noise") for kind, value in kinds)
    assert not any(kind == "grain" for kind, _ in kinds)  # the preset brings its own grain
