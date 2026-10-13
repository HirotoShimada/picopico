"""Motion of AI footage in the render: no repeated frames, smooth slow motion, no synthetic glide.

Veo-style clips are 24 fps and the output is 30 fps; the planner also slows shots down (to 0.75x).
Converting with ``fps`` repeated every 4th source frame (every ~3rd in a slowed shot), so content
stopped and started under a smooth Ken Burns move. The renderer now blends such sources
(``framerate``) and the planner gives generated clips only a slight centred zoom.
"""

from __future__ import annotations

import hashlib
import itertools
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app import schemas
from app.services import render, variation
from app.services.render import RenderShot, RenderSpec, render_video

FFMPEG = "ffmpeg"
W, H = 360, 640
BAR_PX_PER_FRAME = 4  # per source frame (even: 4:2:0 overlays land on even columns)
BAR_X0 = 20.0
BAR_W = 8


# --------------------------------------------------------------------------- helpers


def _bar_clip(path: Path, fps: float, seconds: float = 3.0) -> Path:
    """Black clip with a full-height white bar moving right BAR_PX_PER_FRAME per frame (lossless)."""
    rate = f"{fps:g}" if fps == int(fps) else "30000/1001"
    graph = (
        f"color=c=black:s={W}x{H}:r={rate}:d={seconds}[bg];color=c=white:s={BAR_W}x{H}:r={rate}:d={seconds}[bar];"
        f"[bg][bar]overlay=x='{BAR_X0:g}+n*{BAR_PX_PER_FRAME}':y=0:eval=frame,format=yuv420p"
    )
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", graph, "-c:v", "libx264", "-qp", "0",
           "-preset", "ultrafast", "-r", rate, str(path)]  # fmt: skip
    subprocess.run(cmd, check=True, timeout=120)
    return path


def _spec(shots: list[RenderShot], duration: float, **overrides: Any) -> RenderSpec:
    values: dict[str, Any] = {
        "shots": shots, "audio_path": "", "audio_start": 0.0, "duration": duration, "lyric_lines": [],
        "lyric_style": "simple", "text_style": {}, "color_grade": "natural", "output_path": "", "work_dir": "",
        "width": W, "height": H,
    }  # fmt: skip
    values.update(overrides)
    return RenderSpec(**values)


def _clip_info(path: str, fps: float, duration: float = 6.0) -> render._ClipInfo:
    return render._ClipInfo(path=path, duration=duration, width=720, height=1280, color_matrix="bt709", fps=fps)


def _chain_for(fps: float, speed: float) -> str:
    shot = RenderShot(clip_path="clip.mp4", src_in=0.0, duration=2.0, speed=speed, zoom_from=1.0, zoom_to=1.0)
    spec = _spec([shot], 2.0)
    plans, _ = render._plan_shots(spec, {"clip.mp4": _clip_info("clip.mp4", fps)})
    return render._shot_chain(0, plans[0], spec)


def _shot_gray(clip: Path, shot: RenderShot) -> tuple[np.ndarray, str]:
    """Every output frame of one shot, through the real ``_plan_shots`` + ``_shot_chain`` (gray)."""
    spec = _spec([shot], shot.duration)
    plans, _ = render._plan_shots(spec, {str(clip): render._probe_clip(str(clip))})
    chain = render._shot_chain(0, plans[0], spec)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-i", str(clip), "-filter_complex", chain, "-map", "[s0]",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W)
    assert len(frames) == plans[0].frames
    return frames, chain


def _bar_x(frame: np.ndarray) -> float:
    """Intensity-weighted x of the bar (a blend of two bars sits at their weighted mean)."""
    profile = frame[H // 4 : 3 * H // 4].astype(np.float64).mean(axis=0)
    profile = np.where(profile > 8, profile, 0.0)
    assert profile.sum() > 0
    return float((profile * np.arange(W)).sum() / profile.sum())


# --------------------------------------------------------------------------- filter graph contents


@pytest.mark.parametrize(
    ("fps", "speed", "blended"),
    [
        (24.0, 1.0, True),  # Veo / most AI clips: 24 → 30 fps
        (24.0, 0.8, True),  # a slowed AI shot: 19.2 source frames per second
        (24.0, 0.75, True),
        (25.0, 1.0, True),
        (30.0, 0.8, True),  # a slowed 30 fps upload
        (60.0, 0.4, True),  # 24 effective
        (30.0, 1.0, False),  # one source frame per output frame
        (30000 / 1001, 1.0, False),  # phone footage: a repeat every ~33 s beats blending every frame
        (29.0, 1.0, True),  # a repeat every second
        (30.0, 0.95, True),  # the planner's 0.95x: 1.5 repeats a second with ``fps``
        (30.0, 0.96, True),
        (30.0, 0.98, True),  # 0.6 a second
        (30.0, 0.99, False),  # one every 3.3 s
        (60.0, 1.0, False),  # faster sources: nearest frame (no blur)
        (24.0, 1.25, False),  # exactly 30 effective
        (0.0, 0.8, False),  # unknown rate: as before
    ],
)
def test_rate_conversion_blends_only_sources_slower_than_the_output(fps: float, speed: float, blended: bool) -> None:
    chain = _chain_for(fps, speed)
    assert ("framerate=fps=30,settb=1/30," in chain) is blended, chain  # same time base as ``fps`` gives
    assert ("fps=30," in chain.replace("framerate=fps=30,", "")) is not blended, chain
    assert chain.count("fps=30") == 1
    if abs(speed - 1.0) > 1e-3:
        assert f"setpts=PTS/{speed:g}," in chain  # the speed change comes first, then the conversion
        assert chain.index("setpts=PTS/") < chain.index("fps=30")
    # The exact frame count and the Ken Burns chain follow unchanged.
    assert "tpad=stop_mode=clone" in chain and "trim=end_frame=60," in chain and "perspective=" in chain


@pytest.mark.parametrize(("rate", "expected"), [(24.0, 24.0), (25.0, 25.0), (30000 / 1001, 30000 / 1001)])
def test_probe_reads_the_frame_rate(tmp_path: Path, rate: float, expected: float) -> None:
    clip = _bar_clip(tmp_path / "bar.mp4", rate, seconds=0.5)
    assert render._probe_clip(str(clip)).fps == pytest.approx(expected, rel=1e-6)


def test_frame_rate_parsing_falls_back_and_rejects_nonsense() -> None:
    assert render._frame_rate({"avg_frame_rate": "24/1", "r_frame_rate": "48/1"}) == 24.0
    assert render._frame_rate({"avg_frame_rate": "0/0", "r_frame_rate": "25/1"}) == 25.0
    assert render._frame_rate({"avg_frame_rate": "30000/1001"}) == pytest.approx(29.97, abs=1e-3)
    assert render._frame_rate({}) == 0.0
    assert render._frame_rate({"avg_frame_rate": "1/0", "r_frame_rate": "90000/1"}) == 0.0


# --------------------------------------------------------------------------- rendered motion


@pytest.mark.slow
@pytest.mark.parametrize("speed", [1.0, 0.8, 0.75])
def test_24fps_motion_advances_evenly_every_frame(tmp_path: Path, speed: float) -> None:
    # Regression: 24 → 30 fps with ``fps`` held every 4th source frame for two output frames (a
    # slowed shot about every 3rd), so a bar moving at constant speed jumped 4 px, 4 px, 0 px ...
    clip = _bar_clip(tmp_path / "bar24.mp4", 24.0)
    shot = RenderShot(clip_path=str(clip), src_in=0.0, duration=2.0, speed=speed, zoom_from=1.0, zoom_to=1.0)
    frames, chain = _shot_gray(clip, shot)
    assert "framerate=fps=30" in chain
    xs = np.array([_bar_x(f) for f in frames])
    steps = np.diff(xs)
    expected = BAR_PX_PER_FRAME * 24 * speed / 30  # px per output frame
    print(f"\nspeed {speed}: step {steps.mean():.3f} px (expected {expected:.3f}), min {steps.min():.3f}, "
          f"max {steps.max():.3f}")  # fmt: skip
    assert steps.min() > 0.5 * expected, "a frame repeats its predecessor"
    assert np.abs(steps - expected).max() < 0.35 * expected + 0.1, steps
    # The bar sits where the source's motion puts it at every output time (blend = linear in time),
    # starting on the source's first frame.
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-i", str(clip), "-frames:v", "1", "-f", "rawvideo",
           "-pix_fmt", "gray", "-"]  # fmt: skip
    first = np.frombuffer(subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout, np.uint8)
    ideal = _bar_x(first.reshape(H, W)) + BAR_PX_PER_FRAME * 24 * np.arange(len(frames)) / 30 * speed
    assert np.abs(xs - ideal).max() < 0.3, np.abs(xs - ideal).max()


@pytest.mark.slow
def test_30fps_footage_is_passed_through_frame_for_frame(tmp_path: Path) -> None:
    clip = _bar_clip(tmp_path / "bar30.mp4", 30.0)
    shot = RenderShot(clip_path=str(clip), src_in=0.0, duration=2.0, zoom_from=1.0, zoom_to=1.0)
    frames, chain = _shot_gray(clip, shot)
    assert "framerate" not in chain and "fps=30," in chain
    steps = np.diff([_bar_x(f) for f in frames])
    assert np.abs(steps - BAR_PX_PER_FRAME).max() < 0.05  # every source frame once, in order


@pytest.mark.slow
def test_shot_chain_output_is_deterministic(tmp_path: Path) -> None:
    clip = _bar_clip(tmp_path / "bar24.mp4", 24.0)
    shot = RenderShot(clip_path=str(clip), src_in=0.2, duration=1.5, speed=0.8, zoom_from=1.0, zoom_to=1.02)
    first, _ = _shot_gray(clip, shot)
    second, _ = _shot_gray(clip, shot)
    assert hashlib.sha256(first.tobytes()).digest() == hashlib.sha256(second.tobytes()).digest()


@pytest.mark.slow
def test_static_punch_in_holds_still(tmp_path: Path) -> None:
    # A generated clip shown again in the same video gets a static tighter framing: with a still
    # source, every output frame is the same (no synthetic glide).
    still = tmp_path / "still.mp4"
    graph = f"color=c=black:s={W}x{H}:r=24:d=2,drawbox=x=100:y=200:w=20:h=20:c=white:t=fill,format=yuv420p"
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", graph, "-c:v", "libx264", "-qp", "0",
                    "-preset", "ultrafast", str(still)], check=True, timeout=60)  # fmt: skip
    shot = RenderShot(clip_path=str(still), src_in=0.0, duration=1.5, zoom_from=1.15, zoom_to=1.15)
    frames, _ = _shot_gray(still, shot)
    assert int(np.abs(frames.astype(np.int16) - frames[0]).max()) <= 1
    _, xs = np.nonzero(frames[0] > 128)
    assert xs.max() - xs.min() + 1 == pytest.approx(20 * 1.15, abs=1.5)  # framed 15 % tighter


@pytest.mark.slow
@pytest.mark.parametrize("transition", ["cut", "crossfade"])
def test_rendered_video_of_slowed_24fps_shots_has_no_repeated_frames(
    demo_dir: Path, tmp_path: Path, transition: str
) -> None:
    clip = _bar_clip(tmp_path / "bar24.mp4", 24.0, seconds=4.0)
    shots = [
        RenderShot(clip_path=str(clip), src_in=0.1, duration=2.5, speed=0.82, zoom_from=1.0, zoom_to=1.01),
        RenderShot(clip_path=str(clip), src_in=0.1, duration=2.5, speed=0.77, zoom_from=1.01, zoom_to=1.0),
    ]
    spec = _spec(shots, 5.0, audio_path=str(demo_dir / "demo_song.wav"), audio_start=24.0, transition=transition,
                 output_path=str(tmp_path / "out.mp4"), work_dir=str(tmp_path / "work"), preset="ultrafast",
                 color_grade="vivid")  # fmt: skip
    result = render_video(spec)
    graph = (tmp_path / "work" / "filtergraph.txt").read_text(encoding="utf-8")
    assert graph.count("framerate=fps=30") == 2 and "setpts=PTS/0.82," in graph and "setpts=PTS/0.77," in graph
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-i", result.output_path, "-map", "0:v:0", "-f", "rawvideo",
           "-pix_fmt", "gray", "-"]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W)
    assert len(frames) == 150  # exact frame count (crossfade compensated)
    diffs = np.abs(np.diff(frames.astype(np.int16), axis=0)).mean(axis=(1, 2))
    # Each shot's bar moves 2.5-2.6 px per output frame; a repeated source frame would leave only
    # the 1 % zoom's sub-pixel change (and an encoder flicker) between two frames.
    print(f"\n{transition}: frame difference min {diffs.min():.3f}, median {np.median(diffs):.3f}")
    assert diffs.min() > 0.3 * float(np.median(diffs)), np.argsort(diffs)[:5]


@pytest.mark.slow
def test_blended_and_plain_shots_crossfade_into_each_other(demo_dir: Path, tmp_path: Path) -> None:
    # Regression: ``framerate`` keeps a finer time base than ``fps`` (1/61440 vs 1/30 for a 24 fps
    # MP4) and ``xfade`` refuses inputs on different time bases, so a 24 fps shot next to a 30 fps
    # one failed to render with a crossfade.
    slow, plain = _bar_clip(tmp_path / "bar24.mp4", 24.0), _bar_clip(tmp_path / "bar30.mp4", 30.0)
    shots = [RenderShot(clip_path=str(c), src_in=0.0, duration=1.5, zoom_from=1.0, zoom_to=1.0)
             for c in (slow, plain, slow)]  # fmt: skip
    spec = _spec(shots, 4.5, audio_path=str(demo_dir / "demo_song.wav"), audio_start=24.0, transition="crossfade",
                 output_path=str(tmp_path / "out.mp4"), work_dir=str(tmp_path / "work"),
                 preset="ultrafast")  # fmt: skip
    result = render_video(spec)
    graph = (tmp_path / "work" / "filtergraph.txt").read_text(encoding="utf-8")
    assert graph.count("framerate=fps=30,settb=1/30,") == 2 and graph.count("xfade=") == 2
    data = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
         "-of", "csv=p=0", result.output_path], capture_output=True, check=True, timeout=60, text=True,
    ).stdout  # fmt: skip
    assert int(data.strip()) == 135


# --------------------------------------------------------------------------- plan → render


def _settings(**overrides: Any) -> dict[str, Any]:
    base = schemas.GenerationSettings(provider="mock", model="mock-v1").model_dump()
    base.update(overrides)
    return base


def _analysis(duration: float = 60.0, bpm: float = 120.0) -> dict[str, Any]:
    beats = [round(0.25 + i * 60 / bpm, 3) for i in range(int((duration - 0.25) * bpm / 60) + 1)]
    sections = [
        {"start": 0.0, "end": 20.0, "label": "verse", "energy": 0.5},
        {"start": 20.0, "end": 40.0, "label": "chorus", "energy": 0.85},
        {"start": 40.0, "end": duration, "label": "verse", "energy": 0.5},
    ]
    return {"duration": duration, "bpm": bpm, "beats": beats, "downbeats": beats[::4], "sections": sections}


def _storyboard(n: int = 6) -> dict[str, Any]:
    return {"scenes": [{"id": f"s{i:02d}", "prompt_en": f"scene {i}, natural light", "section": "chorus"}
                       for i in range(1, n + 1)]}  # fmt: skip


def _plan(**settings: Any) -> dict[str, Any]:
    library = settings.get("provider") == "library"
    uploads = [{"id": f"u{i}", "duration": 9.0, "source": "upload"} for i in range(4)]
    return variation.plan_batch(
        analysis=_analysis(), lyric_lines=[], settings=_settings(**settings),
        storyboard=None if library else _storyboard(), segment=(20.0, 40.0), provider_clip_duration=6.0,
        existing_clips=uploads if library else [], seed=7,
    )  # fmt: skip


def test_generated_plans_have_slight_centred_moves_and_render_with_blending() -> None:
    result = _plan(count=4, duration_sec=20, max_new_clips=3, reuse="economy")
    assert result == _plan(count=4, duration_sec=20, max_new_clips=3, reuse="economy")  # deterministic
    shots = [s for v in result["videos"] for s in v["shots"]]
    assert all(s["pan"] == "center" for s in shots)
    for video in result["videos"]:
        seen: set[str] = set()
        for shot in video["shots"]:
            low, high = sorted((shot["zoom_from"], shot["zoom_to"]))
            if shot["clip_key"] in seen:  # in-video repeat: a static punch-in
                assert low == high and variation.PUNCH_IN_ZOOM[0] <= low <= variation.PUNCH_IN_ZOOM[1]
            elif low == high:  # footage back from an earlier video: a static crop, then a static punch-in
                punch = variation.PUNCH_IN_ZOOM[0] <= low <= variation.PUNCH_IN_ZOOM[1]
                assert low == variation.GENERATED_CROP_ZOOM or punch, shot
            else:
                assert low == 1.0 and 0 < high - low <= variation.GENERATED_MAX_ZOOM_DELTA + 1e-9, shot
            seen.add(shot["clip_key"])
    assert any(s["speed"] < 1.0 for s in shots), "a 20 s video from 3 clips of 6 s needs slowed shots"

    # What the renderer makes of it with 24 fps clips: speeds kept, every shot blended to 30 fps.
    specs = {s["key"]: s for s in result["clip_specs"]}
    for video in result["videos"]:
        rshots = [
            RenderShot(clip_path=s["clip_key"], src_in=s["src_in"], duration=s["duration"], speed=s["speed"],
                       pan=s["pan"], zoom_from=s["zoom_from"], zoom_to=s["zoom_to"])
            for s in video["shots"]
        ]  # fmt: skip
        spec = _spec(rshots, video["duration"], width=1080, height=1920, transition=video["transition"])
        clips = {k: _clip_info(k, 24.0, duration=spec_["duration"]) for k, spec_ in specs.items()}
        plans, _ = render._plan_shots(spec, clips)
        for plan, shot in zip(plans, video["shots"], strict=True):
            assert plan.speed == pytest.approx(shot["speed"]) and not plan.loop
            assert plan.src_in == pytest.approx(shot["src_in"])
            # No headroom zoom added: a centred move needs none.
            assert (plan.zoom_from, plan.zoom_to) == (shot["zoom_from"], shot["zoom_to"])
            chain = render._shot_chain(0, plan, spec)
            assert "framerate=fps=30," in chain


def test_library_plans_keep_the_full_camera_moves() -> None:
    result = _plan(provider="library", model=None, count=6, duration_sec=20)
    assert result == _plan(provider="library", model=None, count=6, duration_sec=20)
    shots = [s for v in result["videos"] for s in v["shots"]]
    deltas = [abs(s["zoom_to"] - s["zoom_from"]) for s in shots]
    assert all(0.03 - 1e-9 <= d <= 0.12 + 1e-9 for d in deltas), deltas
    assert len({s["pan"] for s in shots}) >= 3
    assert max(deltas) > 3 * variation.GENERATED_MAX_ZOOM_DELTA


def test_gentle_and_full_moves_leave_the_rest_of_the_plan_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gentle moves draw the same random numbers as the full ones: forcing the full moves onto a
    # generated plan changes only zoom/pan, never the clips, in-points, speeds or cuts.
    gentle = _plan(count=3, duration_sec=20, max_new_clips=3)
    original = variation._ken_burns
    monkeypatch.setattr(variation, "_ken_burns", lambda *a, gentle=False, **k: original(*a, **k))
    full = _plan(count=3, duration_sec=20, max_new_clips=3)
    keys = ("clip_key", "src_in", "duration", "speed", "hflip")
    for a, b in itertools.zip_longest(gentle["videos"], full["videos"]):
        assert [[s[k] for k in keys] for s in a["shots"]] == [[s[k] for k in keys] for s in b["shots"]]
    moved = [s for v in full["videos"] for s in v["shots"]]
    assert any(s["pan"] != "center" for s in moved) or max(abs(s["zoom_to"] - s["zoom_from"]) for s in moved) > 0.02


# --------------------------------------------------------------------------- review fixes (2026-10-13)


def _solid_rgb_clip(path: Path, rgb: tuple[int, int, int], fps: float = 24.0) -> Path:
    """A 720×1280 RGB (QuickTime Animation, rgb24) clip of one colour: an upload ``framerate`` cannot take as is."""
    colour = "0x" + "".join(f"{c:02X}" for c in rgb)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=720x1280:r={fps:g}:d=1",
           "-vf", "format=rgb24", "-c:v", "qtrle", str(path)]  # fmt: skip
    subprocess.run(cmd, check=True, timeout=120)
    return path


def _shot_rgb(clip: Path, shot: RenderShot, chain_fix: Any = None) -> np.ndarray:
    spec = _spec([shot], shot.duration)
    plans, _ = render._plan_shots(spec, {str(clip): render._probe_clip(str(clip))})
    chain = render._shot_chain(0, plans[0], spec)
    if chain_fix is not None:
        chain = chain_fix(chain)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-i", str(clip), "-filter_complex", chain, "-map", "[s0]",
           "-frames:v", "3", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W, 3)


@pytest.mark.parametrize("speed", [1.0, 0.8])
def test_rgb_uploads_keep_their_colour_on_the_blend_path(tmp_path: Path, speed: float) -> None:
    """Regression: ``framerate`` made FFmpeg auto-convert RGB → YUV with BT.601, later read as BT.709:
    (32, 192, 64) came out as (19, 168, 58); the ``fps`` path gave (30, 190, 62)."""
    clip = _solid_rgb_clip(tmp_path / "anim.mov", (32, 192, 64))
    info = render._probe_clip(str(clip))
    assert info.pix_fmt == "rgb24" and info.color_matrix == "bt709" and info.fps == 24.0
    shot = RenderShot(clip_path=str(clip), src_in=0.0, duration=1.0, speed=speed, zoom_from=1.0, zoom_to=1.0)
    blended = _shot_rgb(clip, shot)
    nearest = _shot_rgb(clip, shot, lambda chain: chain.replace(
        "scale=in_color_matrix=bt709:out_color_matrix=bt709,format=yuv444p,framerate=fps=30,settb=1/30", "fps=30"))
    chain = render._shot_chain(0, render._plan_shots(_spec([shot], 1.0), {str(clip): info})[0][0], _spec([shot], 1.0))
    assert "format=yuv444p,framerate=fps=30" in chain  # the explicit, matrix-controlled conversion
    mean_blend = blended[1].reshape(-1, 3).mean(axis=0)
    mean_fps = nearest[1].reshape(-1, 3).mean(axis=0)
    assert np.abs(mean_blend - mean_fps).max() <= 1.5, (mean_blend, mean_fps)
    assert np.abs(mean_blend - np.array([32, 192, 64])).max() <= 4.0, mean_blend


def test_yuv_sources_get_no_extra_conversion() -> None:
    assert "yuv444p" not in _chain_for(24.0, 1.0)  # Veo clips (yuv420p): the chain is unchanged


@pytest.mark.slow
@pytest.mark.parametrize("speed", [0.95, 0.96])
def test_30fps_uploads_slowed_a_little_repeat_no_frame(tmp_path: Path, speed: float) -> None:
    """Regression: 30 fps × 0.95 stayed on ``fps`` (28.5 is not < 30 × 0.95) and repeated 4 frames in 2.6 s."""
    clip = _bar_clip(tmp_path / "bar30.mp4", 30.0, seconds=4.0)
    shot = RenderShot(clip_path=str(clip), src_in=0.0, duration=2.6, speed=speed, zoom_from=1.0, zoom_to=1.0)
    frames, chain = _shot_gray(clip, shot)
    assert "framerate=fps=30" in chain
    steps = np.diff([_bar_x(f) for f in frames])
    assert (np.abs(steps) < 0.5).sum() == 0, steps


@pytest.mark.slow
def test_looped_short_clips_show_no_double_exposure_at_the_seam(tmp_path: Path) -> None:
    """Regression: blending across the loop seam of a short 24 fps upload showed both bars in one frame."""
    clip = _bar_clip(tmp_path / "bar24s.mp4", 24.0, seconds=1.5)
    shot = RenderShot(clip_path=str(clip), src_in=0.0, duration=4.0, speed=1.0, zoom_from=1.0, zoom_to=1.0)
    spec = _spec([shot], 4.0)
    plans, _ = render._plan_shots(spec, {str(clip): render._probe_clip(str(clip))})
    plan = plans[0]
    assert plan.loop
    chain = render._shot_chain(0, plan, spec)
    assert "framerate" not in chain and "fps=30" in chain
    # The same input arguments render_video uses for a looped shot.
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-stream_loop", "-1", "-t", f"{plan.frames / 30 * plan.speed + 0.5:.3f}",
           "-i", str(clip), "-filter_complex", chain, "-map", "[s0]", "-f", "rawvideo", "-pix_fmt", "gray", "-"]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W)
    assert len(frames) == plan.frames
    for index, frame in enumerate(frames):
        lit = frame[H // 4 : 3 * H // 4].astype(np.float64).mean(axis=0) > 8
        lobes = int(np.sum(np.diff(lit.astype(int)) == 1) + int(lit[0]))
        assert lobes == 1, f"frame {index} shows {lobes} bars"
