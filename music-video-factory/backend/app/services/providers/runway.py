"""Runway API (docs/VIDEO_PROVIDERS.md §4.3; shapes from the official @runwayml/sdk 4.21).

* ``POST /v1/text_to_video`` (or ``/v1/image_to_video`` with ``promptImage``)
  -> ``{"id", "estimatedCost": {"credits"}}``
* ``GET /v1/tasks/{id}``: ``PENDING | THROTTLED | RUNNING | SUCCEEDED | FAILED |
  CANCELLED``; ``SUCCEEDED`` carries ``output[]`` URLs (expire in 24-48 h) and
  ``cost.credits`` (1 credit = $0.01), which replaces the price-table cost.
  When downloading the output fails, the task is read again for fresh URLs
  (``ApiVideoProvider._refresh_output``); a succeeded task is never resubmitted.
* ``DELETE /v1/tasks/{id}`` cancels a running task (used on cancel/timeout).
* ``FAILED`` / ``CANCELLED`` tasks carry their reported ``cost.credits`` as
  ``ProviderTaskError.cost_usd`` (the pipeline books it as known spend).

Every request sends ``Authorization: Bearer`` and ``X-Runway-Version``. Runway
asks clients not to poll a task more often than every 5 s.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from app.config import get_settings
from app.services.errors import ProviderError
from app.services.providers.base import (
    PRICING_AS_OF,
    ApiVideoProvider,
    ClipRequest,
    PollConfig,
    PollOutcome,
    ProviderCancelled,
    ProviderModel,
    ProviderTaskError,
    SubmittedJob,
    read_image_base64,
    safe_path_id,
    truncate_utf16,
)

__all__ = ["RUNWAY_API_VERSION", "RunwayProvider", "classify_failure_code"]

RUNWAY_API_VERSION = "2024-11-06"
PRICING_URL = "https://docs.dev.runwayml.com/guides/pricing/"
USD_PER_CREDIT = 0.01
_PROMPT_MAX_UTF16 = 1000
_MAX_DATA_URI_BYTES = 5 * 1024 * 1024
_RATIOS = {"9:16": "720:1280", "16:9": "1280:720"}
_PENDING = frozenset({"PENDING", "THROTTLED"})


def classify_failure_code(code: str | None) -> bool:
    """Whether a FAILED task is worth retrying (Runway task-failure guide).

    ``INTERNAL*`` (incl. ``INTERNAL.BAD_OUTPUT.*``) and a missing code are
    retryable; ``SAFETY.*``, ``ASSET.INVALID`` and unknown codes are not.
    """
    if code is None or code == "":
        return True
    return code.upper().startswith("INTERNAL")


def _runway_model(
    model_id: str,
    name: str,
    credits_per_second: float,
    *,
    durations: list[float],
    note: str,
    text_to_video: bool = True,
    params: dict[str, Any] | None = None,
) -> ProviderModel:
    price = round(credits_per_second * USD_PER_CREDIT, 4)
    return ProviderModel(
        id=model_id,
        name=name,
        clip_durations=durations,
        max_clip_duration=max(durations),
        resolution="720x1280",
        supports_vertical=True,
        price_per_second_usd=price,
        price_note=(
            f"{credits_per_second:g} クレジット/秒 × $0.01 = ${price:g}/秒。{note}"
            f"{PRICING_AS_OF} 時点の公式料金表より（未検証。実費はタスク完了時の cost.credits で記録）"
        ),
        verified=False,
        default_duration=next((d for d in durations if d >= 5.0), max(durations)),
        text_to_video=text_to_video,
        image_to_video=True,
        source_url=PRICING_URL,
        as_of=PRICING_AS_OF,
        params=params or {},
    )


class RunwayProvider(ApiVideoProvider):
    id = "runway"
    name = "Runway"
    commercial_use_note = (
        "Runway 利用規約: 出力の権利は利用者にあり、商用利用は制限されません。"
        "肖像・商標・第三者素材の権利処理は利用者の責任です。"
        "API 利用時は UI に「Powered by Runway」の表示が求められています。"
    )
    poll_config: ClassVar[PollConfig] = PollConfig(initial=6.0, maximum=30.0, factor=1.4, jitter=0.2, minimum=5.0)
    supports_remote_cancel: ClassVar[bool] = True  # DELETE /v1/tasks/{id}

    def __init__(self, *, api_base: str | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._api_base_override = api_base

    @property
    def api_base(self) -> str:
        return (self._api_base_override or get_settings().RUNWAY_API_BASE).rstrip("/")

    def models(self) -> list[ProviderModel]:
        integer_durations = [float(d) for d in range(2, 11)]
        return [
            _runway_model(
                "gen4.5", "Gen-4.5", 12, durations=integer_durations, note="テキスト・画像の両方から生成可（無音）。",
                params={"moderation": True},
            ),
            _runway_model(
                "gen4_turbo", "Gen-4 Turbo（画像→動画専用）", 5, durations=integer_durations,
                note="入力画像が必須（テキストだけでは生成不可）。1 回あたり最低 10 クレジット。",
                text_to_video=False, params={"moderation": True},
            ),
            _runway_model(
                "veo3.1_fast", "Veo 3.1 Fast（Runway 経由・無音）", 10, durations=[4.0, 6.0, 8.0],
                note="Google Veo 3.1 Fast を Runway のキーで利用（audio:false の単価）。",
                params={"audio": False, "negative_prompt": True},
            ),
        ]  # fmt: skip

    def is_configured(self) -> tuple[bool, str | None]:
        if get_settings().RUNWAY_API_KEY:
            return True, None
        return False, "RUNWAY_API_KEY が未設定です。backend/.env に設定すると利用できます"

    def _headers(self) -> dict[str, str]:
        key = get_settings().RUNWAY_API_KEY
        if not key:
            raise ProviderError("RUNWAY_API_KEY が未設定です", retryable=False, code="provider_auth_failed")
        return {"Authorization": f"Bearer {key}", "X-Runway-Version": RUNWAY_API_VERSION}

    def build_request(self, req: ClipRequest, model: ProviderModel, duration: float) -> tuple[str, dict[str, Any]]:
        """``(path, JSON body)`` for the create call."""
        body: dict[str, Any] = {
            "model": model.id,
            "promptText": truncate_utf16(req.prompt, _PROMPT_MAX_UTF16),
            "ratio": _RATIOS[req.aspect_ratio],
            "duration": round(duration),
        }
        if req.seed is not None:
            body["seed"] = int(req.seed) % 4_294_967_296
        if model.params.get("audio") is False:
            body["audio"] = False
        if model.params.get("negative_prompt") and req.negative_prompt.strip():
            body["negativePrompt"] = req.negative_prompt.strip()
        if model.params.get("moderation"):
            # "auto" is Runway's default public-figure check; never relax it ("low").
            body["contentModeration"] = {"publicFigureThreshold": "auto"}
        if req.image_path:
            data, mime = read_image_base64(req.image_path, max_bytes=_MAX_DATA_URI_BYTES)
            body["promptImage"] = f"data:{mime};base64,{data}"
            return "/v1/image_to_video", body
        return "/v1/text_to_video", body

    def _submit(self, client: httpx.Client, req: ClipRequest, model: ProviderModel, duration: float) -> SubmittedJob:
        path, body = self.build_request(req, model, duration)
        response = self._request(client, "POST", f"{self.api_base}{path}", what="生成リクエスト", json=body,
                                 headers=self._headers())  # fmt: skip
        data = self._json(response, "生成リクエスト")
        task_id = safe_path_id(data.get("id"), provider_name=self.name, what="タスク ID")
        return SubmittedJob(id=task_id, raw={"id": task_id, "estimatedCost": data.get("estimatedCost")})

    def _poll(self, client: httpx.Client, job: SubmittedJob, model: ProviderModel) -> PollOutcome:
        response = self._request(client, "GET", f"{self.api_base}/v1/tasks/{job.id}", what="進捗の確認",
                                 headers=self._headers())  # fmt: skip
        task = self._json(response, "進捗の確認")
        status = str(task.get("status") or "").upper()
        credits = (task.get("cost") or {}).get("credits") if isinstance(task.get("cost"), dict) else None
        cost_usd = round(float(credits) * USD_PER_CREDIT, 4) if isinstance(credits, (int, float)) else None
        raw = {"status": status, "cost": task.get("cost")}
        if status in _PENDING:
            return PollOutcome(state="queued", raw=raw)
        if status == "RUNNING":
            progress = task.get("progress")
            return PollOutcome(state="running", progress=float(progress) if isinstance(progress, (int, float)) else None,
                               raw=raw)  # fmt: skip
        if status == "SUCCEEDED":
            output = task.get("output") or []
            url = output[0] if output and isinstance(output[0], str) else None
            return PollOutcome(state="succeeded", video_url=url, cost_usd=cost_usd, raw=raw)
        if status == "FAILED":
            failure_code = task.get("failureCode")
            failure = " ".join(str(task.get("failure") or "").split())[:300]
            retryable = classify_failure_code(failure_code)
            hint = "" if retryable else "。プロンプトや入力画像を見直してください"
            raise ProviderTaskError(
                f"{self.name}: 生成に失敗しました（{failure or '理由の記載なし'}、{failure_code or 'コードなし'}）{hint}",
                retryable=retryable,
                code="provider_content_filtered" if str(failure_code or "").upper().startswith("SAFETY") else "provider_failed",
                external_job_id=job.id,
                failure_code=failure_code,
                cost_usd=cost_usd,  # what Runway reports for the task (often 0); None when absent
            )
        if status == "CANCELLED":
            raise ProviderCancelled(f"{self.name}: タスクがキャンセルされました", external_job_id=job.id, cost_usd=cost_usd)
        raise ProviderError(f"{self.name}: 想定外のタスク状態です（{status or '空'}）", retryable=True,
                            code="provider_bad_response")  # fmt: skip

    def _cancel_remote(self, client: httpx.Client, job: SubmittedJob) -> None:
        self._request(client, "DELETE", f"{self.api_base}/v1/tasks/{job.id}", what="タスクのキャンセル",
                      headers=self._headers())  # fmt: skip
