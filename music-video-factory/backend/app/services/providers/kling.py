"""Kling AI API (docs/VIDEO_PROVIDERS.md §4.4).

Two official APIs share the host (``KLING_API_BASE``, default
``https://api-singapore.klingai.com``):

* **New API** (2026-06, ``KLING_API_KEY`` as ``Authorization: Bearer``) — model
  ``kling-3.0``: ``POST /text-to-video/kling-3.0`` (``/image-to-video/…`` with
  ``contents[first_frame]``), then ``GET /tasks?external_task_ids=<id>``;
  statuses ``submitted | processing | succeeded | failed``. ``multi_shot`` is
  sent as ``false`` (the default ``true`` cuts inside a clip) and ``audio`` as
  ``"off"``. There is no negative-prompt field, so negatives are appended to
  the prompt as "Avoid: …".
* **Legacy API** (``KLING_ACCESS_KEY``/``KLING_SECRET_KEY`` -> HS256 JWT per
  request) — model ``kling-v2-5-turbo``: ``POST /v1/videos/text2video`` (or
  ``image2video``), then ``GET /v1/videos/text2video/{task_id}``; the success
  status is spelled ``succeed``.

Responses carry a business ``code`` (0 = OK): 1302/1303 (rate/concurrency
limit) and 5xxx are retryable, other 1xxx (auth, account, resource pack
exhausted = 1102, parameters, content policy) are not. Kling has no
documented cancel call. Output URLs are deleted after 30 days.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
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
    ProviderModel,
    ProviderTaskError,
    SubmittedJob,
    classify_http_status,
    read_image_base64,
    safe_path_id,
)

__all__ = ["KlingProvider", "kling_code_retryable", "make_jwt"]

NEW_PRICING_URL = "https://kling.ai/document-api/updates/api"
LEGACY_PRICING_URL = "https://kling.ai/document-api/pricing/base/video"
USD_PER_UNIT = 0.14
JWT_TTL_SEC = 1800
_PROMPT_MAX_CHARS = 2500
# Consecutive task lookups (new API) that do not list the task before giving up (~1.5 min on the poll schedule).
MAX_LOOKUP_MISSES = 6
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Words in a failed task's message that suggest a transient server-side problem.
_TRANSIENT_HINTS = ("internal", "timeout", "timed out", "busy", "try again", "temporar", "unavailable", "system error")


def make_jwt(access_key: str, secret_key: str, *, now: float | None = None, ttl: int = JWT_TTL_SEC) -> str:
    """Legacy-API bearer token: HS256 JWT ``{iss: AK, exp: now+ttl, nbf: now-5}`` signed with SK."""

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    issued = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"iss": access_key, "exp": issued + ttl, "nbf": issued - 5}
    signing_input = ".".join(
        b64url(json.dumps(part, separators=(",", ":")).encode("utf-8")) for part in (header, payload)
    )
    signature = hmac.new(secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{b64url(signature)}"


def kling_code_retryable(code: int) -> bool | None:
    """Retryability of a Kling business code; ``None`` when the code is not classified."""
    if code in (1302, 1303) or 5000 <= code < 6000:
        return True
    if 1000 <= code < 1400:
        return False
    return None


def _code_summary(code: int) -> str:
    if code == 1102:
        return "リソースパックが枯渇または期限切れです（Kling のコンソールで購入・更新してください）"
    if code in (1302, 1303):
        return "リクエストが多すぎるか、同時実行数の上限に達しています"
    if code == 1301:
        return "コンテンツポリシーにより拒否されました。プロンプトを見直してください"
    if 1000 <= code < 1100:
        return "認証に失敗しました（API キーを確認してください）"
    if 1100 <= code < 1200:
        return "アカウントの状態により利用できません"
    if 1200 <= code < 1300:
        return "リクエストのパラメータが不正です"
    if 5000 <= code < 6000:
        return "Kling 側で一時的なエラーが発生しました"
    return "リクエストが拒否されました"


def _kling_model(
    model_id: str, name: str, units_per_second: float, *, durations: list[float], api: str, note: str, source_url: str,
    params: dict[str, Any],
) -> ProviderModel:  # fmt: skip
    price = round(units_per_second * USD_PER_UNIT, 4)
    return ProviderModel(
        id=model_id,
        name=name,
        clip_durations=durations,
        max_clip_duration=max(durations),
        resolution="720x1280",
        supports_vertical=True,
        price_per_second_usd=price,
        price_note=(
            f"{units_per_second:g} ユニット/秒 × $0.14 = ${price:g}/秒（720p・無音）。{note}"
            f"{PRICING_AS_OF} 時点の公式情報より（未検証）"
        ),
        verified=False,
        default_duration=5.0,
        text_to_video=True,
        image_to_video=True,
        source_url=source_url,
        as_of=PRICING_AS_OF,
        params={"api": api, **params},
    )


class KlingProvider(ApiVideoProvider):
    id = "kling"
    name = "Kling AI"
    commercial_use_note = (
        "Kling の案内では生成物の商用利用は可能とされていますが、API 有償サービス規約の本文（出力の所有権・実在人物の扱い）は未確認です。"
        "コミュニティガイドラインは他者の肖像権・プライバシーの尊重を求めています。API の出力 URL は透かしなしで、30 日後に削除されます。"
    )
    poll_config: ClassVar[PollConfig] = PollConfig(initial=5.0, maximum=30.0, factor=1.5, jitter=0.2, minimum=3.0)
    supported_aspects: ClassVar[tuple[str, ...]] = ("9:16", "16:9", "1:1")

    NEW_MODEL = "kling-3.0"
    LEGACY_MODEL = "kling-v2-5-turbo"

    def __init__(self, *, api_base: str | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._api_base_override = api_base

    @property
    def api_base(self) -> str:
        return (self._api_base_override or get_settings().KLING_API_BASE).rstrip("/")

    def models(self) -> list[ProviderModel]:
        return [
            _kling_model(
                self.NEW_MODEL, "Kling 3.0（新 API）", 0.8, durations=[float(d) for d in range(3, 16)], api="new",
                note="KLING_API_KEY が必要。料金行の対応（0.8 か 0.6 ユニット/秒か）は未確定。",
                source_url=NEW_PRICING_URL, params={"resolution": "720p"},
            ),
            _kling_model(
                self.LEGACY_MODEL, "Kling 2.5 Turbo（旧 API・std）", 0.3, durations=[5.0, 10.0], api="legacy",
                note="旧 API 用の KLING_ACCESS_KEY / KLING_SECRET_KEY が必要。",
                source_url=LEGACY_PRICING_URL, params={"mode": "std"},
            ),
        ]  # fmt: skip

    # -- configuration --------------------------------------------------------------------------

    @staticmethod
    def _keys() -> tuple[str | None, str | None, str | None]:
        settings = get_settings()
        return settings.KLING_API_KEY, settings.KLING_ACCESS_KEY, settings.KLING_SECRET_KEY

    def is_configured(self) -> tuple[bool, str | None]:
        api_key, access_key, secret_key = self._keys()
        legacy = bool(access_key and secret_key)
        if api_key:
            return True, None
        if legacy:
            return True, f"KLING_API_KEY が未設定のため、旧 API の {self.LEGACY_MODEL} のみ利用できます"
        if access_key or secret_key:
            return False, "旧 API には KLING_ACCESS_KEY と KLING_SECRET_KEY の両方が必要です（新 API は KLING_API_KEY）"
        return False, "KLING_API_KEY（新 API）が未設定です。backend/.env に設定すると利用できます"

    def default_model(self) -> str:
        api_key, access_key, secret_key = self._keys()
        if not api_key and access_key and secret_key:
            return self.LEGACY_MODEL
        return self.NEW_MODEL

    def model_credentials_problem(self, model: ProviderModel) -> str | None:
        api_key, access_key, secret_key = self._keys()
        if model.params["api"] == "new" and not api_key:
            return f"{model.name} には KLING_API_KEY（新 API のキー）が必要です"
        if model.params["api"] == "legacy" and not (access_key and secret_key):
            return f"{model.name} には旧 API 用の KLING_ACCESS_KEY と KLING_SECRET_KEY が必要です"
        return None

    def _headers(self, model: ProviderModel) -> dict[str, str]:
        api_key, access_key, secret_key = self._keys()
        if model.params["api"] == "legacy":
            if not (access_key and secret_key):
                raise ProviderError("Kling の旧 API キーが未設定です", retryable=False, code="provider_auth_failed")
            return {"Authorization": f"Bearer {make_jwt(access_key, secret_key)}"}
        if not api_key:
            raise ProviderError("KLING_API_KEY が未設定です", retryable=False, code="provider_auth_failed")
        return {"Authorization": f"Bearer {api_key}"}

    # -- HTTP -----------------------------------------------------------------------------------

    def _http_error(self, response: httpx.Response, what: str) -> ProviderError:
        try:
            body = response.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        if isinstance(code, int) and code != 0:
            return self._business_error(code, body.get("message"), what, http_status=response.status_code)
        return super()._http_error(response, what)

    def _business_error(self, code: int, message: Any, what: str, *, http_status: int | None = None) -> ProviderError:
        retryable = kling_code_retryable(code)
        if retryable is None:
            retryable = classify_http_status(http_status)[0] if http_status else False
        detail = " ".join(str(message or "").split())[:300]
        status = f"HTTP {http_status}, " if http_status else ""
        return ProviderError(
            f"{self.name}: {what}に失敗しました（{_code_summary(code)}、{status}code {code}{': ' + detail if detail else ''}）",
            retryable=retryable,
            code="provider_rate_limited" if code in (1302, 1303) else "provider_rejected",
        )

    def _checked(self, response: httpx.Response, what: str) -> dict[str, Any]:
        body = self._json(response, what)
        code = body.get("code", 0)
        if isinstance(code, int) and code != 0:
            raise self._business_error(code, body.get("message"), what)
        return body

    # -- requests -------------------------------------------------------------------------------

    @staticmethod
    def _prompt_with_negative(req: ClipRequest) -> str:
        prompt = req.prompt.strip()
        negative = req.negative_prompt.strip()
        if negative:
            prompt = f"{prompt} Avoid: {negative}."
        return prompt[:_PROMPT_MAX_CHARS]

    def build_request(self, req: ClipRequest, model: ProviderModel, duration: float, external_id: str) -> tuple[str, dict[str, Any]]:
        """``(path, JSON body)`` for the create call."""
        seconds = round(duration)
        image = read_image_base64(req.image_path, max_bytes=_MAX_IMAGE_BYTES)[0] if req.image_path else None
        if model.params["api"] == "legacy":
            body: dict[str, Any] = {
                "model_name": model.id,
                "prompt": req.prompt.strip()[:_PROMPT_MAX_CHARS],
                "mode": model.params.get("mode", "std"),
                "aspect_ratio": req.aspect_ratio,
                "duration": str(seconds),
                "external_task_id": external_id,
            }
            if req.negative_prompt.strip():
                body["negative_prompt"] = req.negative_prompt.strip()[:_PROMPT_MAX_CHARS]
            if image is not None:
                body["image"] = image
                return "/v1/videos/image2video", body
            return "/v1/videos/text2video", body
        body = {
            "prompt": self._prompt_with_negative(req),
            "settings": {
                "resolution": model.params.get("resolution", "720p"),
                "aspect_ratio": req.aspect_ratio,
                "duration": seconds,
                "audio": "off",
                "multi_shot": False,
            },
            "options": {"external_task_id": external_id, "watermark_info": {"enabled": False}},
        }
        if image is not None:
            body["contents"] = [{"type": "first_frame", "url": image}]
            return f"/image-to-video/{model.id}", body
        return f"/text-to-video/{model.id}", body

    def _submit(self, client: httpx.Client, req: ClipRequest, model: ProviderModel, duration: float) -> SubmittedJob:
        external_id = f"mvf-{uuid.uuid4().hex}"
        path, body = self.build_request(req, model, duration, external_id)
        response = self._request(client, "POST", f"{self.api_base}{path}", what="生成リクエスト", json=body,
                                 headers=self._headers(model))  # fmt: skip
        data = self._checked(response, "生成リクエスト").get("data") or {}
        if model.params["api"] == "legacy":
            task_id = safe_path_id(data.get("task_id"), provider_name=self.name, what="タスク ID")
            kind = "image2video" if req.image_path else "text2video"
            return SubmittedJob(id=task_id, raw={"task_id": task_id, "external_task_id": external_id},
                                data={"kind": kind})  # fmt: skip
        task_id = str(data.get("id") or "")
        return SubmittedJob(
            id=task_id or external_id,
            raw={"id": task_id, "external_task_id": external_id, "status": data.get("status")},
            data={"external_id": external_id, "task_id": task_id},
        )

    def resume_token(self, job: SubmittedJob) -> str:
        """New API: ``new:<external_task_id>:<task id>`` (polling needs the external id); legacy: ``legacy:<task id>``."""
        if "external_id" in job.data:
            return f"new:{job.data['external_id']}:{job.data.get('task_id') or ''}"
        return f"legacy:{job.id}"

    def _job_from_token(self, token: str, req: ClipRequest, model: ProviderModel) -> SubmittedJob:
        kind, _, rest = token.partition(":")
        if kind == "new" and model.params["api"] == "new":
            external_id, _, task_id = rest.partition(":")
            external_id = safe_path_id(external_id, provider_name=self.name, what="タスク ID")
            task_id = safe_path_id(task_id, provider_name=self.name, what="タスク ID") if task_id else ""
            return SubmittedJob(id=task_id or external_id, raw={"resumed": True},
                                data={"external_id": external_id, "task_id": task_id})  # fmt: skip
        if kind == "legacy" and model.params["api"] == "legacy":
            task_id = safe_path_id(rest, provider_name=self.name, what="タスク ID")
            return SubmittedJob(id=task_id, raw={"resumed": True},
                                data={"kind": "image2video" if req.image_path else "text2video"})  # fmt: skip
        raise ProviderError(f"{self.name}: 再開するジョブの情報が不正です", retryable=False, code="provider_bad_input")

    def _poll(self, client: httpx.Client, job: SubmittedJob, model: ProviderModel) -> PollOutcome:
        if model.params["api"] == "legacy":
            return self._poll_legacy(client, job, model)
        response = self._request(client, "GET", f"{self.api_base}/tasks", what="進捗の確認",
                                 params={"external_task_ids": job.data["external_id"]},
                                 headers=self._headers(model))  # fmt: skip
        items = self._checked(response, "進捗の確認").get("data") or []
        if isinstance(items, dict) and ("id" in items or "status" in items or "external_id" in items):
            items = [items]
        if not isinstance(items, list):
            raise ProviderError(f"{self.name}: 進捗の確認の応答形式が想定外です", retryable=True,
                                code="provider_bad_response")  # fmt: skip
        task = next(
            (t for t in items if isinstance(t, dict)
             and (t.get("external_id") == job.data["external_id"] or (job.data["task_id"] and t.get("id") == job.data["task_id"]))),
            None,
        )  # fmt: skip
        if task is None:
            misses = int(job.data.get("misses") or 0) + 1
            job.data["misses"] = misses
            if misses >= MAX_LOOKUP_MISSES:
                raise ProviderTaskError(
                    f"{self.name}: 作成したタスクが照会で見つかりません（{misses} 回）。Kling のコンソールで確認してください",
                    retryable=False, code="provider_bad_response", external_job_id=job.id,
                )  # fmt: skip
            return PollOutcome(state="queued", raw={"status": None})
        job.data["misses"] = 0
        status = str(task.get("status") or "").lower()
        raw = {"status": status, "id": task.get("id")}
        if status in ("submitted", "pending", "queued"):
            return PollOutcome(state="queued", raw=raw)
        if status in ("processing", "running"):
            return PollOutcome(state="running", raw=raw)
        if status in ("succeeded", "succeed"):
            outputs = [o for o in task.get("outputs") or [] if isinstance(o, dict)]
            video = next((o for o in outputs if o.get("type", "video") == "video" and o.get("url")), None)
            return PollOutcome(state="succeeded", video_url=video.get("url") if video else None,
                               raw={**raw, "duration": video.get("duration") if video else None})  # fmt: skip
        if status == "failed":
            raise self._task_failed(job, task.get("message"))
        raise ProviderError(f"{self.name}: 想定外のタスク状態です（{status or '空'}）", retryable=True,
                            code="provider_bad_response")  # fmt: skip

    def _poll_legacy(self, client: httpx.Client, job: SubmittedJob, model: ProviderModel) -> PollOutcome:
        response = self._request(client, "GET", f"{self.api_base}/v1/videos/{job.data['kind']}/{job.id}",
                                 what="進捗の確認", headers=self._headers(model))  # fmt: skip
        data = self._checked(response, "進捗の確認").get("data") or {}
        status = str(data.get("task_status") or "").lower()
        raw = {"status": status}
        if status == "submitted":
            return PollOutcome(state="queued", raw=raw)
        if status == "processing":
            return PollOutcome(state="running", raw=raw)
        if status in ("succeed", "succeeded"):
            videos = [v for v in (data.get("task_result") or {}).get("videos") or [] if isinstance(v, dict)]
            url = videos[0].get("url") if videos else None
            return PollOutcome(state="succeeded", video_url=url,
                               raw={**raw, "duration": videos[0].get("duration") if videos else None})  # fmt: skip
        if status == "failed":
            raise self._task_failed(job, data.get("task_status_msg"))
        raise ProviderError(f"{self.name}: 想定外のタスク状態です（{status or '空'}）", retryable=True,
                            code="provider_bad_response")  # fmt: skip

    def _task_failed(self, job: SubmittedJob, message: Any) -> ProviderTaskError:
        detail = " ".join(str(message or "").split())[:300]
        retryable = any(hint in detail.lower() for hint in _TRANSIENT_HINTS)
        return ProviderTaskError(
            f"{self.name}: 生成に失敗しました（{detail or '理由の記載なし'}）",
            retryable=retryable,
            code="provider_failed",
            external_job_id=job.id,
        )

