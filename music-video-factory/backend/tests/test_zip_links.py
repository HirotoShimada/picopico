"""Signed ZIP links: ``POST /api/videos/zip-link`` / ``POST /api/batches/{id}/zip-link`` → ``GET /media-zip/…``
(no auth header), streamed by the browser instead of being buffered in JavaScript."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Asset, Batch, Project, User, Video, new_id
from app.security import decode_video_ids, encode_video_ids, sign_zip_url
from app.storage import make_key

ALICE = "alice-token-0123456789abcdef"


def _done_video(db: Session, storage: Any, canned: Path, project: Project, batch: Batch, index: int) -> Video:
    asset = Asset(id=new_id(), project_id=project.id, owner_id=project.owner_id, kind="render", filename="v.mp4",
                  content_type="video/mp4")  # fmt: skip
    asset.storage_key = make_key(project.id, "render", asset.id, ".mp4")
    asset.size_bytes = storage.put_file(asset.storage_key, canned)
    video = Video(project_id=project.id, batch_id=batch.id, index=index, status="done", asset=asset, plan={},
                  rendered_at=datetime(2026, 10, 11, tzinfo=UTC))  # fmt: skip
    db.add_all([asset, video])
    db.commit()
    return video


@pytest.fixture
def make_videos(db: Session, storage: Any, canned_video: Path) -> Callable[..., list[Video]]:
    def _make(owner_id: str, statuses: tuple[str, ...] = ("done", "done")) -> list[Video]:
        project = Project(owner_id=owner_id, title="夜明けのうた", artist_name="デモ")
        db.add(project)
        db.flush()
        batch = Batch(project_id=project.id, status="completed", settings={})
        db.add(batch)
        db.flush()
        videos = []
        for index, status in enumerate(statuses):
            video = _done_video(db, storage, canned_video, project, batch, index)
            video.status = status
            videos.append(video)
        db.commit()
        return videos

    return _make


def _path(url: str) -> str:
    parsed = urlparse(url)
    assert parsed.scheme == "http" and parsed.netloc == "testserver"
    return f"{parsed.path}?{parsed.query}"


def _names(content: bytes) -> list[str]:
    archive = zipfile.ZipFile(io.BytesIO(content))
    assert archive.testzip() is None
    return archive.namelist()


def test_batch_zip_link_streams_the_same_archive(
    client: TestClient, db: Session, local_user: User, make_videos: Callable[..., list[Video]]
) -> None:
    first, _second, _failed = make_videos(local_user.id, ("done", "done", "failed"))
    response = client.post(f"/api/batches/{first.batch_id}/zip-link")
    assert response.status_code == 200
    body = response.json()
    assert body["url"].startswith("http://testserver/media-zip/")
    expires = datetime.fromisoformat(body["expires_at"])
    assert 3500 < (expires - datetime.now(UTC)).total_seconds() <= 3600

    link = TestClient(client.app).get(_path(body["url"]))  # a fresh client: no cookies, no headers
    assert link.status_code == 200 and link.headers["content-type"] == "application/zip"
    assert "no-store" in link.headers["cache-control"]
    assert "attachment" in link.headers["content-disposition"]
    assert "%E5%A4%9C%E6%98%8E%E3%81%91" in link.headers["content-disposition"]  # 夜明け (UTF-8 file name)
    old = client.get(f"/api/batches/{first.batch_id}/zip")
    assert _names(link.content) == _names(old.content) == ["デモ_夜明けのうた_01.mp4", "デモ_夜明けのうた_02.mp4"]


def test_selection_zip_link_keeps_order_and_skips_unfinished(
    client: TestClient, local_user: User, make_videos: Callable[..., list[Video]]
) -> None:
    first, second, queued = make_videos(local_user.id, ("done", "done", "queued"))
    body = client.post("/api/videos/zip-link", json={"video_ids": [second.id, queued.id, first.id, second.id]}).json()
    query = parse_qs(urlparse(body["url"]).query)
    assert decode_video_ids(query["videos"][0]) == [second.id, first.id]
    assert query["owner"] == [local_user.id]
    assert urlparse(body["url"]).path == "/media-zip/videos.zip"
    content = client.get(_path(body["url"])).content
    assert _names(content) == ["デモ_夜明けのうた_02.mp4", "デモ_夜明けのうた_01.mp4"]

    assert client.post("/api/videos/zip-link", json={"video_ids": ["0" * 32]}).status_code == 404
    none_done = client.post("/api/videos/zip-link", json={"video_ids": [queued.id]})
    assert none_done.status_code == 409 and none_done.json()["code"] == "no_videos"
    assert client.post("/api/videos/zip-link", json={"video_ids": []}).status_code == 422


def test_tampered_or_expired_links_are_403(
    client: TestClient, local_user: User, make_videos: Callable[..., list[Video]]
) -> None:
    first, second = make_videos(local_user.id)
    url = client.post("/api/videos/zip-link", json={"video_ids": [first.id, second.id]}).json()["url"]
    parsed = urlparse(url)
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}

    def get(path: str = parsed.path, **changes: str) -> int:
        return client.get(f"{path}?{urlencode({**query, **changes})}").status_code

    assert get() == 200
    assert get(videos=encode_video_ids([first.id])) == 403  # a subset is a different signature
    assert get(videos=encode_video_ids([second.id, first.id])) == 200  # order is not signed, the set is
    assert get(owner="0" * 32) == 403
    assert get(sig="0" * 64) == 403
    assert get(exp=str(int(query["exp"]) + 60)) == 403
    assert get(path="/media-zip/other.zip") == 403
    assert get(videos="not-base64!") == 403
    assert client.get(parsed.path).status_code == 403
    expired, _ = sign_zip_url(local_user.id, [first.id], "videos.zip", ttl=-5)
    assert client.get(_path(expired)).status_code == 403


def test_videos_must_still_belong_to_the_owner_when_streaming(
    client: TestClient, db: Session, local_user: User, make_videos: Callable[..., list[Video]]
) -> None:
    kept = make_videos(local_user.id, ("done",))[0]
    moved = make_videos(local_user.id, ("done",))[0]
    url = client.post("/api/videos/zip-link", json={"video_ids": [kept.id, moved.id]}).json()["url"]
    other = User(name="other")
    db.add(other)
    db.flush()
    db.get(Project, moved.project_id).owner_id = other.id
    db.commit()
    assert _names(client.get(_path(url)).content) == ["デモ_夜明けのうた_01.mp4"]  # only the still-owned video

    db.get(Project, kept.project_id).owner_id = other.id
    db.commit()
    gone = client.get(_path(url))
    assert gone.status_code == 409 and gone.json()["code"] == "no_videos"

    # A link signed for another owner cannot reach these videos either.
    forged, _ = sign_zip_url(local_user.id, [kept.id, moved.id], "videos.zip")
    assert client.get(_path(forged)).status_code == 409


def test_zip_link_works_without_auth_header_in_token_mode(
    make_client: Callable[..., TestClient], db: Session, make_videos: Callable[..., list[Video]]
) -> None:
    token_client = make_client(AUTH_MODE="token", API_TOKENS=f"alice:{ALICE}")
    alice = db.query(User).filter(User.name == "alice").one()
    first, _second = make_videos(alice.id)
    headers = {"Authorization": f"Bearer {ALICE}"}
    assert token_client.post("/api/videos/zip-link", json={"video_ids": [first.id]}).status_code == 401
    assert token_client.post(f"/api/batches/{first.batch_id}/zip-link").status_code == 401
    body = token_client.post(f"/api/batches/{first.batch_id}/zip-link", headers=headers).json()
    response = token_client.get(_path(body["url"]))  # no Authorization header
    assert response.status_code == 200 and len(_names(response.content)) == 2
    assert quote("デモ_夜明けのうた_videos.zip") in body["url"]


def test_video_id_encoding_round_trip() -> None:
    ids = [new_id() for _ in range(500)]
    token = encode_video_ids(ids)
    assert decode_video_ids(token) == ids and len(token) < 11_000
    assert decode_video_ids("") is None and decode_video_ids("AAAA") is None
