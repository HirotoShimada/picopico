"""Database engine, session factory and declarative base.

PostgreSQL is the default; ``sqlite:///...`` URLs also work (tests, scripts).

Prefer :func:`get_engine` over importing ``engine`` directly: tests and scripts
may rebind the engine with :func:`set_engine`, which also rebinds
``SessionLocal`` so existing ``from app.db import SessionLocal`` imports keep
working.

Background tasks open their own session::

    with SessionLocal() as db:
        ...
        db.commit()
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """Create an engine with dialect-appropriate options."""
    kwargs: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:") or "mode=memory" in url:
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)
        event.listen(engine, "connect", _sqlite_on_connect)
        return engine
    if url.startswith("postgresql"):
        kwargs["connect_args"] = {"connect_timeout": 5}
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return create_engine(url, **kwargs)


def _sqlite_on_connect(dbapi_connection: Any, _record: Any) -> None:
    # SQLite ignores foreign keys (and ON DELETE actions) unless enabled per connection.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


engine: Engine = make_engine(get_settings().DATABASE_URL)
SessionLocal: sessionmaker[Session] = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_engine() -> Engine:
    return engine


def set_engine(new_engine: Engine) -> Engine:
    """Rebind the module engine and ``SessionLocal``; returns the previous engine."""
    global engine
    previous = engine
    engine = new_engine
    SessionLocal.configure(bind=new_engine)
    return previous


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


def init_db(bind: Engine | None = None) -> None:
    """Create all tables, then add columns newer versions introduced (MVP: no Alembic yet)."""
    import app.models  # noqa: F401  (registers the mappers on Base.metadata)

    target = bind or engine
    Base.metadata.create_all(bind=target)
    add_missing_columns(target)


def add_missing_columns(bind: Engine) -> list[str]:
    """Additive schema upgrade: ``ALTER TABLE … ADD COLUMN`` for nullable model columns an existing table lacks.

    ``create_all`` never alters existing tables, so a database created by an
    older version would miss columns added since (e.g. ``projects.artist_aliases``).
    Only nullable columns without server-side constraints are added; anything
    else is logged and needs a manual migration. Returns ``table.column`` names added.
    """
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable or column.primary_key or column.foreign_keys:
                logger.error("column %s.%s is missing and cannot be added automatically", table.name, column.name)
                continue
            ddl = column.type.compile(dialect=bind.dialect)
            preparer = bind.dialect.identifier_preparer
            with bind.begin() as connection:
                connection.execute(
                    text(f"ALTER TABLE {preparer.quote(table.name)} ADD COLUMN {preparer.quote(column.name)} {ddl}")
                )
            added.append(f"{table.name}.{column.name}")
            logger.warning("added missing column %s.%s (%s)", table.name, column.name, ddl)
    return added
