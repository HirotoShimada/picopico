"""§5.7 video endpoints: list/detail, favourite, retry, regenerate, edit → re-render, download, ZIP."""

from __future__ import annotations

import io
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Project, Video


@pytest.fixture
def finished_batch(
    client: TestClient, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any
) -> dict[str, Any]:
    settings = {"provider": "mock", "count": 2, "duration_sec": 5, "max_new_clips": 3, "lyric_style": "simple"}
    batch_id = client.post(f"/api/projects/{ready_project.id}/batches", json=settings).json()["id"]
    batch = client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "completed"
    return batch


def test_list_videos_with_filters(client: TestClient, ready_project: Project, finished_batch: dict) -> None:
    url = f"/api/projects/{ready_project.id}/videos"
    videos = client.get(url).json()
    assert [v["index"] for v in videos] == [0, 1] and all(v["status"] == "done" for v in videos)
    first = videos[0]["id"]
    client.patch(f"/api/videos/{first}", json={"favorite": True})
    assert [v["id"] for v in client.get(url, params={"favorite": "true"}).json()] == [first]
    assert len(client.get(url, params={"favorite": "false"}).json()) == 1
    assert len(client.get(url, params={"batch_id": finished_batch["id"]}).json()) == 2
    assert client.get(url, params={"batch_id": "0" * 32}).json() == []


def test_video_detail(client: TestClient, ready_project: Project, finished_batch: dict) -> None:
    video = finished_batch["videos"][0]
    body = client.get(f"/api/videos/{video['id']}").json()
    assert body["id"] == video["id"] and body["url"] and body["duration"] == 5.0
    assert body["quality"]["passed"] is True and body["render_version"] == 1
    assert [line["id"] for line in body["lyric_lines"]] == [line["id"] for line in ready_project.lyric_lines]
    assert body["available_clips"] and all(c["status"] == "ready" for c in body["available_clips"])
    assert client.get(f"/api/videos/{'0' * 32}").status_code == 404


def test_patch_video(client: TestClient, finished_batch: dict) -> None:
    video_id = finished_batch["videos"][1]["id"]
    body = client.patch(f"/api/videos/{video_id}", json={"title": "  サビ版  ", "favorite": True}).json()
    assert body["title"] == "サビ版" and body["favorite"] is True
    assert client.patch(f"/api/videos/{video_id}", json={"title": ""}).status_code == 422
    assert client.patch(f"/api/videos/{video_id}", json={"title": None}).status_code == 422
    assert client.patch(f"/api/videos/{video_id}", json={"favorite": False}).json()["favorite"] is False


def test_edit_rerenders_with_overrides(
    client: TestClient, ready_project: Project, finished_batch: dict, fake_render: Any, add_upload_clips: Any
) -> None:
    uploads = add_upload_clips(ready_project, ("clip_02.mp4",))
    video = finished_batch["videos"][0]
    detail = client.get(f"/api/videos/{video['id']}").json()
    line = next(x for x in detail["lyric_lines"] if x["start"] >= video["plan"]["start"])
    old_url = detail["url"]
    edit = {
        "lyric_style": "fade",
        "color_grade": "warm_film",
        "lyric_offsets": {line["id"]: {"start": line["start"] + 0.4, "end": line["end"] + 0.4}},
        "shots": [{"index": 1, "clip_id": uploads[0].id}],
    }
    response = client.put(f"/api/videos/{video['id']}/edit", json=edit)
    assert response.status_code == 200
    body = client.get(f"/api/videos/{video['id']}").json()
    assert body["status"] == "done" and body["render_version"] == 2 and body["url"] != old_url
    assert body["plan"]["lyric_style"] == "fade" and body["plan"]["color_grade"] == "warm_film"
    assert body["plan"]["shots"][1]["clip_id"] == uploads[0].id
    edited = next(x for x in body["lyric_lines"] if x["id"] == line["id"])
    assert edited["start"] == pytest.approx(line["start"] + 0.4)
    spec = fake_render.specs[-1]
    assert spec.lyric_style == "fade" and spec.color_grade == "warm_film"
    assert spec.shots[1].clip_path.endswith(uploads[0].asset.storage_key)
    assert client.get(f"/api/batches/{finished_batch['id']}").json()["status"] == "completed"

    # Offsets accumulate: a second edit keeps the first line override.
    client.put(f"/api/videos/{video['id']}/edit", json={"text_style": {"position": "top"}})
    body = client.get(f"/api/videos/{video['id']}").json()
    assert body["render_version"] == 3 and line["id"] in body["plan"]["lyric_offsets"]
    assert body["plan"]["text_style"] == {"position": "top"}

    bad = client.put(f"/api/videos/{video['id']}/edit", json={"shots": [{"index": 0, "clip_id": "0" * 32}]})
    assert bad.status_code == 422 and bad.json()["code"] == "clip_unknown"
    assert client.put(f"/api/videos/{video['id']}/edit", json={"lyric_style": "rainbow"}).status_code == 422


def test_edit_and_retry_conflicts(client: TestClient, finished_batch: dict, db: Session) -> None:
    video_id = finished_batch["videos"][0]["id"]
    retry = client.post(f"/api/videos/{video_id}/retry")
    assert retry.status_code == 409 and retry.json()["code"] == "video_not_retryable"
    db.get(Video, video_id).status = "rendering"
    db.commit()
    busy = client.put(f"/api/videos/{video_id}/edit", json={"lyric_style": "fade"})
    assert busy.status_code == 409 and busy.json()["code"] == "video_busy"
    assert client.post(f"/api/videos/{video_id}/regenerate", json={}).status_code == 409


def test_retry_failed_video(client: TestClient, finished_batch: dict, db: Session, fake_render: Any) -> None:
    video_id = finished_batch["videos"][1]["id"]
    video = db.get(Video, video_id)
    video.status, video.error = "failed", "レンダリングに失敗しました"
    db.commit()
    body = client.post(f"/api/videos/{video_id}/retry").json()
    assert body["id"] == video_id
    db.expire_all()
    video = db.get(Video, video_id)
    assert video.status == "done" and video.error is None and video.render_version == 1


def test_regenerate_video(client: TestClient, finished_batch: dict, fake_render: Any, fake_clips: Any) -> None:
    video = finished_batch["videos"][0]
    calls = len(fake_clips.calls)
    response = client.post(f"/api/videos/{video['id']}/regenerate", json={"new_clips": False})
    assert response.status_code == 200
    body = client.get(f"/api/videos/{video['id']}").json()
    assert body["status"] == "done" and body["render_version"] == 2 and body["plan"]["regenerated"] is True
    assert body["plan"]["seed"] != video["plan"]["seed"] and body["title"] == video["title"]
    assert len(fake_clips.calls) == calls  # existing footage only
    fresh = client.post(f"/api/videos/{video['id']}/regenerate", json={"new_clips": True})
    assert fresh.status_code == 200 and len(fake_clips.calls) > calls
    assert client.get(f"/api/videos/{video['id']}").json()["render_version"] == 3


def test_download_redirects_to_signed_attachment(client: TestClient, finished_batch: dict) -> None:
    video_id = finished_batch["videos"][1]["id"]
    response = client.get(f"/api/videos/{video_id}/download", follow_redirects=False)
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert query["download"] == ["1"] and query["filename"] == ["デモアーティスト_夜明けのうた_02.mp4"]
    media = client.get(response.headers["location"].replace("http://testserver", ""))
    assert media.status_code == 200
    disposition = media.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "filename*=UTF-8''" in disposition
    assert media.content[4:8] == b"ftyp"


def test_download_of_unfinished_video_is_409(client: TestClient, finished_batch: dict, db: Session) -> None:
    video_id = finished_batch["videos"][0]["id"]
    db.get(Video, video_id).status = "failed"
    db.commit()
    response = client.get(f"/api/videos/{video_id}/download", follow_redirects=False)
    assert response.status_code == 409 and response.json()["code"] == "video_not_ready"


def test_zip_selected_videos(client: TestClient, finished_batch: dict) -> None:
    ids = [v["id"] for v in finished_batch["videos"]]
    response = client.post("/api/videos/zip", json={"video_ids": [ids[1], ids[1], ids[0]]})
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["デモアーティスト_夜明けのうた_02.mp4", "デモアーティスト_夜明けのうた_01.mp4"]
    assert archive.testzip() is None
    assert client.post("/api/videos/zip", json={"video_ids": []}).status_code == 422
    assert client.post("/api/videos/zip", json={"video_ids": ["0" * 32]}).status_code == 404


def test_video_endpoints_are_owner_scoped(make_client: Any, finished_batch: dict) -> None:
    other = make_client(AUTH_MODE="token", API_TOKENS="bob:bob-token-0123456789abcdef")
    headers = {"Authorization": "Bearer bob-token-0123456789abcdef"}
    video_id = finished_batch["videos"][0]["id"]
    assert other.get(f"/api/videos/{video_id}", headers=headers).status_code == 404
    assert other.patch(f"/api/videos/{video_id}", json={"favorite": True}, headers=headers).status_code == 404
    assert other.get(f"/api/videos/{video_id}/download", headers=headers).status_code == 404
    assert other.post(f"/api/videos/{video_id}/retry", headers=headers).status_code == 404
    assert other.put(f"/api/videos/{video_id}/edit", json={}, headers=headers).status_code == 404
    assert other.post("/api/videos/zip", json={"video_ids": [video_id]}, headers=headers).status_code == 404
