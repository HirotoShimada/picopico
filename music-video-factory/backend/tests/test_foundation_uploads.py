from __future__ import annotations

import asyncio
import io
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, Batch, Clip, Project, Video
from app.services.errors import MediaValidationError
from app.services.media import save_upload_stream


def _new_project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"title": "デモ曲", "artist_name": "デモ"})
    assert response.status_code == 201
    return response.json()["id"]


def _upload_audio(client: TestClient, project_id: str, path: Path, name: str | None = None, mime: str = "audio/mpeg"):
    with path.open("rb") as fh:
        return client.post(f"/api/projects/{project_id}/audio", files={"file": (name or path.name, fh, mime)})


def _work_uploads(settings_dir: Path) -> list[Path]:
    uploads = settings_dir / "uploads"
    return [p for p in uploads.rglob("*") if p.is_file()] if uploads.exists() else []


# --- audio ----------------------------------------------------------------------------------------


def test_audio_upload_demo_mp3(client: TestClient, db: Session, demo_dir: Path, enqueued: list, storage) -> None:
    project_id = _new_project(client)
    project = db.get(Project, project_id)
    project.lyrics_text = "一行目\n二行目"
    project.lyric_lines = [
        {"id": "l1", "text": "一行目", "start": None, "end": None, "words": None, "source": "txt", "confidence": None}
    ]
    db.commit()

    mp3 = demo_dir / "demo_song.mp3"
    response = _upload_audio(client, project_id, mp3, name="デモ曲 (master).mp3")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["analysis_status"] == "queued"
    audio = body["audio"]
    assert audio["kind"] == "audio"
    assert audio["filename"] == "デモ曲 (master).mp3"
    assert audio["content_type"] == "audio/mpeg"
    assert audio["size_bytes"] == mp3.stat().st_size
    assert audio["duration"] == pytest.approx(60.0, abs=0.2)
    assert body["audio_asset_id"] == audio["id"]
    assert body["lyrics_text"] == "一行目\n二行目" and len(body["lyric_lines"]) == 1
    assert enqueued == [("analyze", project_id)]

    db.expire_all()
    asset = db.get(Asset, audio["id"])
    assert asset.storage_key == f"projects/{project_id}/audio/{asset.id}.mp3"
    assert storage.exists(asset.storage_key) and storage.size(asset.storage_key) == mp3.stat().st_size
    assert asset.meta["codec"] == "mp3" and len(asset.meta["sha256"]) == 64

    assert audio["url"].startswith("http://testserver/media/projects/")
    fetched = client.get(audio["url"])
    assert fetched.status_code == 200 and fetched.content == mp3.read_bytes()
    assert not _work_uploads(storage.root.parent / "work")

    summary = client.get("/api/projects").json()[0]
    assert summary["has_audio"] is True and summary["lyric_line_count"] == 1


def test_audio_replacement_clears_analysis_keeps_lyrics(
    client: TestClient, db: Session, demo_dir: Path, enqueued: list, storage
) -> None:
    project_id = _new_project(client)
    first = _upload_audio(client, project_id, demo_dir / "demo_song.mp3").json()["audio"]
    old_key = db.get(Asset, first["id"]).storage_key

    project = db.get(Project, project_id)
    project.analysis = {"version": 1, "bpm": 120.0}
    project.analysis_status = "done"
    project.segment_start, project.segment_end = 24.0, 39.0
    project.lyrics_text = "歌詞"
    db.commit()

    second = _upload_audio(client, project_id, demo_dir / "demo_song.wav", mime="audio/wav")
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["audio"]["id"] != first["id"]
    assert body["audio"]["content_type"] == "audio/wav"
    assert body["analysis"] is None and body["analysis_status"] == "queued"
    assert body["segment_start"] is None and body["segment_end"] is None
    assert body["lyrics_text"] == "歌詞"
    assert enqueued == [("analyze", project_id), ("analyze", project_id)]

    db.expire_all()
    assert db.get(Asset, first["id"]) is None
    assert not storage.exists(old_key)
    assert db.scalar(select(func.count()).select_from(Asset).where(Asset.project_id == project_id)) == 1


def test_fake_audio_rejected_with_japanese_message(
    client: TestClient, db: Session, tmp_path: Path, enqueued: list, storage
) -> None:
    project_id = _new_project(client)
    fake = tmp_path / "song.mp3"
    fake.write_text("これは音声ではありません。just text pretending to be an mp3\n" * 20, encoding="utf-8")

    response = _upload_audio(client, project_id, fake)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "音声" in detail
    assert response.json()["code"] == "media_invalid"
    assert enqueued == []
    assert db.scalar(select(func.count()).select_from(Asset)) == 0
    assert client.get(f"/api/projects/{project_id}").json()["analysis_status"] == "none"
    assert not (storage.root / "projects" / project_id).exists()
    assert not _work_uploads(storage.root.parent / "work")


def test_audio_extension_and_duration_checks(
    client: TestClient, demo_dir: Path, tmp_path: Path, enqueued: list
) -> None:
    project_id = _new_project(client)

    wrong_ext = _upload_audio(client, project_id, demo_dir / "demo_song.mp3", name="song.exe")
    assert wrong_ext.status_code == 400
    assert "対応していない音声形式" in wrong_ext.json()["detail"]

    video_as_audio = _upload_audio(client, project_id, demo_dir / "clip_02.mp4", name="clip.m4a", mime="audio/mp4")
    assert video_as_audio.status_code == 400  # no audio stream

    short = tmp_path / "short.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(short)],
        check=True,
    )
    too_short = _upload_audio(client, project_id, short, mime="audio/wav")
    assert too_short.status_code == 400
    assert "短すぎ" in too_short.json()["detail"]
    assert enqueued == []


def test_oversize_audio_rejected_before_reading(
    make_client: Callable[..., TestClient], demo_dir: Path, enqueued: list
) -> None:
    client = make_client(MAX_AUDIO_MB=1)
    project_id = _new_project(client)
    mp3 = demo_dir / "demo_song.mp3"
    assert mp3.stat().st_size > 1024 * 1024 + 1024 * 1024 // 4
    response = _upload_audio(client, project_id, mp3)
    assert response.status_code == 413
    assert "上限" in response.json()["detail"]
    assert enqueued == []


def test_oversize_audio_rejected_while_streaming(
    make_client: Callable[..., TestClient], demo_dir: Path, enqueued: list, monkeypatch: pytest.MonkeyPatch, storage
) -> None:
    from app.main import UploadSizeLimitMiddleware

    # Bypass the Content-Length pre-check to exercise the per-file streaming limit.
    monkeypatch.setattr(UploadSizeLimitMiddleware, "_limit_for", staticmethod(lambda path: None))
    client = make_client(MAX_AUDIO_MB=1)
    project_id = _new_project(client)
    response = _upload_audio(client, project_id, demo_dir / "demo_song.mp3")
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert "1 MB" in response.json()["detail"]
    assert not _work_uploads(storage.root.parent / "work")


def test_save_upload_stream_enforces_limit(tmp_path: Path) -> None:
    data = b"\x00" * (3 * 1024 * 1024 + 7)
    dest = tmp_path / "out.bin"

    unknown_size = UploadFile(io.BytesIO(data), filename="big.bin")
    assert unknown_size.size is None
    with pytest.raises(MediaValidationError) as excinfo:
        asyncio.run(save_upload_stream(unknown_size, dest, 1024 * 1024))
    assert excinfo.value.status_code == 413
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []  # no partial file left behind

    declared = UploadFile(io.BytesIO(data), filename="big.bin", size=len(data))
    with pytest.raises(MediaValidationError):
        asyncio.run(save_upload_stream(declared, dest, 1024 * 1024))

    ok = UploadFile(io.BytesIO(data), filename="ok.bin")
    assert asyncio.run(save_upload_stream(ok, dest, len(data))) == len(data)
    assert dest.read_bytes() == data


# --- clips --------------------------------------------------------------------------------------------


def _upload_clips(client: TestClient, project_id: str, paths: list[tuple[Path, str]]):
    handles = [path.open("rb") for path, _ in paths]
    try:
        files = [("files", (name, fh, "video/mp4")) for fh, (_, name) in zip(handles, paths, strict=True)]
        return client.post(f"/api/projects/{project_id}/clips/upload", files=files)
    finally:
        for fh in handles:
            fh.close()


def test_clip_upload_demo_clips(client: TestClient, db: Session, demo_dir: Path, storage) -> None:
    project_id = _new_project(client)
    paths = [(demo_dir / f"clip_0{i}.mp4", f"素材{i}.mp4") for i in (1, 2, 3)]
    response = _upload_clips(client, project_id, paths)
    assert response.status_code == 201, response.text
    clips = response.json()
    assert len(clips) == 3

    expected = [(6.0, 720, 1280), (8.0, 1280, 720), (5.0, 1080, 1920)]
    for clip, (duration, width, height), (path, _) in zip(clips, expected, paths, strict=True):
        assert clip["source"] == "upload" and clip["status"] == "ready" and clip["provider"] == "upload"
        assert clip["duration"] == pytest.approx(duration, abs=0.1)
        assert (clip["width"], clip["height"]) == (width, height)
        assert clip["cost_usd"] == 0 and clip["used_in_videos"] == 0
        assert clip["url"] and clip["thumbnail_url"]

        row = db.get(Clip, clip["id"])
        assert row.asset.size_bytes == path.stat().st_size
        assert row.asset.kind == "clip" and row.thumbnail_asset.kind == "thumbnail"
        assert storage.exists(row.asset.storage_key) and storage.exists(row.thumbnail_asset.storage_key)

        thumb = client.get(clip["thumbnail_url"])
        assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
        with Image.open(io.BytesIO(thumb.content)) as image:
            assert image.format == "JPEG" and image.width == 540
            assert image.height == pytest.approx(540 * height / width, abs=2)
        assert client.get(clip["url"]).content == path.read_bytes()

    listed = client.get(f"/api/projects/{project_id}/clips").json()
    assert [c["id"] for c in listed] == [c["id"] for c in clips]
    assert client.get(f"/api/projects/{project_id}").json()["counts"]["clips"] == 3
    assert client.get("/api/projects").json()[0]["cover_url"]


def test_clip_upload_is_all_or_nothing(client: TestClient, db: Session, demo_dir: Path, storage) -> None:
    project_id = _new_project(client)
    response = _upload_clips(
        client, project_id, [(demo_dir / "clip_01.mp4", "good.mp4"), (demo_dir / "demo_song.mp3", "song.mp4")]
    )
    assert response.status_code == 400
    assert response.json()["detail"].startswith("song.mp4: ")
    assert "映像ストリーム" in response.json()["detail"] or "動画" in response.json()["detail"]
    assert db.scalar(select(func.count()).select_from(Clip)) == 0
    assert db.scalar(select(func.count()).select_from(Asset)) == 0
    assert not (storage.root / "projects" / project_id).exists()
    assert not _work_uploads(storage.root.parent / "work")

    bad_ext = _upload_clips(client, project_id, [(demo_dir / "clip_01.mp4", "clip.avi")])
    assert bad_ext.status_code == 400 and "対応していない動画形式" in bad_ext.json()["detail"]


def test_clip_upload_file_count_limit(client: TestClient) -> None:
    project_id = _new_project(client)
    files = [("files", (f"c{i}.mp4", io.BytesIO(b"x"), "video/mp4")) for i in range(21)]
    response = client.post(f"/api/projects/{project_id}/clips/upload", files=files)
    assert response.status_code == 400
    assert "1〜20本" in response.json()["detail"]
    assert client.post(f"/api/projects/{project_id}/clips/upload").status_code == 422


def test_clip_delete(client: TestClient, db: Session, demo_dir: Path, storage) -> None:
    project_id = _new_project(client)
    first, second = _upload_clips(
        client, project_id, [(demo_dir / "clip_03.mp4", "a.mp4"), (demo_dir / "clip_01.mp4", "b.mp4")]
    ).json()
    row = db.get(Clip, first["id"])
    keys = [row.asset.storage_key, row.thumbnail_asset.storage_key]

    assert client.delete(f"/api/clips/{first['id']}").status_code == 204
    assert not any(storage.exists(k) for k in keys)
    assert [c["id"] for c in client.get(f"/api/projects/{project_id}/clips").json()] == [second["id"]]
    assert client.delete(f"/api/clips/{first['id']}").status_code == 404

    batch = Batch(project_id=project_id, status="rendering", settings={})
    db.add(batch)
    db.flush()
    plan = {"index": 0, "shots": [{"clip_key": "c01", "clip_id": second["id"], "src_in": 0, "duration": 2}]}
    video = Video(project_id=project_id, batch_id=batch.id, index=0, status="rendering", plan=plan)
    db.add(video)
    db.commit()

    listed = client.get(f"/api/projects/{project_id}/clips").json()
    assert listed[0]["used_in_videos"] == 1
    conflict = client.delete(f"/api/clips/{second['id']}")
    assert conflict.status_code == 409

    video.status = "done"
    db.commit()
    assert client.delete(f"/api/clips/{second['id']}").status_code == 204
