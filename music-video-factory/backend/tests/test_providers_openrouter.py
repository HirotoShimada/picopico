"""OpenRouter video adapter (docs/VIDEO_PROVIDERS.md §8) against respx-mocked HTTP only.

openrouter.ai is unreachable from the dev container, so these tests pin the
wire format taken from the official SDK (@openrouter/sdk 1.4.25) and the
adapter's own decisions; none of it has been verified against the real API.
"""

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
    ClipRequest,
    PollConfig,
    ProviderCancelled,
    ProviderTaskError,
)
from app.services.providers.openrouter import (
    CACHE_FILE_NAME,
    OpenRouterProvider,
    reset_model_cache,
    sku_price_per_second,
)

KEY = "or-test-key-not-real"
BASE = "https://openrouter.ai/api/v1"
MODELS_URL = f"{BASE}/videos/models"
SUBMIT_URL = f"{BASE}/videos"
JOB = "gen-vid-1791763200-abcdefghij0123456789"
POLL_URL = f"{BASE}/videos/{JOB}"
CONTENT_URL = f"{BASE}/videos/{JOB}/content"
MODEL = "google/veo-3.1-lite"
UNLISTED = "bytedance/seedance-2.0"  # not in the built-in list / static prices


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("openrouter") / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=360x640:r=24:d=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=60,
    )  # fmt: skip
    return path.read_bytes()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = 1_791_763_200.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def wall_clock(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.wall += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _make(clock: FakeClock, **kwargs: Any) -> OpenRouterProvider:
    return OpenRouterProvider(sleep=clock.sleep, clock=clock, wall_clock=clock.wall_clock, **kwargs)


@pytest.fixture
def provider(set_env: Callable[..., Any], clock: FakeClock) -> OpenRouterProvider:
    set_env(OPENROUTER_API_KEY=KEY)
    return _make(clock)


def _video_model(model_id: str = MODEL, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": model_id, "name": "Veo 3.1 Lite", "canonical_slug": model_id, "created": 1776902400,
        "supported_durations": [4, 6, 8], "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_resolutions": ["720p", "1080p"], "supported_sizes": ["720x1280", "1280x720"],
        "supported_frame_images": ["first_frame", "last_frame"], "generate_audio": True, "seed": True,
        "allowed_passthrough_parameters": [], "pricing_skus": {"per-video-second": "0.05"},
        "creativity": None, "upscale_factor": None,
    }  # fmt: skip
    entry.update(overrides)
    return entry


def _listing(*entries: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(entries or [_video_model()])})


def _req(**overrides: Any) -> ClipRequest:
    values: dict[str, Any] = {"prompt": "a fictional woman on a rooftop at dusk, vertical 9:16",
                              "negative_prompt": "text, watermark", "duration": 5.0, "model": MODEL, "seed": 42}
    values.update(overrides)
    return ClipRequest(**values)


def _job(status: str, **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"id": JOB, "polling_url": POLL_URL, "status": status, **extra})


def _submitted() -> httpx.Response:
    return httpx.Response(202, json={"id": JOB, "polling_url": POLL_URL, "status": "pending"})


def _completed(cost: float | None = 0.31, **extra: Any) -> httpx.Response:
    usage = {"cost": cost, "is_byok": False} if cost is not None else None
    body: dict[str, Any] = {"unsigned_urls": [f"{CONTENT_URL}?index=0"], "generation_id": "gen-1", **extra}
    if usage is not None:
        body["usage"] = usage
    return _job("completed", **body)


# --- pricing_skus --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("skus", "resolution", "expected"),
    [
        ({"per-video-second": "0.50", "per-video-second-1080p": "0.75"}, "720p", (0.5, "per-video-second", False)),
        ({"per-video-second": "0.50", "per-video-second-1080p": "0.75"}, "1080p", (0.75, "per-video-second-1080p", True)),
        ({"per-video-second-with-audio": "0.126", "per-video-second-no-audio": "0.084"}, "720p",
         (0.084, "per-video-second-no-audio", False)),
        ({"per-video-second-a": "0.10", "per-video-second-b": "0.12"}, "720p", (0.12, "per-video-second-b", False)),
        ({"per-video-second-480p": "0.06", "per-video-second-1080p": "0.2"}, "720p", (None, None, False)),
        ({"per-video-second": "free", "per-video": "0.5"}, "720p", (None, None, False)),
        ({"per-video-second": "-1"}, None, (None, None, False)),
        (None, "720p", (None, None, False)),
        ({"per-video-second": 0.07}, None, (0.07, "per-video-second", False)),
    ],
)  # fmt: skip
def test_sku_price_per_second(skus: Any, resolution: str | None, expected: tuple[Any, ...]) -> None:
    assert sku_price_per_second(skus, resolution) == expected


# --- model listing ---------------------------------------------------------------------------------------------------


@respx.mock
def test_listing_maps_models_and_leaves_out_unusable_entries(provider: OpenRouterProvider) -> None:
    route = respx.get(MODELS_URL).mock(return_value=_listing(
        _video_model(pricing_skus={"per-video-second": "0.40", "per-video-second-1080p": "0.60"}),
        _video_model("vendor/landscape-only", supported_aspect_ratios=["16:9"]),
        _video_model("vendor/upscaler", upscale_factor={"min": 2, "max": 4}, creativity=[1, 2]),
        {"id": "vendor/bare-minimum", "name": "Bare"},
        _video_model("kwaivgi/kling-v3.0-std", name="Kling v3.0 std", supported_durations=list(range(3, 16)),
                     supported_resolutions=["720p"], seed=None,
                     pricing_skus={"per-video-second-with-audio": "0.126", "per-video-second-no-audio": "0.084"}),
        _video_model("kwaivgi/kling-video-o1", supported_durations=[5, 10], supported_resolutions=["720p"],
                     pricing_skus={"per-video-second": "n/a"}, supported_frame_images=None),
        _video_model("vendor/low-res", supported_resolutions=["480p"], generate_audio=False),
        "not-a-model",
    ))  # fmt: skip
    models = {m.id: m for m in provider.models()}
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["x-openrouter-title"] == "AI Music Video Factory"
    assert set(models) == {MODEL, f"{MODEL}@1080p", "vendor/bare-minimum", "kwaivgi/kling-v3.0-std",
                           "kwaivgi/kling-video-o1", "vendor/low-res"}  # fmt: skip

    veo = models[MODEL]
    assert (veo.price_per_second_usd, veo.clip_durations, veo.default_duration) == (0.40, [4.0, 6.0, 8.0], 6.0)
    assert veo.resolution == "720x1280" and veo.supports_vertical and veo.image_to_video and veo.text_to_video
    assert veo.verified is False and veo.price_known and veo.params["resolution"] == "720p"
    assert "per-video-second" in veo.price_note and "未検証" in veo.price_note and "generate_audio" in veo.price_note
    assert veo.as_of == "2026-10-12" and veo.source_url == f"https://openrouter.ai/{MODEL}"
    hd = models[f"{MODEL}@1080p"]
    assert hd.price_per_second_usd == 0.60 and hd.resolution == "1080x1920" and hd.params["api_model"] == MODEL

    bare = models["vendor/bare-minimum"]
    assert bare.price_known is False and bare.supports_vertical is False and bare.clip_durations == [5.0]
    assert "料金を確認できません" in bare.price_note and "9:16" in bare.price_note and "5 秒" in bare.price_note
    info = next(m for m in provider.info()["models"] if m["id"] == "vendor/bare-minimum")
    assert info["price_per_second_usd"] is None and info["price_known"] is False
    assert provider.check_model("vendor/bare-minimum") == (False, provider.check_model("vendor/bare-minimum")[1])
    assert "料金を確認できない" in (provider.check_model("vendor/bare-minimum")[1] or "")

    kling = models["kwaivgi/kling-v3.0-std"]
    assert kling.price_per_second_usd == 0.084 and kling.default_duration == 5.0 and kling.max_clip_duration == 15
    o1 = models["kwaivgi/kling-video-o1"]  # unreadable SKU → the researched table price
    assert o1.price_per_second_usd == 0.112 and o1.params["price_source"] == "table" and not o1.image_to_video
    assert "読み取れなかった" in o1.price_note
    assert "画質が下がります" in models["vendor/low-res"].price_note

    info = provider.info()
    assert info["models_source"] == "live" and info["configured"] and "2 件は表示していません" in info["models_note"]
    assert info["default_model"] == MODEL and info["models"][0]["id"] == MODEL
    assert route.call_count == 1  # cached in memory


def test_listing_cache_ttl_memory_and_file(set_env: Callable[..., Any], clock: FakeClock) -> None:
    settings = set_env(OPENROUTER_API_KEY=KEY, OPENROUTER_MODELS_CACHE_TTL="600")
    cache_file = settings.WORK_DIR / "cache" / CACHE_FILE_NAME
    with respx.mock(assert_all_called=False) as router:
        route = router.get(MODELS_URL).mock(return_value=_listing())
        provider = _make(clock)
        assert provider.default_model() == MODEL and route.call_count == 1
        provider.models()
        _make(clock).models()  # new instances share the process cache
        assert route.call_count == 1
        stored = json.loads(cache_file.read_text(encoding="utf-8"))
        assert stored["api_base"] == BASE and stored["data"][0]["id"] == MODEL and KEY not in cache_file.read_text()

        reset_model_cache(delete_file=False)  # e.g. another process: the file cache is used
        clock.wall += 300
        assert _make(clock).catalogue().source == "cache" and route.call_count == 1

        clock.wall += 400  # past the TTL → refetch
        assert _make(clock).catalogue().source == "live" and route.call_count == 2


def test_listing_failure_uses_built_in_list_and_backs_off(set_env: Callable[..., Any], clock: FakeClock) -> None:
    set_env(OPENROUTER_API_KEY=KEY)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(MODELS_URL).mock(return_value=httpx.Response(500, json={"error": {"code": 500,
                                                                                            "message": "down"}}))
        provider = _make(clock)
        catalogue = provider.catalogue()
        assert catalogue.source == "fallback" and "HTTP 500" in (catalogue.error or "")
        assert {m.id for m in provider.models()} >= {MODEL, "kwaivgi/kling-v3.0-std"}
        assert "取得できない" in provider.info()["models_note"]
        provider.models()
        assert route.call_count == 1  # backing off, no request storm
        clock.wall += 301
        route.mock(return_value=_listing(_video_model(pricing_skus={"per-video-second": "0.06"})))
        assert provider.catalogue().source == "live" and provider.get_model(MODEL).price_per_second_usd == 0.06
        assert route.call_count == 2


def test_failed_refresh_keeps_the_stale_listing(set_env: Callable[..., Any], clock: FakeClock) -> None:
    set_env(OPENROUTER_API_KEY=KEY, OPENROUTER_MODELS_CACHE_TTL="600")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(MODELS_URL).mock(return_value=_listing(_video_model(pricing_skus={"per-video-second": "0.07"})))
        provider = _make(clock)
        assert provider.get_model(MODEL).price_per_second_usd == 0.07
        clock.wall += 700
        route.mock(side_effect=httpx.ConnectError("unreachable"))
        catalogue = provider.catalogue()
        assert catalogue.stale and catalogue.source in ("live", "cache") and "ConnectError" in (catalogue.error or "")
        assert provider.get_model(MODEL).price_per_second_usd == 0.07
        assert "最新ではない" in provider.info()["models_note"]


def test_without_key_the_listing_is_never_fetched(set_env: Callable[..., Any], clock: FakeClock) -> None:
    set_env(OPENROUTER_API_KEY="")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(MODELS_URL).mock(return_value=_listing())
        provider = _make(clock)
        info = provider.info()
        assert route.called is False
        assert info["configured"] is False and info["models_source"] == "fallback" and info["reason"]
        assert all(m["verified"] is False and m["price_known"] for m in info["models"])
        with pytest.raises(ProviderNotConfigured):
            provider.generate(_req(), "/tmp/never.mp4")


# --- generation ------------------------------------------------------------------------------------------------------


@respx.mock
def test_generate_happy_path(provider: OpenRouterProvider, clock: FakeClock, mp4_bytes: bytes, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    poll = respx.get(POLL_URL).mock(side_effect=[_job("pending"), _job("in_progress"), _completed(0.31)])
    content = respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    submitted: list[tuple[str, str]] = []
    stages: list[str] = []

    def on_submitted(job_id: str, token: str) -> None:
        assert poll.call_count == 0  # reported before any polling
        submitted.append((job_id, token))

    result = provider.generate(_req(), str(tmp_path / "clip.mp4"), on_submitted=on_submitted,
                               on_progress=lambda f, s: stages.append(s))  # fmt: skip

    body = json.loads(submit.calls.last.request.content)
    assert body == {
        "model": MODEL,
        "prompt": "a fictional woman on a rooftop at dusk, vertical 9:16 Avoid: text, watermark.",
        "duration": 6,
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "generate_audio": False,
        "seed": 42,
    }
    # The video API has no zdr / data_collection fields (§8.5): no provider preferences are sent here.
    assert "provider" not in body and "size" not in body and "callback_url" not in body
    for call in [submit.calls.last, *poll.calls, content.calls.last]:
        assert call.request.headers["authorization"] == f"Bearer {KEY}"
    assert content.calls.last.request.url.params["index"] == "0"
    assert content.calls.last.request.headers["accept"] == "video/mp4"
    assert submitted == [(JOB, JOB)]
    assert Path(result.path).read_bytes() == mp4_bytes and (result.width, result.height) == (360, 640)
    assert result.cost_usd == pytest.approx(0.31)  # OpenRouter's usage.cost, not price × seconds (0.30)
    assert result.raw["cost_source"] == "usage.cost" and "usage.cost" in result.raw["cost_note"]
    assert result.raw["requested_duration"] == 6 and result.external_job_id == JOB and result.model == MODEL
    assert len(clock.sleeps) == 2 and all(3.0 <= s <= 30.0 for s in clock.sleeps)
    assert stages[0] == "submitted" and stages[-1] == "done"


@respx.mock
def test_audio_and_seed_are_only_sent_when_the_model_supports_them(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing(_video_model(generate_audio=False, seed=None,
                                                                  supported_resolutions=None)))  # fmt: skip
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_completed(None))
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.generate(_req(negative_prompt=""), str(tmp_path / "clip.mp4"))
    body = json.loads(submit.calls.last.request.content)
    assert body == {"model": MODEL, "prompt": _req().prompt, "duration": 6, "aspect_ratio": "9:16"}
    # No usage.cost reported → the price table (0.05 $/s × 6 s), marked as an estimate.
    assert result.cost_usd == pytest.approx(0.30) and result.raw["cost_source"] == "estimate"
    assert "推定" in result.raw["cost_note"]


@respx.mock
def test_image_to_video_sends_the_first_frame(provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing(
        _video_model(), _video_model("vendor/text-only", supported_frame_images=[])
    ))  # fmt: skip
    image = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1", str(image)],
                   check=True, timeout=30)  # fmt: skip
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_completed())
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    provider.generate(_req(image_path=str(image)), str(tmp_path / "clip.mp4"))
    (frame,) = json.loads(submit.calls.last.request.content)["frame_images"]
    assert frame["type"] == "image_url" and frame["frame_type"] == "first_frame"
    assert frame["image_url"]["url"].startswith("data:image/png;base64,")
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(model="vendor/text-only", image_path=str(image)), str(tmp_path / "x.mp4"))
    assert info.value.code == "provider_bad_input" and submit.call_count == 1


@respx.mock
def test_unknown_price_models_are_refused_before_submitting(provider: OpenRouterProvider, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing(_video_model("vendor/no-price", pricing_skus=None)))
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(model="vendor/no-price"), str(tmp_path / "clip.mp4"))
    assert "料金" in info.value.message and submit.called is False


@pytest.mark.parametrize(
    ("status", "retryable", "code", "text"),
    [
        (402, False, "provider_payment_required", "クレジットが不足"),
        (401, False, "provider_auth_failed", "OPENROUTER_API_KEY が無効"),
        (403, False, "provider_rejected", "モデレーション"),
        (413, False, "provider_bad_input", "大きすぎます"),
        (400, False, "provider_rejected", "HTTP 400"),
        (408, True, "provider_unavailable", "HTTP 408"),
        (429, True, "provider_rate_limited", "HTTP 429"),
        (503, True, "provider_unavailable", "HTTP 503"),
        (529, True, "provider_unavailable", "HTTP 529"),
    ],
)
@respx.mock
def test_submit_http_errors(
    provider: OpenRouterProvider, tmp_path: Path, status: int, retryable: bool, code: str, text: str
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(status, json={"error": {"code": status,
                                                                                   "message": "nope"}}))
    submitted: list[str] = []
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"), on_submitted=lambda j, t: submitted.append(j))
    assert not isinstance(info.value, ProviderTaskError)  # no job exists: nothing billed, nothing reported
    assert (info.value.retryable, info.value.code) == (retryable, code) and text in info.value.message
    assert "nope" in info.value.message and submitted == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ConnectError("refused"), "provider_unreachable"),
        (httpx.ConnectTimeout("connect timed out"), "provider_timeout"),
        (httpx.PoolTimeout("no free connection"), "provider_timeout"),
    ],
)
@respx.mock
def test_connect_phase_errors_on_submit_are_retryable(
    provider: OpenRouterProvider, tmp_path: Path, error: Exception, code: str
) -> None:
    # The request never left this process: no job can exist, so sending it again is safe.
    respx.get(MODELS_URL).mock(return_value=_listing())
    submit = respx.post(SUBMIT_URL).mock(side_effect=error)
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert info.value.retryable and info.value.code == code and submit.call_count == 1
    assert getattr(info.value, "billed", False) is False


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.ReadError("connection reset by peer"),
        httpx.Response(500, json={"error": {"code": 500, "message": "Internal Server Error"}}),
        httpx.Response(502, json={"error": {"code": 502, "message": "Provider returned error"}}),
        httpx.Response(504, text="gateway timeout"),
        httpx.Response(524, text="error code: 524"),
        httpx.Response(202, text="not json"),
        httpx.Response(202, json=["unexpected", "shape"]),
        httpx.Response(202, json={"id": "gen vid with spaces", "status": "pending"}),
        httpx.Response(202, json={"status": "pending"}),
    ],
    ids=["read-timeout", "write-timeout", "disconnected", "read-error", "500", "502", "504", "524",
         "202-not-json", "202-not-object", "202-bad-id", "202-no-id"],
)  # fmt: skip
@respx.mock
def test_ambiguous_submit_failures_are_presumed_billed_and_never_resent(
    provider: OpenRouterProvider, tmp_path: Path, answer: Exception | httpx.Response
) -> None:
    """POST /videos has no idempotency key: once the request may have reached OpenRouter a job may exist."""
    respx.get(MODELS_URL).mock(return_value=_listing())
    if isinstance(answer, Exception):
        submit = respx.post(SUBMIT_URL).mock(side_effect=answer)
    else:
        submit = respx.post(SUBMIT_URL).mock(return_value=answer)
    submitted: list[str] = []
    with pytest.raises(ProviderError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"), on_submitted=lambda j, t: submitted.append(j))
    error = info.value
    assert error.code == "provider_submit_ambiguous" and error.retryable is False
    assert getattr(error, "billed", False) is True  # the pipeline books the estimate instead of resending
    assert "重複課金を避けて再送信しませんでした" in error.message
    assert submit.call_count == 1 and submitted == []


@pytest.mark.parametrize(
    ("job", "retryable", "code", "failure", "cost"),
    [
        (_job("failed", error="Internal server error, please try again", usage={"cost": 0}), True,
         "provider_failed", "failed", 0.0),
        (_job("failed", error="Request blocked by content moderation"), False, "provider_content_filtered",
         "failed", None),
        (_job("failed", error="Invalid duration for this model", usage={"cost": 0.12}), False, "provider_failed",
         "failed", 0.12),
        (_job("cancelled"), False, "provider_failed", "cancelled", None),
        (_job("expired", usage={"cost": 0.3}), False, "provider_bad_output", "expired", 0.3),
    ],
)  # fmt: skip
@respx.mock
def test_terminal_job_statuses(
    provider: OpenRouterProvider, tmp_path: Path, job: httpx.Response, retryable: bool, code: str, failure: str,
    cost: float | None,
) -> None:  # fmt: skip
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=job)
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"))
    error = info.value
    assert (error.retryable, error.code, error.failure_code) == (retryable, code, failure)
    assert error.external_job_id == JOB and error.cost_usd == cost


@respx.mock
def test_unknown_status_is_tolerated_then_fails(provider: OpenRouterProvider, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    poll = respx.get(POLL_URL).mock(return_value=_job("mystery"))
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert info.value.retryable and info.value.code == "provider_bad_response" and poll.call_count == 5


@respx.mock
def test_timeout_abandons_the_job_without_resubmitting(provider: OpenRouterProvider, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_job("in_progress"))
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"), timeout=60)
    assert info.value.code == "provider_timeout" and info.value.retryable and info.value.external_job_id == JOB
    assert submit.call_count == 1 and OpenRouterProvider.supports_remote_cancel is False  # no cancel endpoint


@respx.mock
def test_cancel_check_stops_polling(provider: OpenRouterProvider, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    poll = respx.get(POLL_URL).mock(return_value=_job("pending"))
    checks = iter([False, False, True])
    with pytest.raises(ProviderCancelled) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"), cancel_check=lambda: next(checks))
    assert info.value.external_job_id == JOB and poll.call_count == 1


# --- SSRF guards, redirects, downloads ------------------------------------------------------------------------------


@respx.mock
def test_polling_url_on_another_origin_is_never_used(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(
        202, json={"id": JOB, "polling_url": "https://evil.example/poll?steal=1", "status": "pending"}
    ))  # fmt: skip
    evil = respx.get("https://evil.example/poll").mock(return_value=_completed())
    poll = respx.get(POLL_URL).mock(return_value=_completed())
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert evil.called is False and poll.call_count == 1


@respx.mock
def test_same_origin_polling_url_is_used(provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    alt = f"{BASE}/videos/{JOB}?view=status"
    respx.post(SUBMIT_URL).mock(return_value=httpx.Response(202, json={"id": JOB, "polling_url": alt,
                                                                       "status": "pending"}))  # fmt: skip
    poll = respx.get(alt).mock(return_value=_completed())
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert poll.call_count == 1 and poll.calls.last.request.url.params["view"] == "status"


@respx.mock
def test_content_redirect_drops_the_authorization_header(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_completed())
    storage_url = "https://storage.example.net/videos/out.mp4?sig=abc"
    content = respx.get(CONTENT_URL).mock(return_value=httpx.Response(302, headers={"location": storage_url}))
    storage = respx.get("https://storage.example.net/videos/out.mp4").mock(
        return_value=httpx.Response(200, content=mp4_bytes)
    )
    result = provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert content.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in storage.calls.last.request.headers  # the key never leaves the API origin
    assert Path(result.path).read_bytes() == mp4_bytes


@respx.mock
def test_redirect_to_a_private_address_is_refused(provider: OpenRouterProvider, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_completed(0.31, unsigned_urls=[]))
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
    ))  # fmt: skip
    metadata = respx.get(url__regex=r"http://169\.254\.169\.254/.*").mock(return_value=httpx.Response(200))
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert metadata.called is False and info.value.code == "provider_bad_output"
    # The job finished (billed): the error carries the cost and is final, never resubmitted.
    assert info.value.retryable is False and info.value.cost_usd == pytest.approx(0.31) and submit.call_count == 1


@respx.mock
def test_unsigned_urls_are_a_fallback_for_https_public_hosts_only(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    good = "https://cdn.example.org/v/out.mp4"
    respx.get(POLL_URL).mock(return_value=_completed(0.31, unsigned_urls=[
        "http://cdn.example.org/v/plain.mp4", "https://127.0.0.1/v/local.mp4", "https://localhost/v/x.mp4",
        "file:///etc/passwd", good,
    ]))  # fmt: skip
    content = respx.get(CONTENT_URL).mock(return_value=httpx.Response(404, json={"error": {"message": "gone?"}}))
    plain = respx.get("http://cdn.example.org/v/plain.mp4").mock(return_value=httpx.Response(200, content=b"x"))
    local = respx.get("https://127.0.0.1/v/local.mp4").mock(return_value=httpx.Response(200, content=b"x"))
    cdn = respx.get(good).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert content.call_count == 1 and plain.called is False and local.called is False
    assert "authorization" not in cdn.calls.last.request.headers
    assert Path(result.path).read_bytes() == mp4_bytes and result.cost_usd == pytest.approx(0.31)


@respx.mock
def test_content_409_is_retried_without_resubmitting(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    poll = respx.get(POLL_URL).mock(return_value=_completed(0.31, unsigned_urls=[]))
    content = respx.get(CONTENT_URL).mock(side_effect=[
        httpx.Response(409, json={"error": {"message": "not ready"}}), httpx.Response(200, content=mp4_bytes),
    ])  # fmt: skip
    result = provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert submit.call_count == 1 and content.call_count == 2 and poll.call_count == 2  # re-read in between
    assert result.cost_usd == pytest.approx(0.31)


@respx.mock
def test_content_410_is_final_and_carries_the_cost(provider: OpenRouterProvider, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_completed(0.31, unsigned_urls=[]))
    content = respx.get(CONTENT_URL).mock(return_value=httpx.Response(410, json={"error": {"message": "gone"}}))
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert content.call_count == 1 and info.value.retryable is False and info.value.http_status == 410
    assert info.value.cost_usd == pytest.approx(0.31) and "新しいジョブは送信しません" in info.value.message


@respx.mock
def test_oversized_download_is_refused(
    provider: OpenRouterProvider, set_env: Callable[..., Any], clock: FakeClock, tmp_path: Path
) -> None:
    set_env(OPENROUTER_API_KEY=KEY, MAX_VIDEO_MB="1")
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_completed(0.31, unsigned_urls=[]))
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=b"0" * (2 * 1024 * 1024)))
    with pytest.raises(ProviderTaskError) as info:
        _make(clock).generate(_req(), str(tmp_path / "clip.mp4"))
    assert info.value.code == "provider_bad_output" and "大きすぎ" in info.value.message
    assert not (tmp_path / "clip.mp4").exists()


@respx.mock
def test_resume_polls_the_existing_job_without_submitting(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    poll = respx.get(POLL_URL).mock(side_effect=[_job("in_progress"), _completed(0.29)])
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.resume(JOB, _req(), str(tmp_path / "clip.mp4"))
    assert submit.called is False and poll.call_count == 2
    assert result.external_job_id == JOB and result.cost_usd == pytest.approx(0.29)
    with pytest.raises(ProviderError):
        provider.resume("../../etc/passwd", _req(), str(tmp_path / "x.mp4"))


@pytest.mark.parametrize(
    "listing",
    [
        [_video_model()],  # the model left the listing
        [_video_model(UNLISTED, pricing_skus=None)],  # still listed, but its price can no longer be read
        [_video_model(UNLISTED, supported_aspect_ratios=["16:9"])],  # no longer offers 9:16
    ],
    ids=["model-removed", "price-unreadable", "aspect-dropped"],
)
@pytest.mark.parametrize(("reported", "booked"), [(0.42, 0.42), (None, 0.3)], ids=["usage-cost", "no-usage-cost"])
@respx.mock
def test_resume_follows_the_job_whatever_the_current_listing_says(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path, listing: list[dict[str, Any]],
    reported: float | None, booked: float,
) -> None:  # fmt: skip
    """The job exists already: the listing only matters for new submissions, never for following a job."""
    respx.get(MODELS_URL).mock(return_value=_listing(*listing))
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    poll = respx.get(POLL_URL).mock(side_effect=[_job("in_progress"), _completed(reported)])
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    req = _req(model=UNLISTED, duration=6.0)
    result = provider.resume(JOB, req, str(tmp_path / "clip.mp4"), submitted_usd=0.3)
    assert submit.called is False and poll.call_count == 2
    assert result.external_job_id == JOB and result.model == UNLISTED and result.raw["requested_duration"] == 6.0
    # usage.cost when reported, else what the job was estimated at when it was submitted (never $0).
    assert result.cost_usd == pytest.approx(booked)
    # Submitting that model now is still refused (the listing decides for new jobs).
    with pytest.raises(ProviderError):
        provider.generate(req, str(tmp_path / "new.mp4"))
    assert submit.called is False


@respx.mock
def test_resume_without_a_key_is_not_configured(
    set_env: Callable[..., Any], clock: FakeClock, tmp_path: Path
) -> None:
    set_env(OPENROUTER_API_KEY="")
    poll = respx.get(POLL_URL).mock(return_value=_completed())
    with pytest.raises(ProviderNotConfigured):
        _make(clock).resume(JOB, _req(), str(tmp_path / "clip.mp4"), submitted_usd=0.3)
    assert poll.called is False


def test_generate_without_key_is_not_configured(set_env: Callable[..., Any], clock: FakeClock) -> None:
    set_env(OPENROUTER_API_KEY="")
    with pytest.raises(ProviderNotConfigured):
        _make(clock).generate(_req(), "/tmp/never.mp4")


def test_poll_schedule_is_bounded(set_env: Callable[..., Any]) -> None:
    config: PollConfig = OpenRouterProvider.poll_config
    assert (config.initial, config.maximum, config.minimum) == (5.0, 30.0, 3.0)


# --- review follow-ups: listing, SSRF literals, cost labels, BYOK ------------------------------------------------


@pytest.mark.parametrize(
    "bad_body",
    [{"data": []}, {"data": [{"id": "BAD ID"}]}, {"data": [{"foo": 1}]},
     {"data": [_video_model("vendor/landscape-only", supported_aspect_ratios=["16:9"])]},
     {"data": [_video_model("vendor/" + "x" * 84)]}],  # 91 chars: "<id>@1080p" would not fit Clip.model
    ids=["empty", "invalid-id", "no-id", "no-9:16", "id-too-long"],
)  # fmt: skip
def test_a_listing_without_usable_models_never_replaces_the_good_one(
    set_env: Callable[..., Any], clock: FakeClock, bad_body: dict[str, Any]
) -> None:
    settings = set_env(OPENROUTER_API_KEY=KEY, OPENROUTER_MODELS_CACHE_TTL="600")
    cache_file = settings.WORK_DIR / "cache" / CACHE_FILE_NAME
    with respx.mock(assert_all_called=False) as router:
        route = router.get(MODELS_URL).mock(return_value=_listing(_video_model(pricing_skus={"per-video-second": "0.07"})))
        provider = _make(clock)
        assert provider.get_model(MODEL).price_per_second_usd == 0.07
        good_file = cache_file.read_text(encoding="utf-8")

        clock.wall += 700  # past the TTL: the refetch answers 200 with nothing this app can use
        route.mock(return_value=httpx.Response(200, json=bad_body))
        catalogue = provider.catalogue()
        assert catalogue.stale and "no usable model" in (catalogue.error or "")
        assert provider.get_model(MODEL).price_per_second_usd == 0.07  # the good listing is still served
        assert cache_file.read_text(encoding="utf-8") == good_file  # and was not overwritten on disk
        assert "最新ではない" in provider.info()["models_note"]
        provider.models()
        assert route.call_count == 2  # backing off like any failed fetch

        reset_model_cache(delete_file=False)  # a new process still finds the good listing on disk
        assert _make(clock).get_model(MODEL).price_per_second_usd == 0.07


def test_a_first_listing_without_usable_models_falls_back_to_the_built_in_list(
    set_env: Callable[..., Any], clock: FakeClock
) -> None:
    settings = set_env(OPENROUTER_API_KEY=KEY)
    with respx.mock(assert_all_called=False) as router:
        router.get(MODELS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        provider = _make(clock)
        assert provider.catalogue().source == "fallback"
        assert MODEL in {m.id for m in provider.models()}
        assert not (settings.WORK_DIR / "cache" / CACHE_FILE_NAME).exists()


@pytest.mark.parametrize(
    "host",
    ["2130706433", "127.1", "0x7f000001", "0xa9fea9fe", "2852039166", "0177.0.0.1", "127.0.0.1.", "localhost.",
     "10.1", "[::ffff:127.0.0.1]"],
)  # fmt: skip
def test_non_canonical_ip_literals_are_not_public_hosts(provider: OpenRouterProvider, host: str) -> None:
    assert provider._allowed_download_url(f"https://{host}/latest/meta-data/") is False


@pytest.mark.parametrize("host", ["cdn.example.org", "storage.googleapis.com", "8.8.8.8", "1e100.net"])
def test_public_hosts_stay_allowed(provider: OpenRouterProvider, host: str) -> None:
    assert provider._allowed_download_url(f"https://{host}/v/out.mp4") is True


@respx.mock
def test_numeric_host_redirects_and_unsigned_urls_are_refused(provider: OpenRouterProvider, tmp_path: Path) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    submit = respx.post(SUBMIT_URL).mock(return_value=_submitted())
    numeric = ["https://2130706433/v.mp4", "https://127.1/v.mp4", "https://0x7f000001/v.mp4"]
    respx.get(POLL_URL).mock(return_value=_completed(0.31, unsigned_urls=numeric))
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(
        302, headers={"location": "https://0xa9fea9fe/latest/meta-data/"}
    ))  # fmt: skip
    probes = respx.get(url__regex=r"https://(2130706433|127\.1|0x7f000001|0xa9fea9fe)/.*").mock(
        return_value=httpx.Response(200, content=b"x")
    )
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert probes.called is False and info.value.code == "provider_bad_output"
    assert info.value.cost_usd == pytest.approx(0.31) and submit.call_count == 1


@respx.mock
def test_reported_cost_keeps_its_label_after_an_output_refresh(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path
) -> None:
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    # Completed with usage.cost 0.42; the re-read after a failed download omits usage.
    poll = respx.get(POLL_URL).mock(side_effect=[_completed(0.42, unsigned_urls=[]),
                                                 _completed(None, unsigned_urls=[])])  # fmt: skip
    respx.get(CONTENT_URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, content=mp4_bytes)])
    result = provider.generate(_req(), str(tmp_path / "clip.mp4"))
    assert poll.call_count == 2 and result.cost_usd == pytest.approx(0.42)
    assert result.raw["cost_source"] == "usage.cost" and "推定" not in result.raw["cost_note"]


@pytest.mark.parametrize(
    ("reported", "booked", "source"),
    [(0.0, 0.3, "estimate"), (0.012, 0.3, "estimate"), (0.45, 0.45, "usage.cost")],
    ids=["fee-zero", "fee-only", "above-estimate"],
)
@respx.mock
def test_byok_jobs_are_never_booked_below_the_estimate(
    provider: OpenRouterProvider, mp4_bytes: bytes, tmp_path: Path, reported: float, booked: float, source: str
) -> None:
    """BYOK: usage.cost may be OpenRouter's fee only (the vendor bills the user's own key) [B]."""
    respx.get(MODELS_URL).mock(return_value=_listing())
    respx.post(SUBMIT_URL).mock(return_value=_submitted())
    respx.get(POLL_URL).mock(return_value=_job("completed", usage={"cost": reported, "is_byok": True},
                                               unsigned_urls=[]))  # fmt: skip
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4_bytes))
    result = provider.generate(_req(), str(tmp_path / "clip.mp4"))  # 6 s × $0.05 = $0.30 estimate
    assert result.cost_usd == pytest.approx(booked) and result.raw["cost_source"] == source
    assert "BYOK" in result.raw["cost_note"]
