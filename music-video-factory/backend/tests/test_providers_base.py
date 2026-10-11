"""Shared provider machinery: duration snapping, polling schedule, HTTP error classification, downloads."""

from __future__ import annotations

import random
from pathlib import Path

import httpx
import pytest
import respx

from app.services.errors import ProviderError
from app.services.providers.base import (
    ClipRequest,
    PollConfig,
    ProviderCancelled,
    ProviderModel,
    ProviderTaskError,
    classify_http_status,
    download_file,
    error_detail,
    safe_path_id,
    snap_duration,
    truncate_utf16,
)


def test_contract_dataclasses_accept_positional_contract_fields() -> None:
    model = ProviderModel("m", "M", [4, 6, 8], 8, "720x1280", True, 0.05, "note", False)
    assert model.default_duration == 5.0 and model.text_to_video and not model.image_to_video
    assert model.snap_duration(5) == 6
    assert model.cost_for(6) == pytest.approx(0.3)
    request = ClipRequest("prompt", "neg", 5.0, "m")
    assert request.aspect_ratio == "9:16" and request.seed is None and request.scene is None


@pytest.mark.parametrize(
    ("durations", "maximum", "wanted", "expected"),
    [
        ([4, 6, 8], 8, 5, 6),
        ([4, 6, 8], 8, 6, 6),
        ([4, 6, 8], 8, 6.0000001, 6),
        ([4, 6, 8], 8, 9, 8),  # longest when none is long enough
        ([4, 6, 8], 8, 0, 4),
        ([5, 10], 10, 5.2, 10),
        ([], 20, 7.25, 7.25),
        ([], 20, 30, 20),
        ([], 20, -1, 1.0),
    ],
)
def test_snap_duration(durations: list[float], maximum: float, wanted: float, expected: float) -> None:
    assert snap_duration(durations, maximum, wanted) == expected


def test_truncate_utf16_counts_surrogate_pairs() -> None:
    assert truncate_utf16("abc", 5) == "abc"
    assert truncate_utf16("あいうえお", 3) == "あいう"
    assert truncate_utf16("a😀b", 2) == "a"  # the emoji needs two UTF-16 units
    assert len(truncate_utf16("x" * 1500, 1000)) == 1000


def test_poll_interval_backs_off_with_cap_and_floor() -> None:
    config = PollConfig(initial=5, maximum=30, factor=2, jitter=0.0, minimum=5)
    rng = random.Random(1)
    assert [config.interval(i, rng) for i in range(5)] == [5, 10, 20, 30, 30]
    jittery = PollConfig(initial=5, maximum=30, factor=1.5, jitter=0.2, minimum=5)
    values = [jittery.interval(0, rng) for _ in range(200)]
    assert min(values) >= 5 and max(values) <= 6.0 + 1e-9


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (502, True), (503, True), (408, True), (400, False), (401, False), (403, False), (404, False), (422, False)],
)
def test_classify_http_status(status: int, retryable: bool) -> None:
    assert classify_http_status(status)[0] is retryable


def test_error_detail_reads_common_shapes() -> None:
    google = httpx.Response(400, json={"error": {"code": 400, "message": "bad   duration", "status": "INVALID_ARGUMENT"}})
    assert error_detail(google) == "bad duration"
    assert error_detail(httpx.Response(400, json={"error": "Invalid ratio"})) == "Invalid ratio"
    assert error_detail(httpx.Response(429, json={"code": 1302, "message": "too many"})) == "too many"
    assert error_detail(httpx.Response(502, text="x" * 1000)).endswith("…")


def test_safe_path_id_rejects_traversal() -> None:
    assert safe_path_id("models/veo/operations/abc-1", provider_name="P") == "models/veo/operations/abc-1"
    for bad in ("../secrets", "/abs", "a b", "", None, "x?y=1"):
        with pytest.raises(ProviderError):
            safe_path_id(bad, provider_name="P")


@respx.mock
def test_download_follows_redirect_without_leaking_credentials(tmp_path: Path) -> None:
    api = respx.get("https://api.example.com/files/1").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.net/v.mp4?sig=1"})
    )
    cdn = respx.get("https://cdn.example.net/v.mp4").mock(return_value=httpx.Response(200, content=b"video-bytes"))
    dest = tmp_path / "out" / "clip.mp4"
    with httpx.Client() as client:
        size = download_file(client, "https://api.example.com/files/1", dest, provider_name="P",
                             headers={"x-goog-api-key": "secret"})  # fmt: skip
    assert size == len(b"video-bytes") and dest.read_bytes() == b"video-bytes"
    assert api.calls.last.request.headers.get("x-goog-api-key") == "secret"
    assert "x-goog-api-key" not in cdn.calls.last.request.headers
    assert not list(dest.parent.glob(".*.part"))


@respx.mock
def test_download_enforces_size_limit_and_cleans_up(tmp_path: Path) -> None:
    respx.get("https://cdn.example.net/big.mp4").mock(return_value=httpx.Response(200, content=b"x" * 5000))
    dest = tmp_path / "big.mp4"
    with httpx.Client() as client, pytest.raises(ProviderTaskError) as info:
        download_file(client, "https://cdn.example.net/big.mp4", dest, provider_name="P", max_bytes=1000,
                      external_job_id="job-1")  # fmt: skip
    assert info.value.retryable is False and info.value.external_job_id == "job-1"
    assert not dest.exists() and not list(tmp_path.iterdir())


@respx.mock
def test_download_errors_are_classified(tmp_path: Path) -> None:
    respx.get("https://cdn.example.net/a.mp4").mock(return_value=httpx.Response(503))
    respx.get("https://cdn.example.net/b.mp4").mock(return_value=httpx.Response(403, json={"error": "expired"}))
    respx.get("https://cdn.example.net/c.mp4").mock(side_effect=httpx.ReadTimeout("slow"))
    with httpx.Client() as client:
        for name, retryable in (("a", True), ("b", False), ("c", True)):
            with pytest.raises(ProviderTaskError) as info:
                download_file(client, f"https://cdn.example.net/{name}.mp4", tmp_path / f"{name}.mp4", provider_name="P")
            assert info.value.retryable is retryable, name


@respx.mock
def test_download_honours_cancel_check(tmp_path: Path) -> None:
    respx.get("https://cdn.example.net/v.mp4").mock(return_value=httpx.Response(200, content=b"x" * 10))
    with httpx.Client() as client, pytest.raises(ProviderCancelled):
        download_file(client, "https://cdn.example.net/v.mp4", tmp_path / "v.mp4", provider_name="P",
                      cancel_check=lambda: True)  # fmt: skip
    assert not (tmp_path / "v.mp4").exists()
