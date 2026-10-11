"""Background task registry, inline executor and Celery integration (ARCHITECTURE.md §7).

Task bodies are plain functions taking IDs (they open their own DB session)
and are registered by name (``app.pipeline`` registers ``analyze``,
``run_batch`` and ``render_video`` when imported)::

    register_task("analyze", analyze_project_audio)
    worker.enqueue("analyze", project_id)

``TASK_MODE=inline`` runs them on a process-wide ``ThreadPoolExecutor(max_workers=2)``.
``TASK_MODE=celery`` publishes them to Redis as Celery tasks ``mvf.<name>``
(:data:`celery_app`, ``acks_late`` + prefetch 1); a worker runs the very same
registered functions::

    cd backend && .venv/bin/celery -A app.worker.celery_app worker -l info -c 2

Names without a Celery task fall back to inline execution with a warning.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from celery import Celery
from celery.signals import worker_process_init

from app.config import get_settings
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)

TaskFn = Callable[..., Any]
Dispatcher = Callable[[str, TaskFn, tuple[Any, ...]], Any]

INLINE_MAX_WORKERS = 2

# Registered task name -> Celery task name.
CELERY_TASK_NAMES: dict[str, str] = {
    "analyze": "mvf.analyze",
    "run_batch": "mvf.run_batch",
    "render_video": "mvf.render_video",
}

_registry: dict[str, TaskFn] = {}
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


class QueueUnavailable(ServiceError):
    """The Celery broker (Redis) could not accept the task."""

    status_code = 503
    code = "queue_unavailable"


def register_task(name: str, fn: TaskFn) -> TaskFn:
    """Register ``fn`` under ``name`` (re-registering replaces it). Returns ``fn``."""
    _registry[name] = fn
    return fn


def get_task(name: str) -> TaskFn | None:
    return _registry.get(name)


def registered_tasks() -> dict[str, TaskFn]:
    return dict(_registry)


# --- inline -------------------------------------------------------------------------------------------------------


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=INLINE_MAX_WORKERS, thread_name_prefix="mvf-task")
        return _executor


def _run_logged(name: str, fn: TaskFn, args: tuple[Any, ...]) -> Any:
    try:
        return fn(*args)
    except Exception:
        logger.exception("background task %s%r failed", name, args)
        raise


def _dispatch_inline(name: str, fn: TaskFn, args: tuple[Any, ...]) -> Future[Any]:
    return _get_executor().submit(_run_logged, name, fn, args)


def _dispatch_celery_fallback(name: str, fn: TaskFn, args: tuple[Any, ...]) -> Future[Any]:
    logger.warning("TASK_MODE=celery but %r has no Celery task (mvf.<name>); running it inline", name)
    return _dispatch_inline(name, fn, args)


# --- celery -------------------------------------------------------------------------------------------------------


def _make_celery_app() -> Celery:
    settings = get_settings()
    application = Celery("mvf", broker=settings.REDIS_URL)
    application.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_ignore_result=True,
        task_serializer="json",
        accept_content=["json"],
        task_default_queue="mvf",
        broker_connection_retry_on_startup=True,
        # Renders and paid generations can take a while; don't let Redis re-deliver early.
        broker_transport_options={"visibility_timeout": 6 * 3600},
        task_publish_retry_policy={"max_retries": 3, "interval_start": 0, "interval_step": 0.5, "interval_max": 2},
        worker_hijack_root_logger=False,
        imports=("app.pipeline",),
        enable_utc=True,
    )
    return application


celery_app = _make_celery_app()


def _run_registered(name: str, *args: Any) -> Any:
    fn = _registry.get(name)
    if fn is None:
        import app.pipeline  # noqa: F401  (registers the pipeline task bodies)

        fn = _registry.get(name)
    if fn is None:
        raise RuntimeError(f"no task registered under {name!r}")
    return fn(*args)


@celery_app.task(name=CELERY_TASK_NAMES["analyze"])
def celery_analyze(project_id: str) -> None:
    _run_registered("analyze", project_id)


@celery_app.task(name=CELERY_TASK_NAMES["run_batch"])
def celery_run_batch(batch_id: str) -> None:
    _run_registered("run_batch", batch_id)


@celery_app.task(name=CELERY_TASK_NAMES["render_video"])
def celery_render_video(video_id: str) -> None:
    _run_registered("render_video", video_id)


@worker_process_init.connect
def _reset_db_pool_after_fork(**_kwargs: Any) -> None:
    # Pooled connections must not be shared between forked worker processes.
    from app.db import get_engine

    get_engine().dispose(close=False)


def _dispatch_celery(name: str, fn: TaskFn, args: tuple[Any, ...]) -> Any:
    task_name = CELERY_TASK_NAMES.get(name)
    if task_name is None:
        return _dispatch_celery_fallback(name, fn, args)
    try:
        return celery_app.tasks[task_name].apply_async(args=list(args))
    except Exception as exc:
        logger.error("could not publish %s%r to the Celery broker: %s", task_name, args, exc)
        raise QueueUnavailable(
            "ジョブキュー（Redis）に接続できないため、処理を開始できませんでした。しばらくしてから再試行してください。"
        ) from exc


DISPATCHERS: dict[str, Dispatcher] = {
    "inline": _dispatch_inline,
    "celery": _dispatch_celery,
}


def enqueue(name: str, *args: Any) -> Any:
    """Schedule task ``name`` with ``args``.

    Returns a ``concurrent.futures.Future`` in inline mode, a Celery
    ``AsyncResult`` in celery mode, or ``None`` when no task of that name is
    registered. Raises :class:`QueueUnavailable` (503) when the broker is down.
    """
    fn = _registry.get(name)
    if fn is None:
        logger.warning("enqueue(%r): no task registered under this name; ignored", name)
        return None
    mode = get_settings().TASK_MODE
    dispatcher = DISPATCHERS.get(mode, _dispatch_inline)
    return dispatcher(name, fn, args)


def shutdown(wait: bool = False) -> None:
    """Stop the inline executor (called on app shutdown)."""
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=not wait)
