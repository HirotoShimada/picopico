"""Shared pytest fixtures for the backend.

The test environment is configured here *before* any ``app`` module is
imported: PostgreSQL database ``mvf_test`` (override with ``TEST_DATABASE_URL``),
throw-away storage/work directories, dev auth, inline tasks, a fixed
``SECRET_KEY`` and blanked AI-provider keys (tests must never hit paid APIs;
set ``MVF_TEST_KEEP_KEYS=1`` to keep keys from the environment).

Fixtures
- ``db_engine`` (session): drop_all + create_all once per session.
- ``db``: a Session on a freshly truncated database.
- ``client``: ``TestClient`` for a new app (lifespan run) on a clean database.
- ``make_client(**env)``: like ``client`` but with environment overrides
  (e.g. ``AUTH_MODE="token"``).
- ``set_env(**env)``: apply env overrides for this test and return fresh settings.
- ``enqueued``: records ``worker.enqueue`` calls instead of running tasks.
- ``demo_dir``: ``backend/var/demo`` (generated on first use).
- ``storage``: the configured storage backend (local, temp dir).
- ``local_user`` / ``make_project``: DB helpers for the dev-mode user.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEMO_DIR = BACKEND_DIR / "var" / "demo"
DEMO_FILES = (
    "demo_song.wav",
    "demo_song.mp3",
    "demo_lyrics_ja.txt",
    "demo_lyrics_ja.lrc",
    "clip_01.mp4",
    "clip_02.mp4",
    "clip_03.mp4",
)

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="mvf-tests-"))
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://mvf:mvf@127.0.0.1:5432/mvf_test")
TEST_SECRET_KEY = "test-secret-key-not-for-production"

os.environ.update(
    {
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15"),
        "TASK_MODE": "inline",
        "STORAGE_BACKEND": "local",
        "STORAGE_DIR": str(_TMP_ROOT / "storage"),
        "WORK_DIR": str(_TMP_ROOT / "work"),
        "PUBLIC_API_BASE_URL": "http://testserver",
        "CORS_ORIGINS": "http://localhost:3000",
        "AUTH_MODE": "dev",
        "API_TOKENS": "",
        "SECRET_KEY": TEST_SECRET_KEY,
        "MEDIA_URL_TTL": "3600",
        "MAX_AUDIO_MB": "100",
        "MAX_VIDEO_MB": "500",
    }
)
if os.environ.get("MVF_TEST_KEEP_KEYS") != "1":
    for _key in (
        "ANTHROPIC_API_KEY",
        "KLING_API_KEY",
        "KLING_ACCESS_KEY",
        "KLING_SECRET_KEY",
        "RUNWAY_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    ):
        os.environ[_key] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, text, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def _clear_caches() -> None:
    from app.config import get_settings
    from app.services.providers.openrouter import reset_model_cache
    from app.storage import get_storage

    reset_model_cache()  # OpenRouter's /videos/models listing (memory + WORK_DIR file); reads settings
    get_settings.cache_clear()
    get_storage.cache_clear()


def truncate_all(engine: Engine) -> None:
    """Remove every row from every table (fast per-test isolation)."""
    from app.db import Base
    from app.models import Project

    tables = list(Base.metadata.sorted_tables)
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            names = ", ".join(f'"{t.name}"' for t in tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        else:
            conn.execute(update(Project).values(audio_asset_id=None))
            for table in reversed(tables):
                conn.execute(table.delete())


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings/storage are cached per process; rebuild them around every test."""
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    import app.models  # noqa: F401
    from app.db import Base, get_engine

    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Session]:
    from app.db import SessionLocal

    truncate_all(db_engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    def _set(**env: Any) -> Any:
        from app.config import get_settings

        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        _clear_caches()
        return get_settings()

    return _set


@pytest.fixture
def make_client(db: Session, set_env: Callable[..., Any]) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(**env: Any) -> TestClient:
        from app.main import create_app

        if env:
            set_env(**env)
        test_client = TestClient(create_app())
        test_client.__enter__()
        clients.append(test_client)
        return test_client

    yield _make
    for test_client in reversed(clients):
        test_client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    from app import worker

    calls: list[tuple[Any, ...]] = []

    def _record(name: str, *args: Any) -> None:
        calls.append((name, *args))

    monkeypatch.setattr(worker, "enqueue", _record)
    return calls


@pytest.fixture(scope="session")
def demo_dir() -> Path:
    if not all((DEMO_DIR / name).is_file() for name in DEMO_FILES):
        subprocess.run(
            [sys.executable, str(BACKEND_DIR / "scripts" / "make_demo_assets.py")],
            check=True,
            cwd=BACKEND_DIR,
            timeout=600,
        )
    return DEMO_DIR


@pytest.fixture
def storage() -> Any:
    from app.storage import get_storage

    return get_storage()


@pytest.fixture
def local_user(db: Session) -> Any:
    from app.security import get_local_user

    return get_local_user(db)


@pytest.fixture
def make_project(db: Session, local_user: Any) -> Callable[..., Any]:
    def _make(**fields: Any) -> Any:
        from app.models import Project

        values = {"title": "テスト曲", "artist_name": "テストアーティスト", **fields}
        project = Project(owner_id=values.pop("owner_id", local_user.id), **values)
        db.add(project)
        db.commit()
        return project

    return _make


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


# --- pipeline / API integration fixtures (used by test_pipeline.py and test_api_*.py) ----------------------------


@pytest.fixture
def sync_tasks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Run ``worker.enqueue``d tasks synchronously, in order, and record the calls."""
    import app.pipeline
    from app import worker

    calls: list[tuple[Any, ...]] = []

    def _run(name: str, *args: Any) -> None:
        calls.append((name, *args))
        fn = worker.get_task(name)
        if fn is not None:
            fn(*args)

    monkeypatch.setattr(worker, "enqueue", _run)
    monkeypatch.setattr(app.pipeline, "_sleep", lambda _s: None)
    return calls


@pytest.fixture(scope="session")
def canned_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A valid 5 s 1080x1920 30 fps H.264/AAC faststart MP4 (+ ``.jpg`` thumbnail next to it)."""
    out = tmp_path_factory.mktemp("canned") / "canned.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-c:a", "aac", "-ac", "2", "-movflags", "+faststart", "-shortest", str(out),
        ],
        check=True,
        timeout=120,
    )  # fmt: skip
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "1", "-i", str(out), "-frames:v", "1",
         "-vf", "scale=540:960", str(out.with_suffix(".jpg"))],
        check=True,
        timeout=60,
    )  # fmt: skip
    return out


class FakeRender:
    """Stands in for ``app.services.render.render_video``: copies the canned 5 s MP4.

    ``fail`` (exception) is raised instead of rendering; ``before`` is called with
    the spec and progress callback first (e.g. to cancel mid-render).
    """

    def __init__(self, canned: Path):
        self.canned = canned
        self.specs: list[Any] = []
        self.fail: BaseException | None = None
        self.before: Callable[[Any, Any], None] | None = None

    def __call__(self, spec: Any, progress_cb: Any = None) -> Any:
        from app.services.render import RenderResult

        self.specs.append(spec)
        if progress_cb is not None:
            progress_cb(0.0, "prepare")
        if self.before is not None:
            self.before(spec, progress_cb)
        if self.fail is not None:
            raise self.fail
        if progress_cb is not None:
            progress_cb(0.5, "encode")
        out = Path(spec.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.canned, out)
        thumb = out.with_name(f"{out.stem}_thumb.jpg")
        shutil.copyfile(self.canned.with_suffix(".jpg"), thumb)
        return RenderResult(str(out), str(thumb), 5.0, out.stat().st_size, "", {"total": 0.01})


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch, canned_video: Path) -> FakeRender:
    from app.services import render as render_service

    fake = FakeRender(canned_video)
    monkeypatch.setattr(render_service, "render_video", fake)
    return fake


class FakeClipGenerator:
    """Stands in for ``MockProvider.generate``: copies a demo clip (fast, deterministic).

    ``script`` maps the 0-based call number to an exception to raise; ``hook``
    is called with ``(call_number, request, cancel_check)`` before generating.
    Cost = model price × requested duration (so priced mocks behave like a paid API).
    """

    def __init__(self, source: Path):
        self.source = source
        self.calls: list[Any] = []
        self.script: dict[int, BaseException] = {}
        self.hook: Callable[[int, Any, Any], None] | None = None

    def __call__(self, provider: Any, req: Any, out_path: str, *, timeout: float = 900,
                 on_progress: Any = None, cancel_check: Any = None) -> Any:  # fmt: skip
        from app.services.providers import ClipResult, ProviderCancelled

        number = len(self.calls)
        self.calls.append(req)
        if self.hook is not None:
            self.hook(number, req, cancel_check)
        if cancel_check is not None and cancel_check():
            raise ProviderCancelled()
        if number in self.script:
            raise self.script[number]
        model = provider.get_model(req.model)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, out_path)
        return ClipResult(
            path=out_path, duration=6.0, width=720, height=1280, model=model.id,
            cost_usd=round(model.price_per_second_usd * req.duration, 4), external_job_id=f"fake-{number}",
            raw={"requested_duration": req.duration},
        )  # fmt: skip


@pytest.fixture
def fake_clips(monkeypatch: pytest.MonkeyPatch, demo_dir: Path) -> FakeClipGenerator:
    from app.services.providers.mock import MockProvider

    fake = FakeClipGenerator(demo_dir / "clip_01.mp4")

    def _generate(self: Any, req: Any, out_path: str, **kwargs: Any) -> Any:
        return fake(self, req, out_path, **kwargs)

    monkeypatch.setattr(MockProvider, "generate", _generate)
    return fake


@pytest.fixture
def priced_mock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Give the mock provider a price per second (cost tables are rebuilt)."""
    from app.services import cost as cost_service
    from app.services.providers.mock import MockProvider

    original = MockProvider.models

    def _set(price: float) -> None:
        def models(self: Any) -> list[Any]:
            entries = original(self)
            for entry in entries:
                entry.price_per_second_usd = price
            return entries

        monkeypatch.setattr(MockProvider, "models", models)
        monkeypatch.setattr(cost_service, "PRICING", cost_service.build_pricing())

    return _set


@pytest.fixture(scope="session")
def demo_analysis(demo_dir: Path) -> dict[str, Any]:
    from app.services.audio_analysis import analyze_audio

    return analyze_audio(demo_dir / "demo_song.mp3")


ALL_RIGHTS = {
    "audio_rights": True,
    "lyrics_rights": True,
    "no_real_person_likeness": True,
    "model_terms_checked": True,
    "platform_terms": True,
}


@pytest.fixture
def ready_project(
    db: Session, local_user: Any, storage: Any, demo_dir: Path, demo_analysis: dict[str, Any]
) -> Any:
    """Project with the demo song stored, analysis done, timed LRC lyrics and all rights confirmed."""
    from app.models import Asset, Project, new_id, utcnow
    from app.services.lyrics_parse import parse_lyrics
    from app.storage import make_key

    project = Project(owner_id=local_user.id, title="夜明けのうた", artist_name="デモアーティスト")
    db.add(project)
    db.flush()
    asset = Asset(
        id=new_id(), project_id=project.id, owner_id=local_user.id, kind="audio", filename="demo_song.mp3",
        content_type="audio/mpeg", duration=demo_analysis["duration"],
    )  # fmt: skip
    asset.storage_key = make_key(project.id, "audio", asset.id, ".mp3")
    asset.size_bytes = storage.put_file(asset.storage_key, demo_dir / "demo_song.mp3")
    db.add(asset)
    lines, _ = parse_lyrics((demo_dir / "demo_lyrics_ja.lrc").read_text(encoding="utf-8"), "lrc")
    project.audio_asset = asset
    project.analysis = demo_analysis
    project.analysis_status = "done"
    project.segment_start, project.segment_end = 24.033, 39.033
    project.lyrics_text = (demo_dir / "demo_lyrics_ja.lrc").read_text(encoding="utf-8")
    project.lyrics_format = "lrc"
    project.lyric_lines = [dict(line) for line in lines]
    project.rights = dict(ALL_RIGHTS)
    project.rights_confirmed_at = utcnow()
    db.commit()
    return project


@pytest.fixture
def add_upload_clips(db: Session, storage: Any, demo_dir: Path) -> Callable[..., list[Any]]:
    """Store the demo clips as ready ``upload`` clips of a project."""

    def _add(project: Any, names: tuple[str, ...] = ("clip_01.mp4", "clip_02.mp4", "clip_03.mp4")) -> list[Any]:
        from app.models import Asset, Clip, new_id
        from app.services.media import probe
        from app.storage import make_key

        clips = []
        for name in names:
            info = probe(demo_dir / name)
            asset = Asset(
                id=new_id(), project_id=project.id, owner_id=project.owner_id, kind="clip", filename=name,
                content_type="video/mp4", duration=info["duration"], width=info["width"], height=info["height"],
            )  # fmt: skip
            asset.storage_key = make_key(project.id, "clip", asset.id, ".mp4")
            asset.size_bytes = storage.put_file(asset.storage_key, demo_dir / name)
            clip = Clip(
                project_id=project.id, source="upload", provider="upload", status="ready", asset=asset,
                duration=info["duration"], width=info["width"], height=info["height"],
            )  # fmt: skip
            db.add_all([asset, clip])
            clips.append(clip)
        db.commit()
        return clips

    return _add
