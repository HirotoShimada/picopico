"""Shared provider machinery: duration snapping, polling schedule, HTTP error classification, downloads."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.services.errors import ProviderError
from app.services.providers.base import (
    OUTPUT_FETCH_ATTEMPTS,
    ApiVideoProvider,
    ClipRequest,
    PollConfig,
    PollOutcome,
    ProviderCancelled,
    ProviderModel,
    ProviderTaskError,
    SubmittedJob,
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


# --- ApiVideoProvider: a succeeded job is billed, so only its download is retried --------------------------------

OUT_URL = "https://cdn.example.net/out/clip.mp4"


class FinishedJobProvider(ApiVideoProvider):
    """Every job succeeds on its first poll with ``OUT_URL``; table price $0.05/s (6 s clip = $0.30)."""

    id = "fake"
    name = "Fake"
    poll_config = PollConfig(initial=5.0, maximum=5.0, factor=1.0, jitter=0.0, minimum=5.0)

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        super().__init__(sleep=self._fake_sleep, clock=lambda: self.now)
        self.submits = 0
        self.polls = 0
        self.remote_cancels = 0
        self.succeeded = False
        self.bad_probes = 0  # this many probes run on the real (non-video) bytes and fail

    def _fake_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def models(self) -> list[ProviderModel]:
        return [ProviderModel("fake-1", "Fake 1", [4.0, 6.0, 8.0], 8.0, "720x1280", True, 0.05, "", False)]

    def _submit(self, client: httpx.Client, req: ClipRequest, model: ProviderModel, duration: float) -> SubmittedJob:
        self.submits += 1
        return SubmittedJob(id=f"job-{self.submits}")

    def _poll(self, client: httpx.Client, job: SubmittedJob, model: ProviderModel) -> PollOutcome:
        self.polls += 1
        self.succeeded = True
        return PollOutcome(state="succeeded", video_url=OUT_URL)

    def _cancel_remote(self, client: httpx.Client, job: SubmittedJob) -> None:
        self.remote_cancels += 1

    def _probe_output(self, path: str, job_id: str) -> dict[str, Any]:
        if self.bad_probes:
            self.bad_probes -= 1
            return super()._probe_output(path, job_id)
        return {"duration": 6.0, "width": 720, "height": 1280, "has_video": True}


def _fake_req() -> ClipRequest:
    return ClipRequest("a fictional city at dawn", "", 5.0, "fake-1")


def test_task_error_cost_defaults_to_unknown() -> None:
    assert ProviderTaskError("x").cost_usd is None and ProviderCancelled().cost_usd is None
    assert ProviderTaskError("x", cost_usd=0.6).cost_usd == 0.6


@respx.mock
def test_failed_download_of_a_finished_job_is_retried_without_resubmitting(tmp_path: Path) -> None:
    cdn = respx.get(OUT_URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, content=b"video-bytes")])
    provider = FinishedJobProvider()
    result = provider.generate(_fake_req(), str(tmp_path / "clip.mp4"))
    assert provider.submits == 1 and cdn.call_count == 2
    assert provider.polls == 2  # the finished job was re-read for its output before the second try
    assert provider.sleeps == [5.0]  # backoff on the provider's poll schedule
    assert result.external_job_id == "job-1" and result.cost_usd == pytest.approx(0.3)
    assert (tmp_path / "clip.mp4").read_bytes() == b"video-bytes"


@respx.mock
def test_finished_job_whose_video_cannot_be_fetched_carries_its_cost(tmp_path: Path) -> None:
    cdn = respx.get(OUT_URL).mock(return_value=httpx.Response(503))
    provider = FinishedJobProvider()
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_fake_req(), str(tmp_path / "clip.mp4"))
    assert provider.submits == 1 and cdn.call_count == OUTPUT_FETCH_ATTEMPTS
    assert info.value.retryable is False  # the pipeline must not submit (and pay for) a new job
    assert info.value.cost_usd == pytest.approx(0.3) and info.value.external_job_id == "job-1"
    assert info.value.code == "provider_unavailable" and "課金済み" in info.value.message
    assert provider.remote_cancels == 0 and not (tmp_path / "clip.mp4").exists()


@respx.mock
def test_unreadable_output_is_downloaded_again(tmp_path: Path) -> None:
    cdn = respx.get(OUT_URL).mock(return_value=httpx.Response(200, content=b"not a video"))
    provider = FinishedJobProvider()
    provider.bad_probes = 1  # the first probe really runs ffprobe on the bytes and fails
    result = provider.generate(_fake_req(), str(tmp_path / "clip.mp4"))
    assert provider.submits == 1 and cdn.call_count == 2 and result.cost_usd == pytest.approx(0.3)


@respx.mock
def test_non_retryable_fetch_error_is_not_repeated_but_billed(tmp_path: Path) -> None:
    cdn = respx.get(OUT_URL).mock(return_value=httpx.Response(403, json={"error": "denied"}))
    provider = FinishedJobProvider()
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_fake_req(), str(tmp_path / "clip.mp4"))
    assert cdn.call_count == 1 and provider.submits == 1
    assert info.value.retryable is False and info.value.cost_usd == pytest.approx(0.3)


@respx.mock
def test_fetch_retries_stop_at_the_deadline(tmp_path: Path) -> None:
    cdn = respx.get(OUT_URL).mock(return_value=httpx.Response(503))
    provider = FinishedJobProvider()
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_fake_req(), str(tmp_path / "clip.mp4"), timeout=3)
    assert cdn.call_count == 2 and provider.sleeps == [3.0]  # one backoff, cut to the time left
    assert info.value.retryable is False and info.value.cost_usd == pytest.approx(0.3)


@respx.mock
def test_cancel_while_downloading_a_finished_job_carries_its_cost(tmp_path: Path) -> None:
    respx.get(OUT_URL).mock(return_value=httpx.Response(200, content=b"video-bytes"))
    provider = FinishedJobProvider()
    with pytest.raises(ProviderCancelled) as info:
        provider.generate(_fake_req(), str(tmp_path / "clip.mp4"), cancel_check=lambda: provider.succeeded)
    assert info.value.cost_usd == pytest.approx(0.3) and info.value.external_job_id == "job-1"
    assert provider.submits == 1 and provider.remote_cancels == 0  # a finished job is not cancelled/deleted


@respx.mock
def test_cancel_between_fetch_tries_carries_its_cost(tmp_path: Path) -> None:
    respx.get(OUT_URL).mock(return_value=httpx.Response(503))
    provider = FinishedJobProvider()
    with pytest.raises(ProviderCancelled) as info:
        provider.generate(_fake_req(), str(tmp_path / "clip.mp4"), cancel_check=lambda: bool(provider.sleeps))
    assert info.value.cost_usd == pytest.approx(0.3) and provider.submits == 1


@respx.mock
def test_output_that_cannot_be_saved_carries_its_cost(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    provider = FinishedJobProvider()
    with pytest.raises(ProviderTaskError) as info:
        provider.generate(_fake_req(), str(blocker / "clip.mp4"))
    assert info.value.retryable is False and info.value.cost_usd == pytest.approx(0.3)
    assert provider.submits == 1


# --- on_submitted / resume / remote cancel (crash recovery without paying twice) ---------------------------------


@respx.mock
def test_on_submitted_reports_the_job_right_after_submission(tmp_path: Path) -> None:
    respx.get(OUT_URL).mock(return_value=httpx.Response(200, content=b"video-bytes"))
    provider = FinishedJobProvider()
    reported: list[tuple[str, str, int]] = []
    provider.generate(_fake_req(), str(tmp_path / "clip.mp4"),
                      on_submitted=lambda job_id, token: reported.append((job_id, token, provider.polls)))  # fmt: skip
    assert reported == [("job-1", "job-1", 0)]  # before the first poll


@respx.mock
def test_a_failing_on_submitted_callback_does_not_break_generation(tmp_path: Path) -> None:
    respx.get(OUT_URL).mock(return_value=httpx.Response(200, content=b"video-bytes"))

    def broken(job_id: str, token: str) -> None:
        raise RuntimeError("db down")

    result = FinishedJobProvider().generate(_fake_req(), str(tmp_path / "clip.mp4"), on_submitted=broken)
    assert result.external_job_id == "job-1"


@respx.mock
def test_resume_polls_the_existing_job_without_submitting(tmp_path: Path) -> None:
    respx.get(OUT_URL).mock(return_value=httpx.Response(200, content=b"video-bytes"))
    provider = FinishedJobProvider()
    result = provider.resume("job-7", _fake_req(), str(tmp_path / "clip.mp4"), timeout=60)
    assert provider.submits == 0 and provider.polls == 1
    assert result.external_job_id == "job-7" and result.cost_usd == pytest.approx(0.3)
    with pytest.raises(ProviderCancelled):
        provider.resume("job-7", _fake_req(), str(tmp_path / "c2.mp4"), cancel_check=lambda: True)
    with pytest.raises(ProviderError):
        provider.resume("../etc", _fake_req(), str(tmp_path / "c3.mp4"))


def test_remote_cancel_capability_flags() -> None:
    from app.services.providers.kling import KlingProvider
    from app.services.providers.runway import RunwayProvider
    from app.services.providers.veo import VeoProvider

    assert RunwayProvider.supports_remote_cancel is True
    assert VeoProvider.supports_remote_cancel is False and KlingProvider.supports_remote_cancel is False
