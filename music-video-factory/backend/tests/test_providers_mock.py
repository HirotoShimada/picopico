"""Mock provider: fast, deterministic, clearly labelled FFmpeg placeholder clips."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.services.errors import ProviderError
from app.services.presets import WORLD_PRESETS
from app.services.providers.base import ClipRequest, ProviderCancelled
from app.services.providers.mock import MOCK_FPS, MockProvider, build_look


def _request(**overrides: Any) -> ClipRequest:
    preset = overrides.pop("preset", "cinematic")
    scene = {"preset": preset, "palette": WORLD_PRESETS[preset]["palette"], "scene_type": "walking"}
    scene.update(overrides.pop("scene", {}))
    values: dict[str, Any] = {
        "prompt": "a fictional woman walking through a rainy city at dusk",
        "negative_prompt": "text, watermark",
        "duration": 5.0,
        "model": "mock-v1",
        "seed": 1234,
        "scene": scene,
    }
    values.update(overrides)
    return ClipRequest(**values)


def _ffprobe(path: str) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, check=True, timeout=60,
    ).stdout  # fmt: skip
    return json.loads(out)


def _frame(path: str, at: float, out: Path) -> Image.Image:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at), "-i", path, "-frames:v", "1", str(out)],
        check=True, timeout=60,
    )  # fmt: skip
    return Image.open(out).convert("L")


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_generates_valid_vertical_h264_clip_quickly(tmp_path: Path) -> None:
    provider = MockProvider()
    stages: list[str] = []
    started = time.monotonic()
    result = provider.generate(_request(), str(tmp_path / "clip.mp4"), on_progress=lambda f, s: stages.append(s))
    elapsed = time.monotonic() - started

    info = _ffprobe(result.path)
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    assert len(video) == 1 and not [s for s in info["streams"] if s["codec_type"] == "audio"]
    stream = video[0]
    assert (stream["codec_name"], stream["pix_fmt"], stream["width"], stream["height"]) == ("h264", "yuv420p", 1080, 1920)
    assert stream["r_frame_rate"] == f"{MOCK_FPS}/1"
    assert int(stream["nb_read_frames"]) == 5 * MOCK_FPS
    assert float(info["format"]["duration"]) == pytest.approx(5.0, abs=1 / MOCK_FPS)
    assert (result.width, result.height, result.duration, result.cost_usd) == (1080, 1920, 5.0, 0.0)
    assert result.model == "mock-v1" and result.external_job_id is None
    assert stages[-1] == "done"
    assert not list(tmp_path.glob(".*part*"))
    # ~2 s on the 4-core dev container; generous bound for loaded CI machines.
    assert elapsed < 15, f"mock generation took {elapsed:.1f}s"


def test_duration_rounds_to_frames_and_respects_model_limits(tmp_path: Path) -> None:
    provider = MockProvider()
    result = provider.generate(_request(duration=1.3), str(tmp_path / "short.mp4"))
    assert result.duration == pytest.approx(round(1.3 * MOCK_FPS) / MOCK_FPS, abs=1e-3)
    assert int(_ffprobe(result.path)["streams"][0]["nb_read_frames"]) == round(1.3 * MOCK_FPS)
    assert provider.clip_duration_for("mock-v1", 5.0) == 5.0
    assert provider.clip_duration_for("mock-v1", 99.0) == 20.0


def test_output_is_deterministic_per_prompt_and_seed(tmp_path: Path) -> None:
    provider = MockProvider()
    first = provider.generate(_request(duration=1.5), str(tmp_path / "a.mp4"))
    again = provider.generate(_request(duration=1.5), str(tmp_path / "b.mp4"))
    other_seed = provider.generate(_request(duration=1.5, seed=99), str(tmp_path / "c.mp4"))
    other_prompt = provider.generate(_request(duration=1.5, prompt="a quiet lake at dawn"), str(tmp_path / "d.mp4"))
    assert _sha(first.path) == _sha(again.path)
    assert first.raw["look"] == again.raw["look"]
    assert _sha(other_seed.path) != _sha(first.path)
    assert other_seed.raw["look"] != first.raw["look"]
    assert other_prompt.raw["look"] != first.raw["look"]


def test_look_follows_scene_palette_and_type() -> None:
    night = build_look(_request(preset="urban_night", scene={"scene_type": "street_flow"}), 540, 960)
    sky = build_look(_request(preset="dreamy", scene={"scene_type": "sky_drift"}), 540, 960)
    assert night.composition == "bokeh" and len(night.orbs) >= 6
    assert sky.composition == "sky"
    assert night.palette != sky.palette
    # Palette colours are normalised so nothing is pitch black or blown out.
    for look in (night, sky, build_look(_request(preset="dark"), 540, 960)):
        for color in look.palette:
            channels = [int(color[i : i + 2], 16) for i in (0, 2, 4)]
            assert max(channels) >= 15 and min(channels) <= 235
    without_scene = build_look(_request(scene={"palette": None, "scene_type": None, "preset": None}), 540, 960)
    assert len(without_scene.palette) == 3


def test_frames_are_not_black_and_carry_the_mock_tag(tmp_path: Path) -> None:
    result = MockProvider().generate(_request(preset="dark", duration=1.0), str(tmp_path / "dark.mp4"))
    frame = _frame(result.path, 0.5, tmp_path / "f.png")
    histogram = frame.histogram()
    mean = sum(i * n for i, n in enumerate(histogram)) / sum(histogram)
    assert mean > 18, f"frame too dark (mean luma {mean:.1f})"
    # MOCK tag: dark box with bright text, top-left inside the Stories safe area (y >= 270).
    tag = frame.crop((64, 307, 64 + 132, 307 + 52))
    low, high = tag.getextrema()
    assert high >= 200 and low <= 40
    assert max(frame.crop((300, 1300, 800, 1700)).getextrema()) < 200  # no text elsewhere


def test_other_aspect_ratios_and_image_input(tmp_path: Path) -> None:
    provider = MockProvider()
    wide = provider.generate(_request(duration=1.0, aspect_ratio="16:9"), str(tmp_path / "wide.mp4"))
    assert (wide.width, wide.height) == (1920, 1080)
    stream = _ffprobe(wide.path)["streams"][0]
    assert (stream["width"], stream["height"]) == (1920, 1080)

    image = tmp_path / "still.png"
    Image.new("RGB", (400, 600), (200, 120, 40)).save(image)
    from_image = provider.generate(_request(duration=1.0, image_path=str(image)), str(tmp_path / "i2v.mp4"))
    frame = _frame(from_image.path, 0.5, tmp_path / "i.png").convert("L")
    assert frame.size == (1080, 1920)

    with pytest.raises(ProviderError) as info:
        provider.generate(_request(aspect_ratio="4:5"), str(tmp_path / "x.mp4"))
    assert info.value.retryable is False
    with pytest.raises(ProviderError):
        provider.generate(_request(image_path=str(tmp_path / "missing.png")), str(tmp_path / "y.mp4"))
    with pytest.raises(ProviderError) as unknown:
        provider.generate(_request(model="mock-v9"), str(tmp_path / "z.mp4"))
    assert unknown.value.code == "provider_unknown_model"


def test_cancel_and_timeout(tmp_path: Path) -> None:
    provider = MockProvider()
    with pytest.raises(ProviderCancelled):
        provider.generate(_request(), str(tmp_path / "a.mp4"), cancel_check=lambda: True)

    calls = {"n": 0}

    def cancel_after_start() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # first check (before FFmpeg starts) passes

    with pytest.raises(ProviderCancelled):
        provider.generate(_request(duration=20), str(tmp_path / "b.mp4"), cancel_check=cancel_after_start)
    with pytest.raises(ProviderError) as info:
        provider.generate(_request(duration=20), str(tmp_path / "c.mp4"), timeout=0.05)
    assert info.value.retryable is True and info.value.code == "provider_timeout"
    assert not list(tmp_path.iterdir()), "partial outputs must be removed"


def test_info_and_readiness() -> None:
    provider = MockProvider()
    assert provider.is_configured() == (True, None)
    info = provider.info()
    assert info["kind"] == "mock" and info["configured"] and info["available"]
    assert info["models"][0]["price_per_second_usd"] == 0 and info["models"][0]["verified"] is True
    assert "MOCK" in info["commercial_use_note"]
    missing = MockProvider(ffmpeg_bin="ffmpeg-does-not-exist")
    assert missing.is_configured()[0] is True
    assert missing.is_available()[0] is False and "FFmpeg" in (missing.info()["reason"] or "")


def test_generates_from_a_real_planner_clip_spec(tmp_path: Path) -> None:
    from app.services.storyboard import generate_storyboard
    from app.services.variation import plan_batch

    analysis = {"duration": 30.0, "bpm": 120.0, "beats": [0.5 * i for i in range(60)], "downbeats": [2.0 * i for i in range(15)]}
    storyboard = generate_storyboard(title="t", artist_name="a", mood="夜", purpose="", lyrics=[], analysis=analysis,
                                     preset_id="urban_night", custom_world=None, n_scenes=4, seed=2, use_claude=False)  # fmt: skip
    provider = MockProvider()
    settings = {"provider": "mock", "count": 1, "duration_sec": 5, "reuse": "economy", "preset": "urban_night"}
    plan = plan_batch(analysis=analysis, lyric_lines=[], settings=settings, storyboard=storyboard, segment=None,
                      provider_clip_duration=provider.clip_duration_for("mock-v1", 2.0), existing_clips=[], seed=1)  # fmt: skip
    spec = plan["clip_specs"][0]
    request = ClipRequest(prompt=spec["prompt"], negative_prompt=spec["negative_prompt"], duration=spec["duration"],
                          model=provider.default_model(), seed=spec["seed"], scene=spec["hints"])  # fmt: skip
    result = provider.generate(request, str(tmp_path / "spec.mp4"))
    assert result.duration == pytest.approx(2.0) and result.raw["look"]["palette"]
    assert spec["hints"]["preset"] == "urban_night"
