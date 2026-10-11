from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

import pytest

from app import worker


@pytest.fixture
def clean_registry() -> Iterator[None]:
    saved = worker.registered_tasks()
    yield
    worker._registry.clear()
    worker._registry.update(saved)


def test_inline_enqueue_runs_on_thread_pool(clean_registry: None) -> None:
    seen: dict[str, object] = {}

    def add(a: int, b: int) -> int:
        seen["thread"] = threading.current_thread().name
        return a + b

    assert worker.register_task("test.add", add) is add
    assert worker.get_task("test.add") is add
    future = worker.enqueue("test.add", 2, 3)
    assert future is not None
    assert future.result(timeout=10) == 5
    assert str(seen["thread"]).startswith("mvf-task")


def test_enqueue_unregistered_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        assert worker.enqueue("test.missing", "p1") is None
    assert "no task registered" in caplog.text


def test_failing_task_is_logged_and_surfaces_in_future(clean_registry: None, caplog: pytest.LogCaptureFixture) -> None:
    def boom(project_id: str) -> None:
        raise ValueError(f"bad {project_id}")

    worker.register_task("test.boom", boom)
    with caplog.at_level(logging.ERROR, logger="app.worker"):
        future = worker.enqueue("test.boom", "p1")
        with pytest.raises(ValueError, match="bad p1"):
            future.result(timeout=10)
    assert "test.boom" in caplog.text


def test_pool_is_bounded_to_two_workers(clean_registry: None) -> None:
    lock = threading.Lock()
    running = 0
    peak = 0
    release = threading.Event()

    def slow() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(5)
        with lock:
            running -= 1

    worker.register_task("test.slow", slow)
    futures = [worker.enqueue("test.slow") for _ in range(4)]
    threading.Timer(0.3, release.set).start()
    for future in futures:
        future.result(timeout=10)
    assert peak == worker.INLINE_MAX_WORKERS == 2


def test_celery_mode_without_celery_task_falls_back(clean_registry: None, set_env, caplog) -> None:
    # Only the pipeline tasks (mvf.analyze / mvf.run_batch / mvf.render_video) exist in Celery.
    set_env(TASK_MODE="celery")
    worker.register_task("test.echo", lambda x: x)
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        assert worker.enqueue("test.echo", 7).result(timeout=10) == 7
    assert "has no Celery task" in caplog.text


def test_shutdown_recreates_executor(clean_registry: None) -> None:
    worker.register_task("test.one", lambda: 1)
    assert worker.enqueue("test.one").result(timeout=10) == 1
    worker.shutdown(wait=True)
    assert worker.enqueue("test.one").result(timeout=10) == 1
