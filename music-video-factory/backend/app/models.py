"""SQLAlchemy ORM models (ARCHITECTURE.md §4).

Conventions
- IDs are ``uuid4().hex`` strings (``String(32)``).
- Timestamps are timezone-aware UTC (:class:`UTCDateTime` also normalises the
  naive values SQLite returns).
- JSON columns use ``JSONB`` on PostgreSQL and ``JSON`` elsewhere. Dict/list
  columns are wrapped in ``MutableDict``/``MutableList`` so *top-level* in-place
  changes (``project.rights["x"] = True``) are tracked; for nested changes
  reassign the value or call ``sqlalchemy.orm.attributes.flag_modified``.
- Status/kind columns are plain strings; the ``StrEnum`` classes below list the
  allowed values (they compare equal to the raw strings).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that always round-trips aware UTC datetimes."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _json_type() -> Any:
    return JSON().with_variant(JSONB(), "postgresql")


# ``as_mutable`` binds to the type *instance*, so dicts and lists need separate ones.
JSONDict = MutableDict.as_mutable(_json_type())
JSONList = MutableList.as_mutable(_json_type())

ID = String(32)


# --- enumerations ---------------------------------------------------------------


class AnalysisStatus(StrEnum):
    NONE = "none"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class LyricsFormat(StrEnum):
    TXT = "txt"
    LRC = "lrc"
    SRT = "srt"


class AssetKind(StrEnum):
    AUDIO = "audio"
    LYRICS = "lyrics"
    MATERIAL = "material"
    CLIP = "clip"
    RENDER = "render"
    THUMBNAIL = "thumbnail"
    ZIP = "zip"


class ClipSource(StrEnum):
    UPLOAD = "upload"
    MOCK = "mock"
    KLING = "kling"
    RUNWAY = "runway"
    VEO = "veo"


class ClipStatus(StrEnum):
    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    GENERATING_CLIPS = "generating_clips"
    RENDERING = "rendering"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


class VideoStatus(StrEnum):
    QUEUED = "queued"
    WAITING_CLIPS = "waiting_clips"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CostKind(StrEnum):
    VIDEO_GENERATION = "video_generation"
    LLM = "llm"


class CostUnit(StrEnum):
    SECOND = "second"
    TOKEN = "token"
    CLIP = "clip"


ACTIVE_BATCH_STATUSES: frozenset[str] = frozenset(
    {BatchStatus.QUEUED, BatchStatus.PLANNING, BatchStatus.GENERATING_CLIPS, BatchStatus.RENDERING}
)
ACTIVE_VIDEO_STATUSES: frozenset[str] = frozenset(
    {VideoStatus.QUEUED, VideoStatus.WAITING_CLIPS, VideoStatus.RENDERING}
)


# --- models -----------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    projects: Mapped[list[Project]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    artist_name: Mapped[str] = mapped_column(String(200))
    # Other spellings of the artist's name (romanised / English, e.g. "Kenshi Yonezu" for 米津玄師) that the
    # likeness guard also removes from prompts. Nullable so ``init_db`` can add it to existing databases.
    artist_aliases: Mapped[list[str] | None] = mapped_column(JSONList, default=list)
    mood: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    # projects <-> assets is a reference cycle; ``use_alter`` lets create_all/drop_all
    # order the DDL, ``post_update`` on the relationship orders the DML.
    audio_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL", use_alter=True, name="fk_projects_audio_asset_id")
    )
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    analysis_status: Mapped[str] = mapped_column(String(16), default=AnalysisStatus.NONE)
    analysis_error: Mapped[str | None] = mapped_column(Text)

    lyrics_text: Mapped[str] = mapped_column(Text, default="")
    lyrics_format: Mapped[str] = mapped_column(String(8), default=LyricsFormat.TXT)
    lyric_lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONList, default=list)

    segment_start: Mapped[float | None] = mapped_column(Float)
    segment_end: Mapped[float | None] = mapped_column(Float)

    world_preset: Mapped[str] = mapped_column(String(32), default="cinematic")
    custom_world: Mapped[str | None] = mapped_column(Text)

    rights: Mapped[dict[str, bool]] = mapped_column(JSONDict, default=dict)
    rights_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    budget_limit_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    owner: Mapped[User] = relationship(back_populates="projects")
    audio_asset: Mapped[Asset | None] = relationship(foreign_keys=[audio_asset_id], post_update=True)
    assets: Mapped[list[Asset]] = relationship(
        back_populates="project",
        foreign_keys="Asset.project_id",
        cascade="all, delete-orphan",
    )
    clips: Mapped[list[Clip]] = relationship(back_populates="project", cascade="all, delete-orphan")
    batches: Mapped[list[Batch]] = relationship(back_populates="project", cascade="all, delete-orphan")
    videos: Mapped[list[Video]] = relationship(back_populates="project", cascade="all, delete")
    cost_records: Mapped[list[CostRecord]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    content_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="assets", foreign_keys=[project_id])

    __table_args__ = (Index("ix_assets_project_kind", "project_id", "kind"),)


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    scene_id: Mapped[str | None] = mapped_column(String(64))
    prompt: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default=ClipStatus.PENDING)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    thumbnail_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    external_job_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="clips")
    asset: Mapped[Asset | None] = relationship(foreign_keys=[asset_id])
    thumbnail_asset: Mapped[Asset | None] = relationship(foreign_keys=[thumbnail_asset_id])
    cost_records: Mapped[list[CostRecord]] = relationship(back_populates="clip")

    __table_args__ = (Index("ix_clips_project_status", "project_id", "status"),)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    estimate: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    storyboard: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    status: Mapped[str] = mapped_column(String(24), default=BatchStatus.QUEUED, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    project: Mapped[Project] = relationship(back_populates="batches")
    videos: Mapped[list[Video]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Video.index"
    )
    cost_records: Mapped[list[CostRecord]] = relationship(back_populates="batch")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default=VideoStatus.QUEUED, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    thumbnail_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    render_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    rendered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    project: Mapped[Project] = relationship(back_populates="videos")
    batch: Mapped[Batch] = relationship(back_populates="videos")
    asset: Mapped[Asset | None] = relationship(foreign_keys=[asset_id])
    thumbnail_asset: Mapped[Asset | None] = relationship(foreign_keys=[thumbnail_asset_id])


class CostRecord(Base):
    __tablename__ = "cost_records"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.id", ondelete="SET NULL"), index=True)
    clip_id: Mapped[str | None] = mapped_column(ForeignKey("clips.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    units: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), default=CostUnit.SECOND)
    unit_price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    project: Mapped[Project] = relationship(back_populates="cost_records")
    batch: Mapped[Batch | None] = relationship(back_populates="cost_records")
    clip: Mapped[Clip | None] = relationship(back_populates="cost_records")
