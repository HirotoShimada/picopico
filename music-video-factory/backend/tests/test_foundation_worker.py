from __future__ import annotations

import logging
import threading
import time
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


def test_inline_pools_keep_analysis_flowing_while_batches_generate(clean_registry: None) -> None:
    """run_batch waits on provider APIs for minutes; it must not occupy the analysis/render threads."""
    release = threading.Event()
    started: dict[str, float] = {}

    def slow_batch(batch_id: str) -> None:
        release.wait(5)

    def analyze(project_id: str) -> None:
        started[project_id] = time.monotonic()

    worker.register_task("run_batch", slow_batch)
    worker.register_task("analyze", analyze)
    try:
        batches = [worker.enqueue("run_batch", f"b{i}") for i in range(2)]
        begin = time.monotonic()
        worker.enqueue("analyze", "p1").result(timeout=5)
        assert started["p1"] - begin < 0.5
    finally:
        release.set()
    for future in batches:
        future.result(timeout=10)


def test_celery_routes_run_batch_to_its_own_queue() -> None:
    queues = worker.celery_app.amqp.queues
    assert set(queues) == {worker.DEFAULT_QUEUE, worker.BATCH_QUEUE}  # a worker without -Q consumes both
    route = worker.celery_app.amqp.router.route
    assert route({}, worker.CELERY_TASK_NAMES["run_batch"])["queue"].name == worker.BATCH_QUEUE
    assert route({}, worker.CELERY_TASK_NAMES["render_video"])["queue"].name == worker.DEFAULT_QUEUE
    assert queues[worker.BATCH_QUEUE].routing_key != queues[worker.DEFAULT_QUEUE].routing_key


def test_worker_ready_runs_the_recovery_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import pipeline

    seen: list[float | None] = []
    monkeypatch.setattr(pipeline, "recover_interrupted_jobs", lambda *, stale_after=None: seen.append(stale_after))
    worker._recover_after_worker_start(sender=None)
    assert seen == [pipeline.STALE_TASK_SEC]
