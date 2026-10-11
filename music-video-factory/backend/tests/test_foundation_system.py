"""Health endpoint, app wiring and the SQLite backend."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import make_engine
from app.routers import system


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "db", "redis", "ffmpeg", "task_mode", "storage_backend"}
    assert body["db"] is True and body["ffmpeg"] is True
    assert isinstance(body["redis"], bool)
    assert body["status"] == "ok"
    assert body["task_mode"] == "inline" and body["storage_backend"] == "local"


def test_health_never_raises(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = make_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    monkeypatch.setattr(system, "get_engine", lambda: broken)
    monkeypatch.setattr(system, "check_ffmpeg", lambda: False)
    body = client.get("/api/health").json()
    assert body["db"] is False and body["ffmpeg"] is False
    assert body["status"] == "degraded"
    broken.dispose()


def test_cors_preflight(client: TestClient) -> None:
    response = client.options(
        "/api/projects",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    denied = client.options(
        "/api/projects",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers


def test_cors_exposes_the_headers_the_ui_reads(client: TestClient) -> None:
    """Cross-origin responses only show the browser the headers listed in Access-Control-Expose-Headers:
    Retry-After of a 429 (Claude storyboard preview limit) must be readable, like the download headers."""
    response = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    exposed = {h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")}
    assert {"retry-after", "content-disposition", "content-length"} <= exposed


def test_placeholder_routers_are_mounted() -> None:
    from app.main import API_ROUTERS
    from app.routers import analysis, generation, lyrics, videos

    for module in (analysis, lyrics, generation, videos):
        assert module in API_ROUTERS


def test_sqlite_backend_end_to_end(tmp_path: Path, demo_dir: Path, enqueued: list) -> None:
    """The whole foundation API works on SQLite (DATABASE_URL=sqlite:///...)."""
    from app import db as db_module
    from app.main import create_app
    from app.models import Asset, Clip, Project

    engine = make_engine(f"sqlite:///{tmp_path / 'mvf.sqlite3'}")
    previous = db_module.set_engine(engine)
    try:
        with TestClient(create_app()) as client:  # lifespan runs init_db on SQLite
            assert client.get("/api/health").json()["db"] is True
            created = client.post("/api/projects", json={"title": "SQLite曲", "artist_name": "S", "mood": "静か"})
            assert created.status_code == 201
            project = created.json()
            assert project["created_at"].endswith("Z") or "+00:00" in project["created_at"]
            project_id = project["id"]
            with engine.connect() as conn:  # the row really lives in the SQLite file
                assert conn.scalar(select(Project.title).where(Project.id == project_id)) == "SQLite曲"

            rights = {k: True for k in project["rights"]}
            confirmed = client.put(f"/api/projects/{project_id}/rights", json={"items": rights}).json()
            assert confirmed["rights_confirmed_at"] is not None

            with (demo_dir / "demo_song.mp3").open("rb") as fh:
                audio = client.post(f"/api/projects/{project_id}/audio", files={"file": ("s.mp3", fh, "audio/mpeg")})
            assert audio.status_code == 201 and audio.json()["analysis_status"] == "queued"
            with (demo_dir / "demo_song.wav").open("rb") as fh:
                replaced = client.post(f"/api/projects/{project_id}/audio", files={"file": ("s.wav", fh, "audio/wav")})
            assert replaced.status_code == 201
            with (demo_dir / "clip_03.mp4").open("rb") as fh:
                clips = client.post(
                    f"/api/projects/{project_id}/clips/upload", files=[("files", ("c.mp4", fh, "video/mp4"))]
                )
            assert clips.status_code == 201

            assert client.get("/api/projects").json()[0]["has_audio"] is True
            assert client.get("/api/dashboard").json()["projects"] == 1
            assert client.delete(f"/api/clips/{clips.json()[0]['id']}").status_code == 204
            assert client.delete(f"/api/projects/{project_id}").status_code == 204

        with engine.connect() as conn:
            for model in (Project, Asset, Clip):
                assert conn.scalar(select(func.count()).select_from(model)) == 0
        assert enqueued == [("analyze", project_id), ("analyze", project_id)]
    finally:
        db_module.set_engine(previous)
        engine.dispose()
