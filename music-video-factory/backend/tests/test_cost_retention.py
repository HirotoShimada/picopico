"""Usage history outlives projects: ``CostRecord.project_id`` is nullable (ON DELETE SET NULL), ``owner_id`` keeps
records attributed, the dashboard aggregates by owner, and ``init_db`` upgrades databases created before."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Session

from app.models import CostRecord, Project, User, utcnow


def _cost(project_id: str, amount: float, provider: str = "veo") -> CostRecord:
    return CostRecord(
        project_id=project_id,
        kind="video_generation",
        provider=provider,
        model=f"{provider}-model",
        units=amount * 10,
        unit="second",
        unit_price_usd=0.1,
        amount_usd=amount,
    )


def test_owner_is_filled_from_the_project(db: Session, make_project) -> None:
    project = make_project()
    record = _cost(project.id, 1.0)
    db.add(record)
    db.commit()
    assert record.owner_id == project.owner_id


def test_deleting_a_project_keeps_its_usage_history(client: TestClient, db: Session, local_user: User) -> None:
    project_id = client.post("/api/projects", json={"title": "削除される曲", "artist_name": "A"}).json()["id"]
    other = User(name="other")
    db.add(other)
    db.flush()
    foreign = Project(owner_id=other.id, title="他人の曲", artist_name="B")
    db.add(foreign)
    db.flush()
    db.add_all([_cost(project_id, 1.5), _cost(project_id, 0.25, provider="runway"), _cost(foreign.id, 9.0)])
    db.commit()
    before = client.get("/api/dashboard").json()["usage"]
    assert before["total_usd"] == pytest.approx(1.75) and before["clips_generated"] == 2

    assert client.delete(f"/api/projects/{project_id}").status_code == 204

    db.expire_all()
    assert db.get(Project, project_id) is None
    kept = db.scalars(select(CostRecord).where(CostRecord.owner_id == local_user.id)).all()
    assert len(kept) == 2 and all(r.project_id is None for r in kept)
    after = client.get("/api/dashboard").json()["usage"]
    assert after == before  # totals, month, per-provider rows and counts are unchanged
    assert {row["provider"] for row in after["by_provider"]} == {"veo", "runway"}


# --- upgrading a database created before owner_id / SET NULL existed ----------------------------------------


@pytest.fixture
def scratch_engine(tmp_path: Path) -> Iterator[Engine]:
    """An empty database of the test dialect: a SQLite file, or a throw-away PostgreSQL schema."""
    from app.db import make_engine

    url = os.environ["DATABASE_URL"]
    if url.startswith("sqlite"):
        engine = make_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
        yield engine
        engine.dispose()
        return
    schema = f"mvf_upgrade_{uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _create_old_cost_records(engine: Engine) -> Table:
    """``cost_records`` as versions before the fix created it: project_id NOT NULL + ON DELETE CASCADE, no owner."""
    from app.db import Base

    Base.metadata.tables["cost_records"].drop(engine)
    metadata = MetaData()
    for name in ("projects", "batches", "clips"):
        Table(name, metadata, autoload_with=engine)
    old = Table(
        "cost_records",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("project_id", String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        Column("batch_id", String(32), ForeignKey("batches.id", ondelete="SET NULL"), index=True),
        Column("clip_id", String(32), ForeignKey("clips.id", ondelete="SET NULL"), index=True),
        Column("kind", String(24), nullable=False),
        Column("provider", String(32), nullable=False),
        Column("model", String(100)),
        Column("units", Float, nullable=False),
        Column("unit", String(16), nullable=False),
        Column("unit_price_usd", Float, nullable=False),
        Column("amount_usd", Float, nullable=False),
        Column("cached", Boolean, nullable=False),
        Column("note", Text),
        Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    )
    old.create(engine)
    return old


def _old_row(project_id: str, amount: float) -> dict[str, object]:
    return {
        "id": uuid4().hex,
        "project_id": project_id,
        "kind": "llm",
        "provider": "anthropic",
        "units": 1000.0,
        "unit": "token",
        "unit_price_usd": amount / 1000,
        "amount_usd": amount,
        "cached": False,
        "created_at": utcnow(),
    }


def test_init_db_upgrades_cost_records_of_an_older_database(scratch_engine: Engine) -> None:
    from app.db import add_missing_columns, backfill_columns, init_db, relax_foreign_keys

    engine = scratch_engine
    init_db(engine)
    old = _create_old_cost_records(engine)
    with Session(engine) as session:
        owner = User(name="owner")
        session.add(owner)
        session.flush()
        projects = [Project(owner_id=owner.id, title=f"曲{i}", artist_name="A") for i in range(2)]
        session.add_all(projects)
        session.commit()
        owner_id, (kept_id, deleted_id) = owner.id, [p.id for p in projects]
    with engine.begin() as connection:
        connection.execute(old.insert(), [_old_row(kept_id, 0.5), _old_row(deleted_id, 0.25)])
    columns = {c["name"]: c for c in inspect(engine).get_columns("cost_records")}
    assert "owner_id" not in columns and columns["project_id"]["nullable"] is False

    init_db(engine)

    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("cost_records")}
    assert columns["project_id"]["nullable"] is True and columns["owner_id"]["nullable"] is True
    fks = {tuple(fk["constrained_columns"]): fk for fk in inspector.get_foreign_keys("cost_records")}
    assert fks[("project_id",)]["options"].get("ondelete", "").upper() == "SET NULL"
    assert fks[("owner_id",)]["referred_table"] == "users"
    indexes = {tuple(i["column_names"]) for i in inspector.get_indexes("cost_records")}
    assert {("owner_id",), ("project_id",), ("batch_id",), ("clip_id",), ("created_at",)} <= indexes
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT project_id, owner_id, amount_usd FROM cost_records")).all()
    assert sorted(rows, key=lambda r: r[2]) == [(deleted_id, owner_id, 0.25), (kept_id, owner_id, 0.5)]

    # Idempotent: a second start-up changes nothing.
    assert relax_foreign_keys(engine) == [] and add_missing_columns(engine) == [] and backfill_columns(engine) == 0

    # The database itself now keeps the records of a deleted project.
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": deleted_id})
    with Session(engine) as session:
        records = session.scalars(select(CostRecord).order_by(CostRecord.amount_usd)).all()
        assert [(r.project_id, r.owner_id) for r in records] == [(None, owner_id), (kept_id, owner_id)]
        session.add(_cost(kept_id, 2.0))  # new rows get their owner from the project
        session.commit()
        assert session.scalar(select(CostRecord.owner_id).where(CostRecord.amount_usd == 2.0)) == owner_id


def test_sqlite_rebuild_drops_dangling_references(tmp_path: Path) -> None:
    from app.db import init_db, make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'dangling.sqlite3'}")
    init_db(engine)
    old = _create_old_cost_records(engine)
    with engine.connect() as connection:  # rows written while foreign keys were not enforced
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(old.insert(), [_old_row("f" * 32, 1.0)])
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    init_db(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT project_id, owner_id, amount_usd FROM cost_records")).all() == [
            (None, None, 1.0)
        ]
    engine.dispose()
