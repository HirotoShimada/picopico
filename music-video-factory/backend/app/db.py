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
from sqlalchemy import Column, Engine, ForeignKey, Table, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import AddConstraint

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
    """Create all tables, then apply the additive upgrades newer versions introduced (MVP: no Alembic yet).

    Every step is idempotent: on an up-to-date database they only inspect the schema.
    """
    import app.models  # noqa: F401  (registers the mappers on Base.metadata)

    target = bind or engine
    Base.metadata.create_all(bind=target)
    relax_foreign_keys(target)
    add_missing_columns(target)
    backfill_columns(target)


def add_missing_columns(bind: Engine) -> list[str]:
    """Additive schema upgrade: ``ALTER TABLE … ADD COLUMN`` for nullable model columns an existing table lacks.

    ``create_all`` never alters existing tables, so a database created by an
    older version would miss columns added since (e.g. ``projects.artist_aliases``).
    Only nullable columns are added (with their ``REFERENCES … ON DELETE …`` clause
    for a single-column foreign key, e.g. ``cost_records.owner_id``, and their
    index); anything else is logged and needs a manual migration. Returns
    ``table.column`` names added.
    """
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    preparer = bind.dialect.identifier_preparer
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable or column.primary_key or len(column.foreign_keys) > 1:
                logger.error("column %s.%s is missing and cannot be added automatically", table.name, column.name)
                continue
            ddl = column.type.compile(dialect=bind.dialect)
            for fk in column.foreign_keys:  # NULL default, so SQLite accepts REFERENCES in ADD COLUMN
                target = fk.column
                ddl += f" REFERENCES {preparer.quote(target.table.name)} ({preparer.quote(target.name)})"
                if fk.ondelete:
                    ddl += f" ON DELETE {fk.ondelete}"
            with bind.begin() as connection:
                connection.execute(
                    text(f"ALTER TABLE {preparer.quote(table.name)} ADD COLUMN {preparer.quote(column.name)} {ddl}")
                )
                for index in table.indexes:
                    if column in index.columns.values():
                        index.create(connection, checkfirst=True)
            added.append(f"{table.name}.{column.name}")
            logger.warning("added missing column %s.%s (%s)", table.name, column.name, ddl)
    return added


# Foreign-key columns that older versions created ``NOT NULL`` / with another ``ON DELETE`` action than the
# model declares now: ``cost_records.project_id`` was ``ON DELETE CASCADE``, so deleting a project erased its
# usage history; it is now nullable with ``ON DELETE SET NULL``.
RELAXED_FOREIGN_KEYS: tuple[tuple[str, str], ...] = (("cost_records", "project_id"),)


def relax_foreign_keys(bind: Engine) -> list[str]:
    """Make the :data:`RELAXED_FOREIGN_KEYS` columns of an existing database match the model.

    PostgreSQL: ``DROP NOT NULL`` and the foreign key re-created with the model's
    ``ON DELETE`` action, in one transaction. SQLite cannot alter a column or a
    constraint, so the table is rebuilt (renamed, re-created from the model,
    rows copied, old table dropped) inside an explicit transaction. Idempotent:
    a matching column is left alone. Returns ``table.column`` names changed.
    """
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    changed: list[str] = []
    for table_name, column_name in RELAXED_FOREIGN_KEYS:
        if table_name not in existing_tables:
            continue
        table = Base.metadata.tables[table_name]
        (fk,) = table.c[column_name].foreign_keys
        wanted = (fk.ondelete or "NO ACTION").upper()
        db_column = next(c for c in inspector.get_columns(table_name) if c["name"] == column_name)
        db_fk = next(
            (f for f in inspector.get_foreign_keys(table_name) if f["constrained_columns"] == [column_name]), None
        )
        current = str(((db_fk or {}).get("options") or {}).get("ondelete") or "NO ACTION").upper()
        if db_column["nullable"] and db_fk is not None and current == wanted:
            continue
        if bind.dialect.name == "sqlite":
            _rebuild_sqlite_table(bind, table)
        elif bind.dialect.name == "postgresql":
            _relax_postgres_foreign_key(bind, table, column_name, fk, db_fk)
        else:
            logger.error("cannot relax %s.%s on %s automatically", table_name, column_name, bind.dialect.name)
            continue
        changed.append(f"{table_name}.{column_name}")
        logger.warning("relaxed %s.%s to NULL / ON DELETE %s", table_name, column_name, wanted)
    return changed


def _relax_postgres_foreign_key(
    bind: Engine, table: Table, column_name: str, fk: ForeignKey, db_fk: dict[str, Any] | None
) -> None:
    preparer = bind.dialect.identifier_preparer
    quoted = preparer.quote(table.name)
    with bind.begin() as connection:
        connection.execute(text(f"ALTER TABLE {quoted} ALTER COLUMN {preparer.quote(column_name)} DROP NOT NULL"))
        if db_fk is not None and db_fk.get("name"):
            connection.execute(text(f"ALTER TABLE {quoted} DROP CONSTRAINT {preparer.quote(db_fk['name'])}"))
        connection.execute(AddConstraint(fk.constraint))  # type: ignore[arg-type]


def _rebuild_sqlite_table(bind: Engine, table: Table) -> None:
    preparer = bind.dialect.identifier_preparer
    inspector = inspect(bind)
    old_columns = {c["name"] for c in inspector.get_columns(table.name)}
    old_indexes = [i["name"] for i in inspector.get_indexes(table.name) if i.get("name")]
    common = [c for c in table.columns if c.name in old_columns]
    columns = ", ".join(preparer.quote(c.name) for c in common)
    values = ", ".join(_copy_expression(preparer, c) for c in common)
    old_name = preparer.quote(f"_old_{table.name}")
    new_name = preparer.quote(table.name)
    with bind.begin() as connection:
        # pysqlite does not open a transaction for DDL; without BEGIN a failure would leave a half-done rebuild.
        connection.exec_driver_sql("BEGIN")
        for index_name in old_indexes:
            connection.execute(text(f"DROP INDEX {preparer.quote(index_name)}"))
        connection.execute(text(f"ALTER TABLE {new_name} RENAME TO {old_name}"))
        table.create(connection)
        connection.execute(text(f"INSERT INTO {new_name} ({columns}) SELECT {values} FROM {old_name}"))
        connection.execute(text(f"DROP TABLE {old_name}"))


def _copy_expression(preparer: Any, column: Column[Any]) -> str:
    """Copied value of ``column``; a nullable reference to a missing row becomes NULL (it would fail the FK)."""
    name = preparer.quote(column.name)
    if not column.nullable or len(column.foreign_keys) != 1:
        return name
    target = next(iter(column.foreign_keys)).column
    parent = f"SELECT {preparer.quote(target.name)} FROM {preparer.quote(target.table.name)}"
    return f"CASE WHEN {name} IN ({parent}) THEN {name} END"


def backfill_columns(bind: Engine) -> int:
    """Fill columns that upgraded databases added empty: ``cost_records.owner_id`` from the project's owner.

    Idempotent (only rows still NULL with a project are touched). Returns the number of rows updated.
    """
    if "cost_records" not in set(inspect(bind).get_table_names()):
        return 0
    with bind.begin() as connection:
        result = connection.execute(
            text(
                "UPDATE cost_records SET owner_id = "
                "(SELECT projects.owner_id FROM projects WHERE projects.id = cost_records.project_id) "
                "WHERE owner_id IS NULL AND project_id IS NOT NULL"
            )
        )
    updated = int(result.rowcount or 0)
    if updated:
        logger.warning("backfilled cost_records.owner_id for %d rows", updated)
    return updated
