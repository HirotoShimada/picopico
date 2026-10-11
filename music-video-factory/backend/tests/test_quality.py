"""Tests for rendered-output validation (app.services.quality)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.quality import mp4_top_level_boxes, validate_output

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def make_video(
    path: Path,
    *,
    size: str = "540x960",
    fps: int = 30,
    duration: float = 2.0,
    video: str = "testsrc2",
    pix_fmt: str = "yuv420p",
    vcodec: str = "libx264",
    audio: str | None = "aac",
    faststart: bool = True,
    audio_duration: float | None = None,
) -> Path:
    joiner = ":" if "=" in video else "="
    source = f"{video}{joiner}s={size}:r={fps}:d={duration}"
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", source]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={audio_duration or duration}"]
    cmd += ["-c:v", vcodec, "-pix_fmt", pix_fmt]
    if vcodec == "libx264":
        cmd += ["-preset", "ultrafast"]
    if audio:
        cmd += ["-c:a", audio, "-ac", "2"]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", "mp4", str(path)]
    subprocess.run(cmd, check=True, timeout=120)
    return path


def checks_by_name(report: dict) -> dict[str, dict]:
    return {check["name"]: check for check in report["checks"]}


def test_compliant_full_size_video_passes(tmp_path: Path) -> None:
    path = make_video(tmp_path / "good.mp4", size="1080x1920", duration=2.0)
    report = validate_output(path, expected_duration=2.0)
    checks = checks_by_name(report)
    assert report["passed"], [c for c in report["checks"] if not c["passed"]]
    assert set(checks) == {
        "container",
        "video_codec",
        "pix_fmt",
        "resolution",
        "fps",
        "duration",
        "audio_codec",
        "audio_duration",
        "faststart",
        "black_frames",
        "loudness",
    }
    assert checks["resolution"]["value"] == "1080x1920"
    assert checks["faststart"]["value"] == "moov < mdat"
    assert report["black_ratio"] == 0.0
    assert isinstance(report["loudness_lufs"], float) and -40 < report["loudness_lufs"] < -5
    assert all(set(c) == {"name", "passed", "value", "expected"} for c in report["checks"])


def test_missing_faststart_is_detected(tmp_path: Path) -> None:
    path = make_video(tmp_path / "slow start.mp4", faststart=False)
    report = validate_output(path, expected_duration=2.0, width=540, height=960)
    checks = checks_by_name(report)
    assert not report["passed"]
    assert not checks["faststart"]["passed"] and checks["faststart"]["value"] == "mdat < moov"
    assert all(c["passed"] for name, c in checks.items() if name != "faststart")
    kinds = [kind for kind, _, _ in mp4_top_level_boxes(path)]
    assert kinds[0] == "ftyp" and kinds.index("mdat") < kinds.index("moov")


@pytest.mark.parametrize(
    ("kwargs", "failing"),
    [
        ({"fps": 25}, {"fps"}),
        ({"size": "720x1280"}, {"resolution"}),
        ({"pix_fmt": "yuv444p"}, {"pix_fmt"}),
        ({"vcodec": "mpeg4"}, {"video_codec"}),
        ({"audio": None}, {"audio_codec", "audio_duration"}),
        ({"audio": "libmp3lame"}, {"audio_codec"}),
        ({"duration": 2.5, "audio_duration": 2.5}, {"duration", "audio_duration"}),
        ({"audio_duration": 1.0}, {"audio_duration"}),
        ({"video": "color=c=black"}, {"black_frames"}),
    ],
)
def test_each_failure_is_reported(tmp_path: Path, kwargs: dict, failing: set[str]) -> None:
    path = make_video(tmp_path / "bad.mp4", **kwargs)
    report = validate_output(path, expected_duration=2.0, width=540, height=960)
    failed = {c["name"] for c in report["checks"] if not c["passed"]}
    assert not report["passed"]
    assert failed == failing, report["checks"]
    if "black_frames" in failing:
        assert report["black_ratio"] > 0.9


def test_loudness_is_informational(tmp_path: Path) -> None:
    path = make_video(tmp_path / "quiet.mp4")
    report = validate_output(path, expected_duration=2.0, width=540, height=960)
    loudness = checks_by_name(report)["loudness"]
    assert loudness["passed"] is True
    assert loudness["value"] == pytest.approx(report["loudness_lufs"], abs=0.06)


def test_unreadable_or_missing_file(tmp_path: Path) -> None:
    missing = validate_output(tmp_path / "nope.mp4", expected_duration=15.0)
    assert missing["passed"] is False and missing["checks"][0]["name"] == "readable"
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"not a video at all")
    report = validate_output(garbage, expected_duration=15.0)
    assert report["passed"] is False and report["loudness_lufs"] is None


def test_top_level_box_parser_handles_large_and_truncated_boxes(tmp_path: Path) -> None:
    import struct

    path = tmp_path / "boxes.mp4"
    data = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x02\x00"
    data += struct.pack(">I4sQ", 1, b"moov", 24) + b"\x00" * 8  # 64-bit size
    data += struct.pack(">I4s", 0, b"mdat") + b"\x00" * 10  # size 0 = to end of file
    path.write_bytes(data)
    assert [(k, o) for k, o, _ in mp4_top_level_boxes(path)] == [("ftyp", 0), ("moov", 16), ("mdat", 40)]
