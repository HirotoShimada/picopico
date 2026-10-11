from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, Batch, Clip, Project
from app.schemas import REQUIRED_RIGHTS_KEYS


def _create(client: TestClient, **fields: object) -> dict:
    body = {"title": "夜明けの歌", "artist_name": "テストアーティスト", **fields}
    response = client.post("/api/projects", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_project_crud(client: TestClient) -> None:
    created = _create(client, title="  夜明けの歌  ", mood="切ない", purpose="新曲告知", budget_limit_usd=20)
    assert created["title"] == "夜明けの歌"
    assert created["artist_name"] == "テストアーティスト"
    assert created["mood"] == "切ない"
    assert created["notes"] is None
    assert created["world_preset"] == "cinematic"
    assert created["analysis_status"] == "none"
    assert created["audio"] is None and created["analysis"] is None
    assert created["lyric_lines"] == [] and created["lyrics_text"] == ""
    assert created["rights"] == {key: False for key in REQUIRED_RIGHTS_KEYS}
    assert created["rights_confirmed_at"] is None
    assert created["counts"] == {"clips": 0, "videos": 0, "batches": 0}
    assert created["created_at"].endswith("Z") or "+00:00" in created["created_at"]
    assert "owner_id" not in created
    project_id = created["id"]

    listed = client.get("/api/projects").json()
    assert [p["id"] for p in listed] == [project_id]
    summary = listed[0]
    assert summary["has_audio"] is False
    assert summary["lyric_line_count"] == 0
    assert summary["video_count"] == 0
    assert summary["cover_url"] is None

    fetched = client.get(f"/api/projects/{project_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "夜明けの歌"

    patched = client.patch(f"/api/projects/{project_id}", json={"mood": "明るい", "world_preset": "urban_night"})
    assert patched.status_code == 200
    body = patched.json()
    assert body["mood"] == "明るい" and body["world_preset"] == "urban_night"
    assert body["title"] == "夜明けの歌"  # untouched
    assert body["updated_at"] >= created["updated_at"]

    cleared = client.patch(f"/api/projects/{project_id}", json={"mood": None, "budget_limit_usd": None})
    assert cleared.json()["mood"] is None and cleared.json()["budget_limit_usd"] is None

    assert client.delete(f"/api/projects/{project_id}").status_code == 204
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert client.delete(f"/api/projects/{project_id}").status_code == 404
    assert client.get("/api/projects").json() == []


def test_project_validation(client: TestClient) -> None:
    assert client.post("/api/projects", json={"title": "曲"}).status_code == 422
    assert client.post("/api/projects", json={"title": "   ", "artist_name": "A"}).status_code == 422
    assert (
        client.post("/api/projects", json={"title": "曲", "artist_name": "A", "world_preset": "anime"}).status_code
        == 422
    )
    assert (
        client.post("/api/projects", json={"title": "曲", "artist_name": "A", "budget_limit_usd": -1}).status_code
        == 422
    )

    project_id = _create(client)["id"]
    assert client.patch(f"/api/projects/{project_id}", json={"title": None}).status_code == 422
    assert client.patch(f"/api/projects/{project_id}", json={"world_preset": "nope"}).status_code == 422
    assert client.get("/api/projects/does-not-exist").status_code == 404


def test_rights_checklist(client: TestClient) -> None:
    project_id = _create(client)["id"]
    url = f"/api/projects/{project_id}/rights"

    partial = client.put(url, json={"items": {"audio_rights": True, "lyrics_rights": True}}).json()
    assert partial["rights"]["audio_rights"] is True
    assert partial["rights"]["platform_terms"] is False
    assert partial["rights_confirmed_at"] is None

    rest = {"no_real_person_likeness": True, "model_terms_checked": True, "platform_terms": True}
    confirmed = client.put(url, json={"items": rest}).json()
    assert all(confirmed["rights"][key] for key in REQUIRED_RIGHTS_KEYS)
    assert confirmed["rights_confirmed_at"] is not None

    # Re-confirming keeps the original confirmation time.
    again = client.put(url, json={"items": {"audio_rights": True}}).json()
    assert again["rights_confirmed_at"] == confirmed["rights_confirmed_at"]

    revoked = client.put(url, json={"items": {"platform_terms": False}}).json()
    assert revoked["rights"]["platform_terms"] is False
    assert revoked["rights_confirmed_at"] is None

    unknown = client.put(url, json={"items": {"audio_rights": True, "made_up": True}})
    assert unknown.status_code == 422
    assert "made_up" in unknown.text


def test_delete_blocked_while_batch_active(client: TestClient, db: Session) -> None:
    project_id = _create(client)["id"]
    batch = Batch(project_id=project_id, status="rendering", settings={})
    db.add(batch)
    db.commit()

    blocked = client.delete(f"/api/projects/{project_id}")
    assert blocked.status_code == 409
    assert "キャンセル" in blocked.json()["detail"]

    batch.status = "completed"
    db.commit()
    assert client.delete(f"/api/projects/{project_id}").status_code == 204


def test_delete_project_removes_rows_and_files(
    client: TestClient, db: Session, demo_dir: Path, enqueued: list, storage
) -> None:
    project_id = _create(client)["id"]
    with (demo_dir / "demo_song.mp3").open("rb") as fh:
        assert (
            client.post(f"/api/projects/{project_id}/audio", files={"file": ("song.mp3", fh, "audio/mpeg")}).status_code
            == 201
        )
    with (demo_dir / "clip_03.mp4").open("rb") as fh:
        assert (
            client.post(
                f"/api/projects/{project_id}/clips/upload", files=[("files", ("c.mp4", fh, "video/mp4"))]
            ).status_code
            == 201
        )

    project_dir = storage.root / "projects" / project_id
    stored = [p for p in project_dir.rglob("*") if p.is_file()]
    assert len(stored) == 3  # audio + clip + thumbnail
    keys = db.scalars(select(Asset.storage_key).where(Asset.project_id == project_id)).all()
    assert len(keys) == 3 and all(storage.exists(k) for k in keys)

    assert client.delete(f"/api/projects/{project_id}").status_code == 204
    assert not project_dir.exists()
    assert not any(storage.exists(k) for k in keys)
    db.expire_all()
    for model in (Project, Asset, Clip):
        assert db.scalar(select(func.count()).select_from(model)) == 0
