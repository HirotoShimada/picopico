"""``app.services.media``: probe normalisation, validation, thumbnails, filenames."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.services.errors import MediaValidationError
from app.services.media import (
    file_extension,
    make_thumbnail,
    probe,
    sanitize_filename,
    validate_audio_upload,
    validate_video_upload,
)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, timeout=120)


def test_probe_demo_audio_and_video(demo_dir: Path) -> None:
    song = probe(demo_dir / "demo_song.mp3")
    assert song["has_audio"] and not song["has_video"]
    assert song["duration"] == pytest.approx(60.0, abs=0.2)
    assert song["codecs"] == {"video": None, "audio": "mp3"}
    assert song["sample_rate"] in (44100, 48000) and song["channels"] in (1, 2)
    assert song["width"] is None and song["fps"] is None and song["rotation"] == 0

    clip = probe(demo_dir / "clip_01.mp4")
    assert clip["has_video"]
    assert (clip["width"], clip["height"]) == (720, 1280)
    assert clip["fps"] == pytest.approx(24.0)
    assert clip["codecs"]["video"] == "h264"
    assert clip["duration"] == pytest.approx(6.0, abs=0.05)
    assert "mp4" in clip["format_name"]


def test_probe_ignores_cover_art(demo_dir: Path, tmp_path: Path) -> None:
    cover = tmp_path / "cover.png"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(cover)
    with_cover = tmp_path / "with_cover.mp3"
    _ffmpeg(
        "-i", str(demo_dir / "demo_song.mp3"), "-i", str(cover),
        "-map", "0:a", "-map", "1:v", "-c", "copy", "-id3v2_version", "3",
        "-disposition:v", "attached_pic", str(with_cover),
    )  # fmt: skip
    info = probe(with_cover)
    assert info["has_audio"] and not info["has_video"]
    validate_audio_upload(with_cover, "with_cover.mp3")


def test_probe_reports_rotation_and_display_size(demo_dir: Path, tmp_path: Path) -> None:
    rotated = tmp_path / "rotated.mp4"
    _ffmpeg("-display_rotation", "90", "-i", str(demo_dir / "clip_02.mp4"), "-c", "copy", str(rotated))
    info = probe(rotated)
    assert info["rotation"] in (90, 270)
    assert (info["coded_width"], info["coded_height"]) == (1280, 720)
    assert (info["width"], info["height"]) == (720, 1280)


def test_probe_rejects_non_media(tmp_path: Path) -> None:
    text = tmp_path / "notes.mp4"
    text.write_text("not a video\n")
    with pytest.raises(MediaValidationError):
        probe(text)
    with pytest.raises(MediaValidationError):
        probe(tmp_path / "missing.mp4")


def test_validate_uploads(demo_dir: Path, tmp_path: Path) -> None:
    assert validate_audio_upload(demo_dir / "demo_song.wav", "song.wav")["has_audio"]
    assert validate_video_upload(demo_dir / "clip_03.mp4", "clip.mp4")["has_video"]

    with pytest.raises(MediaValidationError, match="映像ストリーム"):
        validate_video_upload(demo_dir / "demo_song.mp3", "song.mp4")
    with pytest.raises(MediaValidationError, match="対応していない動画形式"):
        validate_video_upload(demo_dir / "clip_03.mp4", "clip.gif")
    with pytest.raises(MediaValidationError, match="音声ストリーム"):
        validate_audio_upload(demo_dir / "clip_02.mp4", "clip.m4a")

    png = tmp_path / "image.mp4"
    Image.new("RGB", (32, 32)).save(png, format="PNG")
    with pytest.raises(MediaValidationError):
        validate_video_upload(png, "image.mp4")

    tiny = tmp_path / "tiny.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.4", "-c:v", "libx264", str(tiny))
    with pytest.raises(MediaValidationError, match="短すぎ"):
        validate_video_upload(tiny, "tiny.mp4")


def test_make_thumbnail(demo_dir: Path, tmp_path: Path) -> None:
    out = make_thumbnail(demo_dir / "clip_03.mp4", tmp_path / "thumbs" / "t.jpg")
    with Image.open(out) as image:
        assert image.format == "JPEG" and image.size == (540, 960)

    # Seeking past the end falls back to the first frame.
    late = make_thumbnail(demo_dir / "clip_02.mp4", tmp_path / "late.jpg", at=999, width=320)
    with Image.open(late) as image:
        assert image.size == (320, 180)

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 100)
    with pytest.raises(MediaValidationError):
        make_thumbnail(broken, tmp_path / "broken.jpg")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("夜明けの歌.MP3", "夜明けの歌.mp3"),
        ("C:\\Users\\me\\Music\\song.wav", "song.wav"),
        ("../../etc/passwd", "passwd"),
        ("  .hidden file .mp4  ", "hidden file.mp4"),
        ('a<b>c:d"e|f?g*h.mov', "a_b_c_d_e_f_g_h.mov"),
        ("evil\u202egnp.mp4", "evilgnp.mp4"),
        ("line\nbreak\t.mp4", "linebreak.mp4"),
        ("", "file"),
        ("....", "file"),
        (None, "file"),
        ("CON.mp4", "_CON.mp4"),
        ("x" * 300 + ".mp4", "x" * 146 + ".mp4"),
        ("multi   space.mp4", "multi space.mp4"),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_file_extension() -> None:
    assert file_extension("A.Song.FLAC") == ".flac"
    assert file_extension("noext") == ""
    assert file_extension(None) == ""
