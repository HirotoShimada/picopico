"""Runway adapter against respx-mocked HTTP (api.dev.runwayml.com is unreachable here)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.services.errors import ProviderError, ProviderNotConfigured
from app.services.providers.base import (
    OUTPUT_FETCH_ATTEMPTS,
    ClipRequest,
    PollConfig,
    ProviderCancelled,
    ProviderTaskError,
)
from app.services.providers.runway import (
    RUNWAY_API_VERSION,
    RunwayProvider,
    classify_failure_code,
)

KEY = "test-runway-key"
BASE = "https://api.dev.runwayml.com"
TASK = "0b9b7a5e-1c0f-4c55-9d7c-5f1a2b3c4d5e"
OUTPUT_URL = "https://dnznrvs05pmza.cloudfront.example/out/video.mp4?_jwt=x"
FRESH_OUTPUT_URL = "https://dnznrvs05pmza.cloudfront.example/out/video-fresh.mp4?_jwt=y"


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("runway") / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=720x1280:r=24:d=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=60,
    )  # fmt: skip
    return path.read_bytes()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider(set_env: Callable[..., Any], clock: FakeClock) -> RunwayProvider:
    set_env(RUNWAY_API_KEY=KEY, RUNWAY_API_BASE=BASE)
    return RunwayProvider(sleep=clock.sleep, clock=clock)


def _req(**overrides: Any) -> ClipRequest:
    values: dict[str, Any] = {"prompt": "a fictional man walking through neon rain", "negative_prompt": "text",
                              "duration": 5.0, "model": "gen4.5", "seed": 7}
    values.update(overrides)
    return ClipRequest(**values)


def _task(status: str, **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"id": TASK, "createdAt": "2026-10-11T00:00:00Z", "status": status, **extra})


@respx.mock
def test_happy_path(provider: RunwayProvider, clock: FakeClock, mp4_bytes: bytes, tmp_path: Path) -> None:
    create = respx.post(f"{BASE}/v1/text_to_video").mock(
        return_value=httpx.Response(200, json={"id": TASK, "estimatedCost": {"credits": 60}})
    )
    poll = respx.get(f"{BASE}/v1/tasks/{TASK}").mock(side_effect=[
        _task("PENDING", estimatedCost={"credits": 60}),
        _task("THROTTLED", estimatedCost={"credits": 60}),
        _task("RUNNING", progress=0.5, estimatedCost={"credits": 60}),
        _task("SUCCEEDED", output=[OUTPUT_URL], cost={"credits": 58}),
    ])  # fmt: skip
    download = respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    progress: list[tuple[float, str]] = []

    result = provider.generate(_req(), str(tmp_path / "clip.mp4"), on_progress=lambda f, s: progress.append((f, s)))

    request = create.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["x-runway-version"] == RUNWAY_API_VERSION
    assert json.loads(request.content) == {
        "model": "gen4.5", "promptText": _req().prompt, "ratio": "720:1280", "duration": 5, "seed": 7,
        "contentModeration": {"publicFigureThreshold": "auto"},
    }  # fmt: skip
    assert poll.call_count == 4
    assert all(c.request.headers["x-runway-version"] == RUNWAY_API_VERSION for c in poll.calls)
    assert "authorization" not in download.calls.last.request.headers  # output URLs are pre-signed
    assert result.cost_usd == pytest.approx(0.58)  # actual task cost (credits x $0.01), not the table price
    assert (result.width, result.height, result.external_job_id) == (720, 1280, TASK)
    assert all(s >= 5.0 for s in clock.sleeps)  # Runway: never poll faster than every 5 s
    assert (0.05 + 0.85 * 0.5, "running") in progress and progress[-1] == (1.0, "done")


@respx.mock
def test_veo_via_runway_sends_audio_false_and_negative(provider: RunwayProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    create = respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(return_value=_task("SUCCEEDED", output=[OUTPUT_URL]))
    respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.generate(_req(model="veo3.1_fast", prompt="あ" * 1200), str(tmp_path / "c.mp4"))
    body = json.loads(create.calls.last.request.content)
    assert body["model"] == "veo3.1_fast" and body["duration"] == 6 and body["audio"] is False
    assert body["negativePrompt"] == "text" and "contentModeration" not in body
    assert len(body["promptText"]) == 1000  # 1000 UTF-16 units max
    assert result.cost_usd == pytest.approx(6 * 0.10)  # no cost in the task -> price table


def test_image_only_model_is_not_faked_as_text_to_video(provider: RunwayProvider, tmp_path: Path) -> None:
    ok, reason = provider.check_model("gen4_turbo")
    assert ok is False and "画像" in (reason or "")
    with respx.mock(assert_all_called=False) as router:
        create = router.post(f"{BASE}/v1/text_to_video")
        with pytest.raises(ProviderError) as info:
            provider.generate(_req(model="gen4_turbo"), str(tmp_path / "c.mp4"))
        assert info.value.retryable is False and create.call_count == 0
    model = provider.get_model("gen4_turbo")
    assert model.text_to_video is False and model.image_to_video is True
    assert provider.check_model("gen4_turbo", has_image=True) == (True, None)


@respx.mock
def test_image_to_video_uses_data_uri(provider: RunwayProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    create = respx.post(f"{BASE}/v1/image_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(return_value=_task("SUCCEEDED", output=[OUTPUT_URL], cost={"credits": 25}))
    respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.generate(_req(model="gen4_turbo", image_path=str(image)), str(tmp_path / "c.mp4"))
    body = json.loads(create.calls.last.request.content)
    assert body["promptImage"].startswith("data:image/jpeg;base64,") and body["model"] == "gen4_turbo"
    assert result.cost_usd == pytest.approx(0.25)


@respx.mock
@pytest.mark.parametrize(
    ("failure_code", "retryable", "error_code"),
    [("SAFETY.INPUT.TEXT", False, "provider_content_filtered"), ("ASSET.INVALID", False, "provider_failed"),
     ("INTERNAL.BAD_OUTPUT.CODE01", True, "provider_failed"), (None, True, "provider_failed")],
)  # fmt: skip
def test_failed_task_classification(
    provider: RunwayProvider, tmp_path: Path, failure_code: str | None, retryable: bool, error_code: str
) -> None:
    respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(
        return_value=_task("FAILED", failure="Generation failed", failureCode=failure_code, cost={"credits": 0})
    )
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is retryable and info.value.code == error_code
    assert info.value.failure_code == failure_code and info.value.external_job_id == TASK


def test_classify_failure_code() -> None:
    assert classify_failure_code("INTERNAL") and classify_failure_code(None) and classify_failure_code("")
    assert not classify_failure_code("SAFETY.OUTPUT.VIDEO") and not classify_failure_code("UNKNOWN.THING")


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (502, True), (400, False), (401, False)])
def test_create_http_errors(provider: RunwayProvider, tmp_path: Path, status: int, retryable: bool) -> None:
    respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(status, json={"error": "Daily limit reached"}))
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is retryable and "Daily limit reached" in str(info.value)
    assert KEY not in str(info.value)


@respx.mock
def test_timeout_cancels_remote_task(provider: RunwayProvider, clock: FakeClock, tmp_path: Path) -> None:
    respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(return_value=_task("RUNNING", progress=0.1))
    delete = respx.delete(f"{BASE}/v1/tasks/{TASK}").mock(return_value=httpx.Response(204))
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"), timeout=120)
    assert info.value.code == "provider_timeout" and info.value.retryable is True
    assert delete.call_count == 1 and clock.now == pytest.approx(120)


@respx.mock
def test_cancel_check_cancels_remote_task(provider: RunwayProvider, tmp_path: Path) -> None:
    respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(return_value=_task("PENDING"))
    delete = respx.delete(f"{BASE}/v1/tasks/{TASK}").mock(return_value=httpx.Response(500))  # failure is tolerated
    checks = iter([False, False, True])
    with pytest.raises(ProviderCancelled) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"), cancel_check=lambda: next(checks))
    assert info.value.external_job_id == TASK and delete.call_count == 1


@respx.mock
def test_failed_download_rereads_the_task_instead_of_resubmitting(
    provider: RunwayProvider, clock: FakeClock, mp4_bytes: bytes, tmp_path: Path
) -> None:
    create = respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    poll = respx.get(f"{BASE}/v1/tasks/{TASK}").mock(side_effect=[
        _task("SUCCEEDED", output=[OUTPUT_URL], cost={"credits": 60}),
        _task("SUCCEEDED", output=[FRESH_OUTPUT_URL], cost={"credits": 60}),  # fresh pre-signed URL
    ])  # fmt: skip
    stale = respx.get(OUTPUT_URL).mock(return_value=httpx.Response(503))
    fresh = respx.get(FRESH_OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert create.call_count == 1 and poll.call_count == 2
    assert stale.call_count == 1 and fresh.call_count == 1
    assert result.cost_usd == pytest.approx(0.6) and result.external_job_id == TASK
    assert all(s >= 5.0 for s in clock.sleeps)  # the re-read respects Runway's 5 s polling floor


def test_succeeded_task_whose_output_cannot_be_downloaded_is_billed_not_resubmitted(
    provider: RunwayProvider, tmp_path: Path
) -> None:
    with respx.mock(assert_all_called=False) as router:
        create = router.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
        router.get(f"{BASE}/v1/tasks/{TASK}").mock(
            return_value=_task("SUCCEEDED", output=[OUTPUT_URL], cost={"credits": 60})
        )
        cdn = router.get(OUTPUT_URL).mock(return_value=httpx.Response(503))
        delete = router.delete(f"{BASE}/v1/tasks/{TASK}").mock(return_value=httpx.Response(204))
        with pytest.raises(ProviderTaskError) as info:
            provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is False and info.value.cost_usd == pytest.approx(0.6)
    assert info.value.external_job_id == TASK
    assert create.call_count == 1 and cdn.call_count == OUTPUT_FETCH_ATTEMPTS
    assert delete.call_count == 0  # a succeeded task is neither cancelled nor deleted


def test_pipeline_books_a_succeeded_task_whose_output_cannot_be_downloaded(
    db: Any, ready_project: Any, sync_tasks: list, fake_render: Any, set_env: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the paid task is submitted once, its cost lands in a CostRecord and ``batch.cost_usd``."""
    from sqlalchemy import select

    from app import pipeline
    from app.models import Batch, Clip, CostRecord
    from app.schemas import GenerationSettings

    set_env(RUNWAY_API_KEY=KEY, RUNWAY_API_BASE=BASE)
    monkeypatch.setattr(RunwayProvider, "poll_config", PollConfig(initial=0.0, maximum=0.0, factor=1.0, jitter=0.0,
                                                                  minimum=0.0))  # fmt: skip
    with respx.mock(assert_all_called=False) as router:
        create = router.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
        router.get(f"{BASE}/v1/tasks/{TASK}").mock(
            return_value=_task("SUCCEEDED", output=[OUTPUT_URL], cost={"credits": 60})
        )
        cdn = router.get(OUTPUT_URL).mock(return_value=httpx.Response(503))
        settings = GenerationSettings(provider="runway", model="gen4.5", count=1, duration_sec=5, seed=7,
                                      max_new_clips=1)  # fmt: skip
        batch_id = pipeline.create_batch(db, ready_project, settings).id
        pipeline.run_batch(batch_id)
    assert create.call_count == 1 and cdn.call_count == OUTPUT_FETCH_ATTEMPTS
    db.expire_all()
    (clip,) = db.scalars(select(Clip).where(Clip.project_id == ready_project.id, Clip.provider == "runway")).all()
    assert clip.status == "failed" and clip.attempts == 1 and clip.cost_usd == pytest.approx(0.6)
    assert clip.external_job_id == TASK and "課金済み" in (clip.error or "")
    assert db.get(Batch, batch_id).cost_usd == pytest.approx(0.6)
    paid = db.scalars(select(CostRecord).where(CostRecord.batch_id == batch_id, CostRecord.cached.is_(False))).all()
    assert [round(r.amount_usd, 4) for r in paid] == [0.6]


@respx.mock
def test_remotely_cancelled_task(provider: RunwayProvider, tmp_path: Path) -> None:
    respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(return_value=_task("CANCELLED", cost={"credits": 0}))
    with pytest.raises(ProviderCancelled):
        provider.generate(_req(), str(tmp_path / "c.mp4"))


def test_not_configured_without_key(set_env: Callable[..., Any], tmp_path: Path) -> None:
    set_env(RUNWAY_API_KEY="")
    provider = RunwayProvider()
    assert provider.is_configured()[0] is False and "RUNWAY_API_KEY" in (provider.is_configured()[1] or "")
    assert provider.info()["available"] is False
    with pytest.raises(ProviderNotConfigured):
        provider.generate(_req(), str(tmp_path / "c.mp4"))


def test_models_and_durations() -> None:
    provider = RunwayProvider()
    assert provider.default_model() == "gen4.5"
    assert provider.clip_duration_for("gen4.5", 4.2) == 5
    assert provider.clip_duration_for("gen4.5", 12) == 10
    assert provider.clip_duration_for("veo3.1_fast", 5) == 6
    prices = {m.id: m.price_per_second_usd for m in provider.models()}
    assert prices == {"gen4.5": 0.12, "gen4_turbo": 0.05, "veo3.1_fast": 0.1}
    assert all(m.verified is False for m in provider.models())


@respx.mock
def test_resume_reads_the_stored_task_instead_of_creating_one(provider: RunwayProvider, mp4_bytes: bytes,
                                                              tmp_path: Path) -> None:  # fmt: skip
    create = respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": "other"}))
    poll = respx.get(f"{BASE}/v1/tasks/{TASK}").mock(
        side_effect=[_task("RUNNING", progress=0.5), _task("SUCCEEDED", output=[OUTPUT_URL], cost={"credits": 60})]
    )
    respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.resume(TASK, _req(), str(tmp_path / "c.mp4"), timeout=300)
    assert create.call_count == 0 and poll.call_count == 2
    assert result.external_job_id == TASK and result.cost_usd == pytest.approx(0.6)


@respx.mock
def test_on_submitted_gets_the_task_id(provider: RunwayProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(return_value=_task("SUCCEEDED", output=[OUTPUT_URL]))
    respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    reported: list[tuple[str, str]] = []
    provider.generate(_req(), str(tmp_path / "c.mp4"), on_submitted=lambda job, token: reported.append((job, token)))
    assert reported == [(TASK, TASK)]


@respx.mock
@pytest.mark.parametrize(("status", "error"), [("FAILED", ProviderTaskError), ("CANCELLED", ProviderCancelled)])
def test_failed_or_cancelled_task_reports_its_charged_credits(
    provider: RunwayProvider, tmp_path: Path, status: str, error: type[Exception]
) -> None:
    respx.post(f"{BASE}/v1/text_to_video").mock(return_value=httpx.Response(200, json={"id": TASK}))
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(
        return_value=_task(status, failureCode="INTERNAL.BAD_OUTPUT.CODE01", cost={"credits": 0})
    )
    with pytest.raises(error) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.cost_usd == 0.0  # reported (0 credits) — known, not presumed
    respx.get(f"{BASE}/v1/tasks/{TASK}").mock(return_value=_task(status, failureCode="INTERNAL"))
    with pytest.raises(error) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.cost_usd is None  # not reported
