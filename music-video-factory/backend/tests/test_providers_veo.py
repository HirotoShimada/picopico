"""Veo (Gemini API) adapter against respx-mocked HTTP (no real API calls)."""

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
from app.services.providers.base import ClipRequest, ProviderCancelled, ProviderTaskError
from app.services.providers.veo import GEMINI_API_BASE, VeoProvider

KEY = "test-google-key"
MODEL = "veo-3.1-lite-generate-preview"
OP = f"models/{MODEL}/operations/op-123"
SUBMIT_URL = f"{GEMINI_API_BASE}/models/{MODEL}:predictLongRunning"
POLL_URL = f"{GEMINI_API_BASE}/{OP}"
FILE_URL = f"{GEMINI_API_BASE}/files/file-1:download?alt=media"
STORAGE_URL = "https://storage.googleapis.example/veo/file-1.mp4"


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("veo") / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=360x640:r=24:d=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=60,
    )  # fmt: skip
    return path.read_bytes()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
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
def provider(set_env: Callable[..., Any], clock: FakeClock) -> VeoProvider:
    set_env(GOOGLE_API_KEY=KEY)
    return VeoProvider(sleep=clock.sleep, clock=clock)


def _req(**overrides: Any) -> ClipRequest:
    values: dict[str, Any] = {"prompt": "a fictional woman on a rooftop at dusk, 9:16 vertical",
                              "negative_prompt": "text, watermark", "duration": 5.0, "model": MODEL, "seed": 42}
    values.update(overrides)
    return ClipRequest(**values)


def _done(uri: str = FILE_URL) -> httpx.Response:
    return httpx.Response(200, json={"name": OP, "done": True, "response": {
        "@type": "type.googleapis.com/google.ai.generativelanguage.v1beta.PredictLongRunningResponse",
        "generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}}})  # fmt: skip


@respx.mock
def test_happy_path_submit_poll_download(provider: VeoProvider, clock: FakeClock, mp4_bytes: bytes, tmp_path: Path) -> None:
    submit = respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    poll = respx.get(POLL_URL).mock(side_effect=[
        httpx.Response(200, json={"name": OP}),
        httpx.Response(200, json={"name": OP, "done": False}),
        _done(),
    ])  # fmt: skip
    file_route = respx.get(FILE_URL).mock(return_value=httpx.Response(302, headers={"location": STORAGE_URL}))
    storage = respx.get(STORAGE_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    stages: list[tuple[float, str]] = []

    result = provider.generate(_req(), str(tmp_path / "clip.mp4"), on_progress=lambda f, s: stages.append((f, s)))

    request = submit.calls.last.request
    assert request.headers["x-goog-api-key"] == KEY
    body = json.loads(request.content)
    assert body == {
        "instances": [{"prompt": _req().prompt}],
        "parameters": {"aspectRatio": "9:16", "resolution": "720p", "durationSeconds": 6,
                       "negativePrompt": "text, watermark"},
    }  # fmt: skip
    assert poll.call_count == 3 and all(c.request.headers["x-goog-api-key"] == KEY for c in poll.calls)
    assert file_route.calls.last.request.headers["x-goog-api-key"] == KEY
    assert "x-goog-api-key" not in storage.calls.last.request.headers  # key never leaves the API host
    assert Path(result.path).read_bytes() == mp4_bytes
    assert (result.width, result.height) == (360, 640)
    assert result.duration == pytest.approx(1.0, abs=0.1)
    assert result.cost_usd == pytest.approx(6 * 0.05)  # billed for the requested 6 s
    assert result.external_job_id == OP and result.model == MODEL
    assert result.raw["requested_duration"] == 6
    assert len(clock.sleeps) == 2 and all(2.0 <= s <= 20.0 for s in clock.sleeps)
    assert stages[0][1] == "submitted" and stages[-1] == (1.0, "done")


@respx.mock
def test_failed_download_of_a_finished_operation_is_retried_not_resubmitted(
    provider: VeoProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    submit = respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    poll = respx.get(POLL_URL).mock(return_value=_done())
    file_route = respx.get(FILE_URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, content=mp4_bytes)])
    result = provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert submit.call_count == 1 and poll.call_count == 2 and file_route.call_count == 2
    assert all(c.request.headers["x-goog-api-key"] == KEY for c in file_route.calls)
    assert result.cost_usd == pytest.approx(6 * 0.05) and result.external_job_id == OP


@respx.mock
def test_1080p_variant_and_image_to_video(provider: VeoProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    submit = respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    respx.get(POLL_URL).mock(return_value=_done(STORAGE_URL))
    respx.get(STORAGE_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    image = tmp_path / "first.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    result = provider.generate(_req(model=f"{MODEL}@1080p", image_path=str(image)), str(tmp_path / "c.mp4"))
    body = json.loads(submit.calls.last.request.content)
    assert body["parameters"]["resolution"] == "1080p" and body["parameters"]["durationSeconds"] == 8
    assert body["instances"][0]["image"]["mimeType"] == "image/png"
    assert "seed" not in json.dumps(body) and "generateAudio" not in json.dumps(body)
    assert result.cost_usd == pytest.approx(8 * 0.08) and result.model == f"{MODEL}@1080p"


@respx.mock
@pytest.mark.parametrize(("rpc_code", "retryable"), [(13, True), (14, True), (3, False), (7, False)])
def test_operation_error_classification(provider: VeoProvider, tmp_path: Path, rpc_code: int, retryable: bool) -> None:
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    respx.get(POLL_URL).mock(return_value=httpx.Response(200, json={
        "name": OP, "done": True, "error": {"code": rpc_code, "message": "boom", "status": "X"}}))  # fmt: skip
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is retryable and info.value.external_job_id == OP
    assert not (tmp_path / "c.mp4").exists()


@respx.mock
def test_safety_filtered_result_is_not_retryable(provider: VeoProvider, tmp_path: Path) -> None:
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    respx.get(POLL_URL).mock(return_value=httpx.Response(200, json={"name": OP, "done": True, "response": {
        "generateVideoResponse": {"raiMediaFilteredCount": 1, "raiMediaFilteredReasons": ["celebrity"]}}}))  # fmt: skip
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is False and info.value.code == "provider_content_filtered"
    assert "celebrity" in str(info.value)


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (503, True), (400, False), (403, False)])
def test_submit_http_errors(provider: VeoProvider, tmp_path: Path, status: int, retryable: bool) -> None:
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(status, json={
        "error": {"code": status, "message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}}))  # fmt: skip
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert not isinstance(info.value, ProviderTaskError)  # nothing was submitted
    assert info.value.retryable is retryable
    assert f"HTTP {status}" in str(info.value) and KEY not in str(info.value)


@respx.mock
def test_invalid_key_is_reported_as_auth_failure(provider: VeoProvider, tmp_path: Path) -> None:
    # Body as returned by the live endpoint for an invalid key (captured 2026-10-11).
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(400, json={"error": {
        "code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID",
                     "domain": "googleapis.com"}]}}))  # fmt: skip
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is False and info.value.code == "provider_auth_failed"
    assert "GOOGLE_API_KEY" in str(info.value) and KEY not in str(info.value)


@respx.mock
def test_network_timeout_on_submit_is_retryable(provider: VeoProvider, tmp_path: Path) -> None:
    respx.post(SUBMIT_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is True and info.value.code == "provider_timeout"


@respx.mock
def test_transient_poll_errors_are_tolerated(provider: VeoProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    respx.get(POLL_URL).mock(side_effect=[httpx.Response(503), httpx.ConnectError("reset"), _done(STORAGE_URL)])
    respx.get(STORAGE_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    assert provider.generate(_req(), str(tmp_path / "c.mp4")).external_job_id == OP


@respx.mock
def test_persistent_poll_errors_give_up(provider: VeoProvider, tmp_path: Path) -> None:
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    respx.get(POLL_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is True and info.value.external_job_id == OP


@respx.mock
def test_timeout_while_polling(provider: VeoProvider, clock: FakeClock, tmp_path: Path) -> None:
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    poll = respx.get(POLL_URL).mock(return_value=httpx.Response(200, json={"name": OP, "done": False}))
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "c.mp4"), timeout=60)
    assert info.value.retryable is True and info.value.code == "provider_timeout"
    assert info.value.external_job_id == OP
    assert clock.now - 1000.0 == pytest.approx(60.0)
    assert poll.call_count >= 3


@respx.mock
def test_cancel_check(provider: VeoProvider, tmp_path: Path) -> None:
    submit = respx.post(SUBMIT_URL).mock(return_value=httpx.Response(200, json={"name": OP}))
    respx.get(POLL_URL).mock(return_value=httpx.Response(200, json={"name": OP, "done": False}))
    with pytest.raises(ProviderCancelled):
        provider.generate(_req(), str(tmp_path / "a.mp4"), cancel_check=lambda: True)
    assert submit.call_count == 0

    checks = iter([False, False, False, True])
    with pytest.raises(ProviderCancelled) as info:
        provider.generate(_req(), str(tmp_path / "b.mp4"), cancel_check=lambda: next(checks))
    assert info.value.external_job_id == OP and info.value.retryable is False


def test_not_configured_without_key(set_env: Callable[..., Any], tmp_path: Path) -> None:
    set_env(GOOGLE_API_KEY="")
    provider = VeoProvider()
    configured, reason = provider.is_configured()
    assert configured is False and "GOOGLE_API_KEY" in (reason or "")
    assert provider.check_model(MODEL)[0] is False
    with respx.mock(assert_all_mocked=True, assert_all_called=False), pytest.raises(ProviderNotConfigured):
        provider.generate(_req(), str(tmp_path / "c.mp4"))


def test_models_and_durations() -> None:
    provider = VeoProvider()
    assert provider.default_model() == MODEL
    assert all(not m.verified and m.supports_vertical and m.source_url for m in provider.models())
    assert provider.clip_duration_for(MODEL, 5) == 6
    assert provider.clip_duration_for(MODEL, 7.5) == 8
    assert provider.clip_duration_for(f"{MODEL}@1080p", 4) == 8
    assert provider.get_model(MODEL).price_per_second_usd == 0.05
