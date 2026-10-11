from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Asset, Batch, CostRecord, Project, User, Video, utcnow


def test_dashboard_empty_shape(client: TestClient) -> None:
    body = client.get("/api/dashboard").json()
    assert body == {
        "projects": 0,
        "videos_done": 0,
        "videos_total": 0,
        "active_jobs": [],
        "recent_videos": [],
        "usage": {
            "total_usd": 0.0,
            "month_usd": 0.0,
            "by_provider": [],
            "clips_generated": 0,
            "clips_reused": 0,
            "saved_usd": 0.0,
        },
        "pending_estimate_usd": 0.0,
    }


def _cost(
    project_id: str,
    provider: str,
    units: float,
    price: float,
    *,
    cached: bool = False,
    kind: str = "video_generation",
    **extra,
) -> CostRecord:
    return CostRecord(
        project_id=project_id,
        kind=kind,
        provider=provider,
        model=f"{provider}-model",
        units=units,
        unit="second" if kind == "video_generation" else "token",
        unit_price_usd=price,
        amount_usd=0.0 if cached else round(units * price, 6),
        cached=cached,
        **extra,
    )


def test_dashboard_aggregates(client: TestClient, db: Session) -> None:
    project_id = client.post("/api/projects", json={"title": "集計テスト", "artist_name": "A"}).json()["id"]
    project = db.get(Project, project_id)

    active = Batch(project_id=project_id, status="rendering", settings={}, estimate={"total_usd": 2.5}, cost_usd=1.0)
    finished = Batch(project_id=project_id, status="completed", settings={}, estimate={"total_usd": 9.0}, cost_usd=9.0)
    db.add_all([active, finished])
    db.flush()

    now = utcnow()
    assets = []
    for i in range(3):
        render = Asset(
            project_id=project_id,
            owner_id=project.owner_id,
            kind="render",
            filename=f"v{i}.mp4",
            storage_key=f"projects/{project_id}/render/v{i}.mp4",
            duration=15.0,
        )
        thumb = Asset(
            project_id=project_id,
            owner_id=project.owner_id,
            kind="thumbnail",
            filename=f"v{i}.jpg",
            storage_key=f"projects/{project_id}/thumbnail/v{i}.jpg",
        )
        assets.append((render, thumb))
        db.add_all([render, thumb])
    db.flush()
    videos = [
        Video(project_id=project_id, batch_id=active.id, index=0, title="A #1", status="done",
              asset_id=assets[0][0].id, thumbnail_asset_id=assets[0][1].id, rendered_at=now - timedelta(minutes=5)),
        Video(project_id=project_id, batch_id=active.id, index=1, title="A #2", status="rendering", progress=0.4),
        Video(project_id=project_id, batch_id=active.id, index=2, title="A #3", status="failed", error="x"),
        Video(project_id=project_id, batch_id=finished.id, index=0, title="F #1", status="done",
              asset_id=assets[1][0].id, thumbnail_asset_id=assets[1][1].id, rendered_at=now - timedelta(minutes=1)),
        Video(project_id=project_id, batch_id=finished.id, index=1, title="F #2", status="done",
              asset_id=assets[2][0].id, thumbnail_asset_id=assets[2][1].id, rendered_at=now - timedelta(minutes=10)),
    ]  # fmt: skip
    db.add_all(videos)

    last_month = now.replace(day=1) - timedelta(days=1)
    db.add_all(
        [
            _cost(project_id, "kling", 5, 0.1, batch_id=active.id),
            _cost(project_id, "kling", 5, 0.1, cached=True, batch_id=active.id),
            _cost(project_id, "kling", 10, 0.1, cached=True, batch_id=active.id),
            _cost(project_id, "veo", 8, 0.4, batch_id=finished.id),
            _cost(project_id, "anthropic", 5000, 0.000004, kind="llm"),
            _cost(project_id, "kling", 10, 0.1, created_at=last_month),
        ]
    )

    # Another user's data must not leak into this dashboard.
    stranger = User(name="stranger")
    db.add(stranger)
    db.flush()
    other = Project(owner_id=stranger.id, title="他人", artist_name="B")
    db.add(other)
    db.flush()
    other_batch = Batch(project_id=other.id, status="planning", settings={}, estimate={"total_usd": 100})
    db.add(other_batch)
    db.flush()
    db.add(Video(project_id=other.id, batch_id=other_batch.id, index=0, status="done"))
    db.add(_cost(other.id, "runway", 100, 0.5))
    db.commit()

    body = client.get("/api/dashboard").json()
    assert body["projects"] == 1
    assert body["videos_done"] == 3
    assert body["videos_total"] == 5
    assert body["active_jobs"] == [
        {
            "batch_id": active.id,
            "project_id": project_id,
            "project_title": "集計テスト",
            "status": "rendering",
            "done": 1,
            "total": 3,
            "failed": 1,
        }
    ]
    assert [v["title"] for v in body["recent_videos"]] == ["F #1", "A #1", "F #2"]
    recent = body["recent_videos"][0]
    assert recent["url"].startswith("http://testserver/media/") and recent["thumbnail_url"]
    assert recent["duration"] == 15.0

    usage = body["usage"]
    assert usage["total_usd"] == pytest.approx(0.5 + 3.2 + 0.02 + 1.0)
    assert usage["month_usd"] == pytest.approx(0.5 + 3.2 + 0.02)
    assert usage["clips_generated"] == 3
    assert usage["clips_reused"] == 2
    assert usage["saved_usd"] == pytest.approx(0.5 + 1.0)
    by_provider = {p["provider"]: p for p in usage["by_provider"]}
    assert set(by_provider) == {"kling", "veo", "anthropic"}
    assert by_provider["kling"] == {"provider": "kling", "amount_usd": pytest.approx(1.5), "clips": 2}
    assert by_provider["veo"]["clips"] == 1 and by_provider["anthropic"]["clips"] == 0
    assert [p["provider"] for p in usage["by_provider"]] == ["veo", "kling", "anthropic"]
    assert body["pending_estimate_usd"] == pytest.approx(1.5)
