"""Kling adapter (new API-key API and legacy AK/SK JWT API) against respx-mocked HTTP."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.services.errors import ProviderError, ProviderNotConfigured
from app.services.providers.base import ClipRequest, ProviderCancelled, ProviderTaskError
from app.services.providers.kling import KlingProvider, kling_code_retryable, make_jwt

BASE = "https://api-singapore.klingai.com"
API_KEY = "test-kling-api-key"
AK, SK = "test-access-key", "test-secret-key"
OUTPUT_URL = "https://v15-kling.klingai.example/video/out.mp4"


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("kling") / "v.mp4"
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
def new_api(set_env: Callable[..., Any], clock: FakeClock) -> KlingProvider:
    set_env(KLING_API_KEY=API_KEY, KLING_ACCESS_KEY="", KLING_SECRET_KEY="", KLING_API_BASE=BASE)
    return KlingProvider(sleep=clock.sleep, clock=clock)


@pytest.fixture
def legacy_api(set_env: Callable[..., Any], clock: FakeClock) -> KlingProvider:
    set_env(KLING_API_KEY="", KLING_ACCESS_KEY=AK, KLING_SECRET_KEY=SK, KLING_API_BASE=BASE)
    return KlingProvider(sleep=clock.sleep, clock=clock)


def _req(**overrides: Any) -> ClipRequest:
    values: dict[str, Any] = {"prompt": "a fictional dancer spinning in a field", "negative_prompt": "text, logo",
                              "duration": 5.0, "model": "kling-3.0", "seed": 3}
    values.update(overrides)
    return ClipRequest(**values)


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _tasks(status: str, external_id: str, **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "message": "SUCCEED", "request_id": "r", "data": [
        {"id": "task-9", "status": status, "external_id": external_id, "create_time": 1, "update_time": 2, **extra}]})  # fmt: skip


def test_jwt_claims_and_signature() -> None:
    token = make_jwt(AK, SK, now=1_800_000_000)
    header_b64, payload_b64, signature_b64 = token.split(".")
    assert json.loads(_b64url_decode(header_b64)) == {"alg": "HS256", "typ": "JWT"}
    assert json.loads(_b64url_decode(payload_b64)) == {"iss": AK, "exp": 1_800_001_800, "nbf": 1_799_999_995}
    expected = hmac.new(SK.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    assert _b64url_decode(signature_b64) == expected
    assert "=" not in token


@respx.mock
def test_new_api_happy_path(new_api: KlingProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    create = respx.post(f"{BASE}/text-to-video/kling-3.0").mock(side_effect=lambda request: httpx.Response(200, json={
        "code": 0, "message": "SUCCEED", "request_id": "r", "data": {
            "id": "task-9", "status": "submitted", "external_id": json.loads(request.content)["options"]["external_task_id"]}}))  # fmt: skip
    states = iter(["submitted", "processing", "succeeded"])

    def poll_handler(request: httpx.Request) -> httpx.Response:
        external = parse_qs(urlsplit(str(request.url)).query)["external_task_ids"][0]
        status = next(states)
        extra = {"outputs": [{"type": "video", "id": "v1", "url": OUTPUT_URL, "duration": "5.0"}]} if status == "succeeded" else {}
        return _tasks(status, external, **extra)

    poll = respx.get(f"{BASE}/tasks").mock(side_effect=poll_handler)
    download = respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))

    result = new_api.generate(_req(duration=4.4), str(tmp_path / "clip.mp4"))

    request = create.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["settings"] == {"resolution": "720p", "aspect_ratio": "9:16", "duration": 5, "audio": "off",
                                "multi_shot": False}  # fmt: skip
    assert body["prompt"] == "a fictional dancer spinning in a field Avoid: text, logo."
    assert body["options"]["external_task_id"].startswith("mvf-")
    assert body["options"]["watermark_info"] == {"enabled": False}
    assert "negative_prompt" not in body and "contents" not in body
    assert poll.call_count == 3 and poll.calls.last.request.headers["authorization"] == f"Bearer {API_KEY}"
    assert "authorization" not in download.calls.last.request.headers
    assert result.cost_usd == pytest.approx(5 * 0.112)
    assert result.external_job_id == "task-9" and (result.width, result.height) == (720, 1280)


@respx.mock
def test_new_api_image_to_video(new_api: KlingProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    image = tmp_path / "first.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    create = respx.post(f"{BASE}/image-to-video/kling-3.0").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"id": "task-9", "status": "submitted"}})
    )
    respx.get(f"{BASE}/tasks").mock(side_effect=lambda request: _tasks(
        "succeeded", parse_qs(urlsplit(str(request.url)).query)["external_task_ids"][0],
        outputs=[{"type": "video", "url": OUTPUT_URL}]))  # fmt: skip
    respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    new_api.generate(_req(image_path=str(image)), str(tmp_path / "c.mp4"))
    contents = json.loads(create.calls.last.request.content)["contents"]
    assert contents == [{"type": "first_frame", "url": base64.b64encode(image.read_bytes()).decode()}]


@respx.mock
def test_legacy_api_happy_path_with_jwt(legacy_api: KlingProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    assert legacy_api.default_model() == "kling-v2-5-turbo"
    create = respx.post(f"{BASE}/v1/videos/text2video").mock(return_value=httpx.Response(200, json={
        "code": 0, "message": "SUCCEED", "request_id": "r", "data": {"task_id": "legacy-1", "task_status": "submitted"}}))  # fmt: skip
    poll = respx.get(f"{BASE}/v1/videos/text2video/legacy-1").mock(side_effect=[
        httpx.Response(200, json={"code": 0, "data": {"task_id": "legacy-1", "task_status": "processing"}}),
        httpx.Response(200, json={"code": 0, "data": {"task_id": "legacy-1", "task_status": "succeed",
                                                      "task_result": {"videos": [{"id": "v", "url": OUTPUT_URL, "duration": "5"}]}}}),
    ])  # fmt: skip
    respx.get(OUTPUT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))

    result = legacy_api.generate(_req(model=None), str(tmp_path / "c.mp4"))  # type: ignore[arg-type]

    request = create.calls.last.request
    token = request.headers["authorization"].removeprefix("Bearer ")
    header_b64, payload_b64, sig_b64 = token.split(".")
    assert json.loads(_b64url_decode(payload_b64))["iss"] == AK
    assert _b64url_decode(sig_b64) == hmac.new(SK.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    body = json.loads(request.content)
    assert body["model_name"] == "kling-v2-5-turbo" and body["mode"] == "std"
    assert body["aspect_ratio"] == "9:16" and body["duration"] == "5" and body["negative_prompt"] == "text, logo"
    assert poll.call_count == 2 and poll.calls.last.request.headers["authorization"].startswith("Bearer ")
    assert result.model == "kling-v2-5-turbo" and result.cost_usd == pytest.approx(5 * 0.042)
    assert result.external_job_id == "legacy-1"


@respx.mock
@pytest.mark.parametrize(
    ("http_status", "code", "retryable"),
    [(429, 1302, True), (429, 1303, True), (400, 1102, False), (429, 1102, False), (400, 1201, False),
     (500, 5000, True), (401, 1004, False)],
)  # fmt: skip
def test_business_codes_on_create(new_api: KlingProvider, tmp_path: Path, http_status: int, code: int, retryable: bool) -> None:
    respx.post(f"{BASE}/text-to-video/kling-3.0").mock(
        return_value=httpx.Response(http_status, json={"code": code, "message": "nope", "request_id": "r"})
    )
    with pytest.raises(ProviderError) as info:
        new_api.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is retryable and f"code {code}" in str(info.value)
    assert API_KEY not in str(info.value)


@respx.mock
def test_business_error_in_http_200(new_api: KlingProvider, tmp_path: Path) -> None:
    respx.post(f"{BASE}/text-to-video/kling-3.0").mock(
        return_value=httpx.Response(200, json={"code": 1303, "message": "parallel task over resource pack limit"})
    )
    with pytest.raises(ProviderError) as info:
        new_api.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is True and info.value.code == "provider_rate_limited"


def test_code_classification_table() -> None:
    assert kling_code_retryable(1302) and kling_code_retryable(5001)
    assert kling_code_retryable(1102) is False and kling_code_retryable(1301) is False
    assert kling_code_retryable(42) is None


@respx.mock
@pytest.mark.parametrize(("message", "retryable"), [("Failure to pass the risk control system", False),
                                                    ("internal error, please try again", True)])  # fmt: skip
def test_failed_task(new_api: KlingProvider, tmp_path: Path, message: str, retryable: bool) -> None:
    respx.post(f"{BASE}/text-to-video/kling-3.0").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"id": "task-9", "status": "submitted"}})
    )
    respx.get(f"{BASE}/tasks").mock(side_effect=lambda request: _tasks(
        "failed", parse_qs(urlsplit(str(request.url)).query)["external_task_ids"][0], message=message))  # fmt: skip
    with pytest.raises(ProviderTaskError) as info:
        new_api.generate(_req(), str(tmp_path / "c.mp4"))
    assert info.value.retryable is retryable and message in str(info.value)
    assert info.value.external_job_id == "task-9"


@respx.mock
def test_timeout_and_cancel(new_api: KlingProvider, clock: FakeClock, tmp_path: Path) -> None:
    respx.post(f"{BASE}/text-to-video/kling-3.0").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"id": "task-9", "status": "submitted"}})
    )
    respx.get(f"{BASE}/tasks").mock(side_effect=lambda request: _tasks(
        "processing", parse_qs(urlsplit(str(request.url)).query)["external_task_ids"][0]))  # fmt: skip
    with pytest.raises(ProviderTaskError) as info:
        new_api.generate(_req(), str(tmp_path / "a.mp4"), timeout=90)
    assert info.value.code == "provider_timeout" and info.value.retryable is True
    assert clock.now == pytest.approx(90)

    checks = iter([False, False, True])
    with pytest.raises(ProviderCancelled):
        new_api.generate(_req(), str(tmp_path / "b.mp4"), cancel_check=lambda: next(checks))


def test_configuration_states(set_env: Callable[..., Any], tmp_path: Path) -> None:
    set_env(KLING_API_KEY="", KLING_ACCESS_KEY="", KLING_SECRET_KEY="")
    provider = KlingProvider()
    assert provider.is_configured()[0] is False and "KLING_API_KEY" in (provider.is_configured()[1] or "")
    with pytest.raises(ProviderNotConfigured):
        provider.generate(_req(), str(tmp_path / "c.mp4"))

    set_env(KLING_ACCESS_KEY=AK)  # half a legacy pair is not enough
    assert KlingProvider().is_configured()[0] is False

    set_env(KLING_SECRET_KEY=SK)  # legacy pair only: legacy model usable, new model not
    provider = KlingProvider()
    configured, reason = provider.is_configured()
    assert configured is True and "kling-v2-5-turbo" in (reason or "")
    assert provider.check_model("kling-v2-5-turbo") == (True, None)
    ok, why = provider.check_model("kling-3.0")
    assert ok is False and "KLING_API_KEY" in (why or "")
    with pytest.raises(ProviderNotConfigured) as missing:
        provider.generate(_req(model="kling-3.0"), str(tmp_path / "c.mp4"))
    assert "KLING_API_KEY" in str(missing.value)

    set_env(KLING_API_KEY=API_KEY, KLING_ACCESS_KEY="", KLING_SECRET_KEY="")
    provider = KlingProvider()
    assert provider.is_configured() == (True, None) and provider.default_model() == "kling-3.0"
    assert provider.check_model("kling-v2-5-turbo")[0] is False


def test_models_and_durations() -> None:
    provider = KlingProvider()
    assert provider.clip_duration_for("kling-3.0", 4.2) == 5 and provider.clip_duration_for("kling-3.0", 20) == 15
    assert provider.clip_duration_for("kling-v2-5-turbo", 6) == 10
    assert {m.id: m.price_per_second_usd for m in provider.models()} == {"kling-3.0": 0.112, "kling-v2-5-turbo": 0.042}
    assert all(not m.verified for m in provider.models())
