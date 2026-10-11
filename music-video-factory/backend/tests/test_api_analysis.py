"""§5.3 analysis endpoints: result, re-run, segment selection."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Project


def test_get_analysis_of_analyzed_project(client: TestClient, ready_project: Project) -> None:
    body = client.get(f"/api/projects/{ready_project.id}/analysis").json()
    assert body["status"] == "done" and body["error"] is None
    assert body["analysis"]["bpm"] > 0 and len(body["analysis"]["waveform"]["peaks"]) == 1200
    assert body["segment"] == {"start": 24.033, "end": 39.033}


def test_get_analysis_without_audio(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"title": "曲", "artist_name": "A"}).json()["id"]
    body = client.get(f"/api/projects/{pid}/analysis").json()
    assert body == {"status": "none", "error": None, "analysis": None, "segment": None}
    response = client.post(f"/api/projects/{pid}/analysis/rerun")
    assert response.status_code == 409 and response.json()["code"] == "audio_missing"
    response = client.put(f"/api/projects/{pid}/segment", json={"start": 0, "end": 10})
    assert response.status_code == 409


def test_rerun_queues_analysis(client: TestClient, ready_project: Project, enqueued: list, db: Session) -> None:
    response = client.post(f"/api/projects/{ready_project.id}/analysis/rerun")
    assert response.status_code == 202 and response.json() == {"status": "queued"}
    assert enqueued == [("analyze", ready_project.id)]
    db.expire_all()
    assert db.get(Project, ready_project.id).analysis_status == "queued"


def test_rerun_reports_queue_outage(client: TestClient, ready_project: Project, monkeypatch: Any, db: Session) -> None:
    from app import worker

    def down(name: str, *args: Any) -> None:
        raise worker.QueueUnavailable("ジョブキュー（Redis）に接続できません")

    monkeypatch.setattr(worker, "enqueue", down)
    response = client.post(f"/api/projects/{ready_project.id}/analysis/rerun")
    assert response.status_code == 503 and response.json()["code"] == "queue_unavailable"
    db.expire_all()
    assert db.get(Project, ready_project.id).analysis_status == "failed"


def test_segment_update_and_validation(client: TestClient, ready_project: Project) -> None:
    url = f"/api/projects/{ready_project.id}/segment"
    response = client.put(url, json={"start": 8.0, "end": 38.0})
    assert response.status_code == 200 and response.json() == {"segment": {"start": 8.0, "end": 38.0}}
    assert client.get(f"/api/projects/{ready_project.id}").json()["segment_start"] == 8.0

    too_long = client.put(url, json={"start": 50.0, "end": 75.0})
    assert too_long.status_code == 422 and too_long.json()["code"] == "segment_out_of_range"
    assert client.put(url, json={"start": 10.0, "end": 12.0}).status_code == 422  # < 3 s
    assert client.put(url, json={"start": -1, "end": 12.0}).status_code == 422
    # A hair past the end (rounding) is clamped to the song length.
    clamped = client.put(url, json={"start": 45.0, "end": 60.04}).json()["segment"]
    assert clamped["end"] <= 60.01


def test_analysis_endpoints_are_owner_scoped(make_client: Any, ready_project: Project) -> None:
    other = make_client(AUTH_MODE="token", API_TOKENS="bob:bob-token-0123456789abcdef")
    headers = {"Authorization": "Bearer bob-token-0123456789abcdef"}
    for method, path in (
        ("get", "analysis"),
        ("post", "analysis/rerun"),
    ):
        response = getattr(other, method)(f"/api/projects/{ready_project.id}/{path}", headers=headers)
        assert response.status_code == 404
    response = other.put(f"/api/projects/{ready_project.id}/segment", json={"start": 0, "end": 10}, headers=headers)
    assert response.status_code == 404
