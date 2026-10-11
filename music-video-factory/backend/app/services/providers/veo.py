"""Google Veo via the Gemini API (docs/VIDEO_PROVIDERS.md §4.1).

Flow (shapes from the generativelanguage v1beta Discovery document and the
official ``google-genai`` 2.29 SDK):

1. ``POST {base}/models/{model}:predictLongRunning`` with ``x-goog-api-key``
   -> ``{"name": "models/{model}/operations/{id}"}``
2. ``GET {base}/{name}`` until ``done`` -> ``response.generateVideoResponse
   .generatedSamples[0].video.uri`` (or ``error`` / RAI-filtered samples)
3. ``GET {uri}`` with the key; redirects to storage are followed *without* it.

Deliberately not sent: ``seed`` and ``generateAudio`` (the Gemini API rejects
them, so audio is always generated and billed), ``personGeneration`` (allowed
values differ by mode and region; the server default is used) and
``sampleCount`` (defaults to 1). The Gemini API has no cancel call for video
operations, so a cancelled or timed-out operation may still be billed.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.services.errors import ProviderError
from app.services.providers.base import (
    PRICING_AS_OF,
    ApiVideoProvider,
    ClipRequest,
    PollConfig,
    PollOutcome,
    ProviderModel,
    ProviderTaskError,
    SubmittedJob,
    read_image_base64,
    safe_path_id,
)

__all__ = ["GEMINI_API_BASE", "VeoProvider"]

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
PRICING_URL = "https://ai.google.dev/gemini-api/docs/pricing"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # inline request payload limit of the Gemini API
# google.rpc.Code values worth retrying: DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED, ABORTED, INTERNAL, UNAVAILABLE.
_RETRYABLE_RPC_CODES = frozenset({4, 8, 10, 13, 14})
_AUDIO_NOTE = "Gemini API では音声を無効にできないため、音声込みの単価で課金されます（音声は使いません）"


def _veo_model(model_id: str, name: str, price: float, *, resolution: str, durations: list[float], note: str) -> ProviderModel:
    api_model, _, _ = model_id.partition("@")
    return ProviderModel(
        id=model_id,
        name=name,
        clip_durations=durations,
        max_clip_duration=8.0,
        resolution="720x1280" if resolution == "720p" else "1080x1920",
        supports_vertical=True,
        price_per_second_usd=price,
        price_note=f"{note}。{_AUDIO_NOTE}。preview モデル。{PRICING_AS_OF} 時点の公式料金表より（未検証）",
        verified=False,
        default_duration=6.0 if durations != [8.0] else 8.0,
        text_to_video=True,
        image_to_video=True,
        source_url=PRICING_URL,
        as_of=PRICING_AS_OF,
        params={"api_model": api_model, "resolution": resolution},
    )


class VeoProvider(ApiVideoProvider):
    id = "veo"
    name = "Google Veo（Gemini API）"
    commercial_use_note = (
        "Gemini API 追加利用規約: Google は生成物の所有権を主張せず、利用の責任は利用者が負います。"
        "生成 AI 禁止使用ポリシーが適用され、全動画に不可視の SynthID 透かしが入ります。"
        "Veo 3.1 は有料ティア専用の preview モデルで、生成動画はサーバー上に 2 日間だけ保持されます。"
    )
    poll_config: ClassVar[PollConfig] = PollConfig(initial=5.0, maximum=20.0, factor=1.4, jitter=0.2, minimum=2.0)

    def __init__(self, *, api_base: str | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.api_base = (api_base or GEMINI_API_BASE).rstrip("/")

    def models(self) -> list[ProviderModel]:
        return [
            _veo_model(
                "veo-3.1-lite-generate-preview", "Veo 3.1 Lite（720p）", 0.05,
                resolution="720p", durations=[4.0, 6.0, 8.0], note="Veo 3.1 Lite 720p: $0.05/秒",
            ),
            _veo_model(
                "veo-3.1-lite-generate-preview@1080p", "Veo 3.1 Lite（1080p・8秒）", 0.08,
                resolution="1080p", durations=[8.0], note="Veo 3.1 Lite 1080p: $0.08/秒（1080p は 8 秒のみ）",
            ),
            _veo_model(
                "veo-3.1-fast-generate-preview", "Veo 3.1 Fast（720p）", 0.10,
                resolution="720p", durations=[4.0, 6.0, 8.0], note="Veo 3.1 Fast 720p: $0.10/秒",
            ),
            _veo_model(
                "veo-3.1-generate-preview", "Veo 3.1（720p）", 0.40,
                resolution="720p", durations=[4.0, 6.0, 8.0], note="Veo 3.1 720p: $0.40/秒",
            ),
        ]  # fmt: skip

    def is_configured(self) -> tuple[bool, str | None]:
        if get_settings().GOOGLE_API_KEY:
            return True, None
        return False, "GOOGLE_API_KEY（Gemini API キー）が未設定です。backend/.env に設定すると利用できます"

    # -- HTTP -----------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        key = get_settings().GOOGLE_API_KEY
        if not key:
            raise ProviderError("GOOGLE_API_KEY が未設定です", retryable=False, code="provider_auth_failed")
        return {"x-goog-api-key": key}

    def _http_error(self, response: httpx.Response, what: str) -> ProviderError:
        # Google answers an invalid key with 400 INVALID_ARGUMENT + ErrorInfo.reason API_KEY_INVALID.
        try:
            details = (response.json().get("error") or {}).get("details") or []
        except (ValueError, AttributeError):
            details = []
        if any(isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details):
            return ProviderError(
                f"{self.name}: {what}に失敗しました（API キーが無効です。GOOGLE_API_KEY を確認してください、HTTP {response.status_code}）",
                retryable=False,
                code="provider_auth_failed",
            )
        return super()._http_error(response, what)

    def _download_headers(self, url: str) -> dict[str, str]:
        # Only the API host gets the key (the file URI normally points there).
        api = urlsplit(self.api_base)
        target = urlsplit(url)
        if (target.scheme, target.hostname) == (api.scheme, api.hostname):
            return self._headers()
        return {}

    def build_request_body(self, req: ClipRequest, model: ProviderModel, duration: float) -> dict[str, Any]:
        instance: dict[str, Any] = {"prompt": req.prompt}
        if req.image_path:
            data, mime = read_image_base64(req.image_path, max_bytes=_MAX_IMAGE_BYTES)
            instance["image"] = {"bytesBase64Encoded": data, "mimeType": mime}
        parameters: dict[str, Any] = {
            "aspectRatio": req.aspect_ratio,
            "resolution": model.params["resolution"],
            "durationSeconds": round(duration),
        }
        if req.negative_prompt.strip():
            parameters["negativePrompt"] = req.negative_prompt.strip()
        return {"instances": [instance], "parameters": parameters}

    def _submit(self, client: httpx.Client, req: ClipRequest, model: ProviderModel, duration: float) -> SubmittedJob:
        api_model = model.params["api_model"]
        body = self.build_request_body(req, model, duration)
        response = self._request(
            client, "POST", f"{self.api_base}/models/{api_model}:predictLongRunning",
            what="生成リクエスト", json=body, headers=self._headers(),
        )  # fmt: skip
        data = self._json(response, "生成リクエスト")
        name = safe_path_id(data.get("name"), provider_name=self.name, what="オペレーション名")
        return SubmittedJob(id=name, raw={"name": name})

    def _poll(self, client: httpx.Client, job: SubmittedJob, model: ProviderModel) -> PollOutcome:
        response = self._request(client, "GET", f"{self.api_base}/{job.id}", what="進捗の確認", headers=self._headers())
        operation = self._json(response, "進捗の確認")
        if not operation.get("done"):
            return PollOutcome(state="running", raw={"done": False})
        error = operation.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = " ".join(str(error.get("message") or error.get("status") or "").split())[:300]
            raise ProviderTaskError(
                f"{self.name}: 生成に失敗しました（{message or 'エラー'}、code {code}）",
                retryable=code in _RETRYABLE_RPC_CODES,
                code="provider_failed",
                external_job_id=job.id,
                failure_code=str(error.get("status") or code),
            )
        video_response = (operation.get("response") or {}).get("generateVideoResponse") or {}
        samples = video_response.get("generatedSamples") or []
        uri = None
        if samples and isinstance(samples[0], dict):
            uri = (samples[0].get("video") or {}).get("uri")
        if uri:
            return PollOutcome(state="succeeded", video_url=str(uri), raw={"done": True, "samples": len(samples)})
        filtered = int(video_response.get("raiMediaFilteredCount") or 0)
        if filtered > 0:
            reasons = "、".join(str(r) for r in video_response.get("raiMediaFilteredReasons") or [])[:300]
            raise ProviderTaskError(
                f"{self.name}: 安全フィルタにより動画が除外されました（{reasons or '理由の記載なし'}）。プロンプトを見直してください",
                retryable=False,
                code="provider_content_filtered",
                external_job_id=job.id,
                failure_code="RAI_FILTERED",
            )
        raise ProviderTaskError(
            f"{self.name}: 生成は完了しましたが動画が返されませんでした",
            retryable=True,
            code="provider_bad_response",
            external_job_id=job.id,
        )
