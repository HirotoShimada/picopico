"""OpenRouter video generation (docs/VIDEO_PROVIDERS.md §8.2–8.3).

Shapes come from the official SDK source (npm ``@openrouter/sdk`` 1.4.25,
``esm/funcs/videoGeneration*.js`` and ``esm/models/video*.js``; PyPI
``openrouter`` 1.3.43 agrees). **Nothing has been verified against the real
API** (openrouter.ai is unreachable from the dev container): every model is
``verified: False`` and the adapter is tested with respx only.

Flow:

1. ``POST {base}/videos`` (``Authorization: Bearer``) with ``{model, prompt,
   duration, aspect_ratio: "9:16", resolution?, generate_audio: false?, seed?,
   frame_images?}`` → 202 ``{id, polling_url, status}``. There is no
   negative-prompt field, so negatives are appended as "Avoid: …" (like Kling).
   The job id is reported via ``on_submitted`` at once.
2. ``GET {base}/videos/{id}`` until ``completed`` (``pending | in_progress |
   completed | failed | cancelled | expired``). ``polling_url`` is used only
   when it has the API's origin (the key never goes to another host).
3. ``GET {base}/videos/{id}/content?index=0`` with the key; redirects are
   followed without it. When that fails, the job's ``unsigned_urls`` are tried
   (https on public hosts only — SSRF guard on every hop). 409 = not ready yet
   (retried within the timeout), 410 = gone.
4. ``usage.cost`` (USD) of the finished job is the clip's cost; the price
   table is only the estimate (budget reservation). With BYOK
   (``is_byok``) the clip is never booked below the estimate, because
   ``usage.cost`` may then be OpenRouter's fee only.

There is no cancel endpoint (``supports_remote_cancel = False``): an
abandoned job may still run and be billed. The video request schema has no
``zdr`` / ``data_collection`` fields (its ``provider`` object only carries
per-vendor ``options``) and the docs state video generation is not eligible
for zero data retention, so no data-policy preferences are sent here (the
Claude route always sends them, see ``app/services/openrouter.py``).

Models come from ``GET {base}/videos/models`` (only when
``OPENROUTER_API_KEY`` is set), cached in memory and in
``WORK_DIR/cache/openrouter_video_models.json`` for
``OPENROUTER_MODELS_CACHE_TTL`` seconds; a built-in list (search-based,
unverified) stands in when the listing cannot be fetched. Models that
explicitly lack 9:16 and upscalers are left out; models whose aspect ratios
are unknown are listed with ``supports_vertical: False`` and a note.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx

from app.config import get_settings
from app.services.errors import ProviderError, ProviderNotConfigured
from app.services.openrouter import error_message, request_headers, same_origin
from app.services.providers.base import (
    ApiVideoProvider,
    CancelCheck,
    ClipRequest,
    ClipResult,
    PollConfig,
    PollOutcome,
    ProgressCallback,
    ProviderCancelled,
    ProviderModel,
    ProviderTaskError,
    SubmittedJob,
    _max_download_bytes,
    classify_http_status,
    download_file,
    read_image_base64,
    safe_path_id,
    snap_duration,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FALLBACK_MODELS",
    "STATIC_PRICES",
    "Catalogue",
    "OpenRouterProvider",
    "reset_model_cache",
    "sku_price_per_second",
]

# Date of the OpenRouter research in docs/VIDEO_PROVIDERS.md §8 (built-in list and static prices).
OPENROUTER_RESEARCH_AS_OF = "2026-10-12"
CACHE_FILE_NAME = "openrouter_video_models.json"
_CACHE_VERSION = 1
_LISTING_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_FAILURE_BACKOFF_SEC = 300.0  # after a failed listing fetch, use the cache/built-in list this long
_KEYLESS_RECHECK_SEC = 60.0  # without a key the built-in list / file cache is re-checked this often
_MAX_LISTING_ENTRIES = 500
_PROMPT_MAX_CHARS = 2000  # [C] no documented limit; sanitize_prompt keeps the positive part ≤ 1000
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # [C] encoded data-URI size; 413 is a typed error of POST /videos
_SEED_MODULUS = 2**31
_MAX_UNSIGNED_URLS = 3
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$")
_MAX_MODEL_ID_CHARS = 90
# Resolution to request for the base model, best first: 720p like the other adapters (the renderer
# scales to 1080x1920); an "@1080p" variant is added when the listing prices 1080p separately.
_RESOLUTION_PREFERENCE = ("720p", "768p", "1080p", "1K", "480p", "2K", "360p", "4K")
_VERTICAL_SIZE = {"360p": "360x640", "480p": "480x854", "720p": "720x1280", "768p": "768x1366",
                  "1080p": "1080x1920", "2K": "1440x2560", "4K": "2160x3840"}  # fmt: skip
_LOW_RESOLUTIONS = frozenset({"360p", "480p"})
_RESOLUTION_TOKENS = frozenset({"360p", "480p", "720p", "768p", "1080p", "1k", "2k", "4k"})
_SILENT_SKU = re.compile(r"(?:no|without|off|mute|muted)[-_]?audio|audio[-_]?(?:off|none|false)|silent|mute",
                         re.IGNORECASE)  # fmt: skip
_CONTENT_HINTS = ("moderation", "safety", "policy", "content", "inappropriate", "nsfw", "blocked", "flagged")
_TRANSIENT_HINTS = ("internal", "timeout", "timed out", "busy", "try again", "temporar", "unavailable",
                    "overload", "capacity")  # fmt: skip


# --- built-in catalogue (search-based, unverified: docs/VIDEO_PROVIDERS.md §8.3) -----------------------------
#
# Used only when GET /videos/models cannot be fetched (no key, network error). Aspect ratios,
# durations and audio flags follow the model pages seen as search snippets [B]/mirrors [C].

FALLBACK_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "google/veo-3.1-lite", "name": "Veo 3.1 Lite", "supported_durations": [4, 6, 8],
     "supported_aspect_ratios": ["16:9", "9:16"], "supported_resolutions": ["720p"], "generate_audio": True},
    {"id": "google/veo-3.1-fast", "name": "Veo 3.1 Fast", "supported_durations": [4, 6, 8],
     "supported_aspect_ratios": ["16:9", "9:16"], "supported_resolutions": ["720p"], "generate_audio": True},
    {"id": "kwaivgi/kling-v3.0-std", "name": "Kling v3.0 Standard", "supported_durations": list(range(3, 16)),
     "supported_aspect_ratios": ["16:9", "9:16", "1:1"], "supported_resolutions": ["720p"], "generate_audio": True},
    {"id": "kwaivgi/kling-video-o1", "name": "Kling Video O1", "supported_durations": [5, 10],
     "supported_aspect_ratios": ["16:9", "9:16", "1:1"], "supported_resolutions": ["720p"]},
    {"id": "minimax/hailuo-2.3", "name": "Hailuo 2.3", "supported_durations": [6, 10]},
    {"id": "alibaba/wan-2.7", "name": "Wan 2.7", "supported_durations": list(range(2, 11)),
     "supported_aspect_ratios": ["16:9", "9:16"], "supported_resolutions": ["720p"]},
)  # fmt: skip
# USD per second for a silent 720p clip [B]/[C] ("from" prices of the model pages; Kling v3.0 std without
# audio). Also the price of a live listing entry whose pricing_skus cannot be read.
STATIC_PRICES: dict[str, float] = {
    "google/veo-3.1-lite": 0.05,
    "google/veo-3.1-fast": 0.10,
    "kwaivgi/kling-v3.0-std": 0.084,
    "kwaivgi/kling-video-o1": 0.112,
    "minimax/hailuo-2.3": 0.0817,
    "alibaba/wan-2.7": 0.10,
}
_STATIC_DURATIONS: dict[str, list[float]] = {
    str(entry["id"]): [float(d) for d in entry["supported_durations"]] for entry in FALLBACK_MODELS
}


# --- small helpers ---------------------------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _str_list(value: Any) -> list[str] | None:
    """A list of strings, or ``None`` when the listing says nothing (null / missing / wrong type)."""
    if not isinstance(value, list):
        return None
    return [str(item) for item in value if isinstance(item, (str, int, float)) and not isinstance(item, bool)]


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _resolution_token(sku: str) -> str | None:
    for token in re.split(r"[-_:\s/]+", sku.lower()):
        if token in _RESOLUTION_TOKENS:
            return token
    return None


def sku_price_per_second(skus: Any, resolution: str | None) -> tuple[float | None, str | None, bool]:
    """``(USD per second, SKU used, resolution-specific?)`` from a VideoModel's ``pricing_skus`` [A].

    The key names are not documented beyond the example ``per-video-second`` /
    ``per-video-second-1080p`` [B], so (docs/VIDEO_PROVIDERS.md §8.3, [C]):
    per-second SKUs (key contains "second", value parses as a number) for the
    requested resolution win, else those naming no resolution; silent-audio
    SKUs are preferred (audio is always turned off when possible); among the
    rest the **highest** price is taken so the estimate is never too low.
    """
    if not isinstance(skus, Mapping):
        return None, None, False
    entries: list[tuple[str, float]] = []
    for key, value in skus.items():
        name = str(key)
        price = _num(value)
        if "second" in name.lower() and price is not None and price >= 0:
            entries.append((name, price))
    if not entries:
        return None, None, False
    wanted = resolution.lower() if resolution else None
    specific = [(k, p) for k, p in entries if wanted and _resolution_token(k) == wanted]
    generic = [(k, p) for k, p in entries if _resolution_token(k) is None]
    pool = specific or generic
    if not pool:
        return None, None, False
    silent = [(k, p) for k, p in pool if _SILENT_SKU.search(k)]
    key, price = max(silent or pool, key=lambda item: item[1])
    return price, key, bool(specific)


def _is_public_host(host: str) -> bool:
    """False for localhost names and for IP literals that are not global (SSRF guard).

    Besides canonical literals, the numeric forms the C resolver also accepts
    (``inet_aton``: ``2130706433``, ``0x7f000001``, ``127.1``, ``0xa9fea9fe`` …)
    are vetted, so they cannot pass as "DNS names". Names that resolve to
    private addresses through DNS are not caught here (documented limitation).
    """
    host = host.strip("[]").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
    except ValueError:
        try:
            packed = socket.inet_aton(host)
        except OSError:
            return True  # a DNS name (resolution happens in httpx)
        address = ipaddress.IPv4Address(packed)
    return address.is_global


# --- catalogue cache ----------------------------------------------------------------------------------------------


@dataclass
class Catalogue:
    """Raw ``VideoModel`` entries and where they came from."""

    entries: tuple[dict[str, Any], ...]
    source: str  # "live" (fetched now) | "cache" (disk) | "fallback" (built-in list)
    fetched_at: float | None  # epoch seconds of the listing (None for the built-in list)
    stale: bool = False  # older than the TTL (refetch impossible or failed)
    error: str | None = None  # last fetch error
    _models: list[ProviderModel] | None = field(default=None, repr=False)
    _excluded: int = 0


@dataclass
class _Held:
    catalogue: Catalogue
    created: float
    valid_until: float


class _CatalogueStore:
    """Process-wide cache (memory + one JSON file) of the listing, per API base.

    A fresh listing is reused for the TTL. Otherwise the file cache is tried,
    then a fetch (with a key); after a failed fetch the newest stale listing —
    or the built-in list — is served and the fetch is retried only after
    ``_FAILURE_BACKOFF_SEC``, so a dead network never stalls every request.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._memory: dict[str, _Held] = {}
        self._failed_at: dict[str, float] = {}
        self._errors: dict[str, str] = {}

    def reset(self, *, cache_path: Path | None = None) -> None:
        with self._lock:
            self._memory.clear()
            self._failed_at.clear()
            self._errors.clear()
            if cache_path is not None:
                cache_path.unlink(missing_ok=True)

    def _hold(self, base: str, catalogue: Catalogue, now: float, valid_until: float) -> Catalogue:
        self._memory[base] = _Held(catalogue, now, valid_until)
        return catalogue

    def get(
        self,
        base: str,
        *,
        now: float,
        ttl: float,
        cache_path: Path,
        fetch: Callable[[], list[dict[str, Any]]] | None,
        keyless: bool,
    ) -> Catalogue:
        """``fetch`` is None when there is no key (``keyless``) or the caller forbids network access."""
        with self._lock:
            held = self._memory.get(base)
            if held is not None and held.created <= now < held.valid_until:
                return held.catalogue
            previous = held.catalogue if held is not None and held.catalogue.fetched_at is not None else None
            disk = _load_cache_file(cache_path, base)
            if disk is not None and disk.fetched_at is not None and 0 <= now - disk.fetched_at < ttl:
                return self._hold(base, disk, now, disk.fetched_at + ttl)
            failed = self._failed_at.get(base)
            in_backoff = failed is not None and 0 <= now - failed < _FAILURE_BACKOFF_SEC
            if fetch is not None and not in_backoff:
                try:
                    entries = fetch()
                except Exception as exc:  # noqa: BLE001 - any failure → stale listing / built-in list
                    failed = now
                    self._failed_at[base] = now
                    self._errors[base] = f"{type(exc).__name__}: {exc}"[:300]
                    logger.warning("openrouter: could not fetch /videos/models (%s); using %s", self._errors[base],
                                   "the cached listing" if (previous or disk) else "the built-in list")  # fmt: skip
                else:
                    catalogue = Catalogue(tuple(entries), "live", now)
                    self._failed_at.pop(base, None)
                    self._errors.pop(base, None)
                    _save_cache_file(cache_path, base, catalogue)
                    return self._hold(base, catalogue, now, now + ttl)
            error = self._errors.get(base)
            newest = max((c for c in (previous, disk) if c is not None and c.fetched_at is not None),
                         key=lambda c: c.fetched_at or 0.0, default=None)  # fmt: skip
            if newest is not None:
                result = replace(newest, stale=True, error=error, _models=None)
            else:
                result = Catalogue(tuple(dict(entry) for entry in FALLBACK_MODELS), "fallback", None, error=error)
            if fetch is not None:  # a fetch failed or is backing off: retry when the backoff ends
                return self._hold(base, result, now, min(now + ttl, (failed or now) + _FAILURE_BACKOFF_SEC))
            if keyless:  # no key: nothing to fetch; look at the file cache again now and then
                return self._hold(base, result, now, now + min(ttl, _KEYLESS_RECHECK_SEC))
            return result  # network not allowed for this call only: do not pin the result


_STORE = _CatalogueStore()


def _cache_path() -> Path:
    return get_settings().WORK_DIR / "cache" / CACHE_FILE_NAME


def reset_model_cache(*, delete_file: bool = True) -> None:
    """Forget the cached listing (tests; ``delete_file`` also removes the JSON cache file)."""
    path: Path | None = None
    if delete_file:
        try:
            path = _cache_path()
        except Exception:  # noqa: BLE001 - settings unavailable: nothing on disk to remove
            path = None
    _STORE.reset(cache_path=path)


def _load_cache_file(path: Path, base: str) -> Catalogue | None:
    try:
        if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION or payload.get("api_base") != base:
        return None
    fetched_at = _num(payload.get("fetched_at"))
    data = payload.get("data")
    if fetched_at is None or not isinstance(data, list):
        return None
    entries = tuple(entry for entry in data[:_MAX_LISTING_ENTRIES] if isinstance(entry, dict))
    return Catalogue(entries, "cache", fetched_at)


def _save_cache_file(path: Path, base: str, catalogue: Catalogue) -> None:
    payload = {"version": _CACHE_VERSION, "api_base": base, "fetched_at": catalogue.fetched_at,
               "data": list(catalogue.entries)}  # fmt: skip
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("openrouter: could not write the model cache %s", path, exc_info=True)


# --- model mapping ------------------------------------------------------------------------------------------------


def _pick_resolution(resolutions: list[str] | None) -> str | None:
    if not resolutions:
        return None
    for candidate in _RESOLUTION_PREFERENCE:
        if candidate in resolutions:
            return candidate
    return resolutions[0]


def _vertical_size(resolution: str | None) -> str:
    if resolution is None:
        return "不明（提供元の既定）"
    return _VERTICAL_SIZE.get(resolution, f"{resolution}（9:16）")


def _as_of(catalogue: Catalogue) -> str:
    if catalogue.fetched_at is None:
        return OPENROUTER_RESEARCH_AS_OF
    return datetime.fromtimestamp(catalogue.fetched_at, tz=UTC).strftime("%Y-%m-%d")


def _models_from_entry(entry: Mapping[str, Any], catalogue: Catalogue) -> tuple[list[ProviderModel], str | None]:
    """ProviderModels for one listing entry (base + optional ``@1080p``), or ``([], why it is left out)``."""
    model_id = str(entry.get("id") or "").strip()
    # ≤ _MAX_MODEL_ID_CHARS so that "<id>@1080p" fits GenerationSettings.model / Clip.model (100 chars).
    if not _MODEL_ID.match(model_id) or "@" in model_id or len(model_id) > _MAX_MODEL_ID_CHARS:
        return [], "invalid id"
    if entry.get("upscale_factor") or entry.get("creativity"):
        return [], "upscaler"
    ratios = _str_list(entry.get("supported_aspect_ratios"))
    if ratios is not None and "9:16" not in ratios:
        return [], "no 9:16"
    listed_durations = entry.get("supported_durations")
    raw_durations: list[Any] = listed_durations if isinstance(listed_durations, list) else []
    durations = sorted({d for d in (_num(v) for v in raw_durations) if d is not None and 0 < d <= 60})
    durations_known = bool(durations)
    if not durations:
        durations = list(_STATIC_DURATIONS.get(model_id, [5.0]))
    resolutions = _str_list(entry.get("supported_resolutions"))
    frames = _str_list(entry.get("supported_frame_images")) or []
    audio = _flag(entry.get("generate_audio"))
    seed = _flag(entry.get("seed"))
    name = str(entry.get("name") or model_id).strip()[:80] or model_id
    base_resolution = _pick_resolution(resolutions)
    skus = entry.get("pricing_skus")
    variants = [base_resolution]
    if resolutions and "1080p" in resolutions and base_resolution != "1080p":
        price, _, specific = sku_price_per_second(skus, "1080p")
        if price is not None and specific:  # only when 1080p has its own price (never guess it low)
            variants.append("1080p")
    as_of = _as_of(catalogue)
    models: list[ProviderModel] = []
    for resolution in variants:
        price, sku, _ = sku_price_per_second(skus, resolution)
        if price is not None:
            price_source = "listing"
            price_text = f"OpenRouter のモデル一覧の料金（{sku}: ${price:g}/秒、{as_of} 取得）"
        elif model_id in STATIC_PRICES and resolution in (None, base_resolution):
            price = STATIC_PRICES[model_id]
            price_source = "table"
            price_text = (
                f"${price:g}/秒（{OPENROUTER_RESEARCH_AS_OF} 時点のモデルページの調査値。"
                + ("モデル一覧を取得できないため組み込みの候補を表示しています）" if catalogue.source == "fallback"
                   else "モデル一覧の料金を読み取れなかったため）")
            )  # fmt: skip
        else:
            price_source = "unknown"
            price_text = "料金を確認できません（モデル一覧に読み取れる料金がなく、調査値もありません）。予算を確保できないため使用できません"
        notes = [price_text + "。未検証。実費は完了時の usage.cost で記録します" if price is not None else price_text]
        if audio is True:
            notes.append("音声は使わないため無音（generate_audio: false）で生成します")
        if ratios is None:
            notes.append("9:16 に対応しているかはモデル一覧に記載がありません（未対応ならリクエストの時点で拒否されます）")
        if not durations_known:
            notes.append(f"対応する秒数がモデル一覧にないため {', '.join(f'{d:g}' for d in durations)} 秒で要求します（未確認）")
        if resolution is None:
            notes.append("解像度はモデル一覧に記載がないため、提供元の既定です")
        elif resolution in _LOW_RESOLUTIONS:
            notes.append(f"出力が {resolution} のため、1080×1920 に拡大すると画質が下がります")
        model = ProviderModel(
            id=model_id if resolution == base_resolution else f"{model_id}@{resolution}",
            name=name if resolution == base_resolution else f"{name}（{resolution}）",
            clip_durations=durations,
            max_clip_duration=max(durations),
            resolution=_vertical_size(resolution),
            supports_vertical=ratios is not None,
            price_per_second_usd=float(price) if price is not None else 0.0,
            price_note="。".join(notes),
            verified=False,
            default_duration=snap_duration(durations, max(durations), 5.0),
            text_to_video=True,
            image_to_video="first_frame" in frames,
            source_url=f"https://openrouter.ai/{model_id}",
            as_of=as_of if price_source == "listing" else OPENROUTER_RESEARCH_AS_OF,
            params={
                "api_model": model_id,
                "resolution": resolution,
                "aspect_ratios": ratios,
                "generate_audio": audio,
                "seed": seed,
                "frame_images": frames,
                "price_source": price_source,
                "price_sku": sku,
                "catalogue": catalogue.source,
            },
            price_known=price is not None,
        )
        models.append(model)
    return models, None


def _parse_catalogue(catalogue: Catalogue) -> list[ProviderModel]:
    if catalogue._models is None:
        models: list[ProviderModel] = []
        seen: set[str] = set()
        excluded = 0
        for entry in catalogue.entries:
            entry_models, reason = _models_from_entry(entry, catalogue)
            if reason is not None:
                excluded += 1
                logger.debug("openrouter: model %r left out (%s)", entry.get("id"), reason)
            for model in entry_models:
                if model.id not in seen:
                    seen.add(model.id)
                    models.append(model)
        catalogue._models = models
        catalogue._excluded = excluded
    return catalogue._models


# --- provider -------------------------------------------------------------------------------------------------------


class OpenRouterProvider(ApiVideoProvider):
    id = "openrouter"
    name = "OpenRouter"
    commercial_use_note = (
        "OpenRouter は複数社の動画モデル（Google Veo・Kling・MiniMax Hailuo・Alibaba Wan など）を 1 本のキーで"
        "中継するサービスです。生成物の権利・商用利用は OpenRouter の利用規約と、実際に使うモデルそれぞれの規約の"
        "両方に従います（規約本文は未確認。OpenRouter の規約では出力の権利はモデルごとの規約に従うとされています）。"
        "Veo の出力には不可視の SynthID 透かしが入ります。プロンプト（英語のシーン説明）は OpenRouter と各提供元に"
        "送られ、動画生成はゼロデータ保持（ZDR）の対象外です。キャンセル API がないため、中断したジョブも課金される"
        "場合があります。モデル・料金はすべて実キーで未検証です。"
    )
    poll_config: ClassVar[PollConfig] = PollConfig(initial=5.0, maximum=30.0, factor=1.5, jitter=0.2, minimum=3.0)
    supports_remote_cancel: ClassVar[bool] = False  # the SDK has no cancel/DELETE operation [A]
    supported_aspects: ClassVar[tuple[str, ...]] = ("9:16", "16:9", "1:1")
    # The model list comes from the API (cached); cost.get_pricing reads it live.
    dynamic_catalogue: ClassVar[bool] = True

    def __init__(
        self, *, api_base: str | None = None, wall_clock: Callable[[], float] | None = None, **kwargs: Any
    ):
        super().__init__(**kwargs)
        self._api_base_override = api_base
        self._wall_clock = wall_clock or time.time

    @property
    def api_base(self) -> str:
        return (self._api_base_override or get_settings().OPENROUTER_API_BASE).rstrip("/")

    # -- catalogue ----------------------------------------------------------------------------------------

    def catalogue(self, *, allow_fetch: bool = True) -> Catalogue:
        """The model listing: memory/disk cache within the TTL, else a fetch (key set), else stale cache / built-in."""
        settings = get_settings()
        keyless = not settings.OPENROUTER_API_KEY
        fetch = self._fetch_listing if allow_fetch and not keyless else None
        return _STORE.get(self.api_base, now=self._wall_clock(), ttl=float(settings.OPENROUTER_MODELS_CACHE_TTL),
                          cache_path=_cache_path(), fetch=fetch, keyless=keyless)  # fmt: skip

    def _fetch_listing(self) -> list[dict[str, Any]]:
        """``GET /videos/models`` → ``{"data": [VideoModel]}`` [A] (videoGenerationListVideosModels.js)."""
        with httpx.Client(timeout=_LISTING_TIMEOUT, transport=self._transport, follow_redirects=False) as client:
            response = client.get(f"{self.api_base}/videos/models", headers=self._headers(required=False))
        if not response.is_success:
            raise ProviderError(f"HTTP {response.status_code} {error_message(response)}".strip(), retryable=True)
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ProviderError("unexpected listing shape", retryable=True)
        entries = [entry for entry in data[:_MAX_LISTING_ENTRIES] if isinstance(entry, dict)]
        # A listing this app cannot use at all (empty, or no entry with a valid id and 9:16) is treated
        # like a failed fetch: the previous listing (or the built-in list) stays, nothing is cached for
        # the TTL, and the fetch is retried after the backoff.
        if not _parse_catalogue(Catalogue(tuple(entries), "live", None)):
            raise ProviderError(f"the listing has no usable model ({len(entries)} entries)", retryable=True)
        return entries

    def _ordered(self, models: Sequence[ProviderModel]) -> list[ProviderModel]:
        wanted = get_settings().OPENROUTER_VIDEO_DEFAULT_MODEL
        return sorted(
            (replace(m) for m in models),
            key=lambda m: (m.id != wanted, not m.price_known, m.price_per_second_usd, m.id),
        )

    def models(self) -> list[ProviderModel]:
        return self._ordered(_parse_catalogue(self.catalogue()))

    def cached_models(self) -> list[ProviderModel]:
        """Like :meth:`models` but never fetches (import-time pricing snapshot)."""
        return self._ordered(_parse_catalogue(self.catalogue(allow_fetch=False)))

    def default_model(self) -> str:
        models = self.models()
        wanted = get_settings().OPENROUTER_VIDEO_DEFAULT_MODEL
        for model in models:
            if model.id == wanted and model.price_known:
                return model.id
        usable = [m for m in models if m.price_known and m.text_to_video]
        if usable:
            return usable[0].id
        return models[0].id if models else wanted

    def info(self) -> dict[str, Any]:
        info = super().info()
        catalogue = self.catalogue()
        _parse_catalogue(catalogue)
        fetched = (datetime.fromtimestamp(catalogue.fetched_at, tz=UTC).isoformat()
                   if catalogue.fetched_at is not None else None)  # fmt: skip
        if catalogue.source == "fallback":
            note = (
                "モデル一覧（GET /videos/models）を取得できないため、組み込みの候補（検索による調査値・未検証）を表示しています。"
                if get_settings().OPENROUTER_API_KEY
                else "OPENROUTER_API_KEY が未設定のため、組み込みの候補（検索による調査値・未検証）を表示しています。"
            )
        else:
            note = f"OpenRouter のモデル一覧（{fetched[:10] if fetched else '不明'} 取得）より。"
            if catalogue.stale:
                note += "一覧を更新できなかったため、最新ではない可能性があります。"
        if catalogue._excluded:
            note += f"9:16 に対応していないモデルなど {catalogue._excluded} 件は表示していません。"
        info.update({"models_source": catalogue.source, "models_fetched_at": fetched, "models_note": note})
        return info

    # -- configuration --------------------------------------------------------------------------------------

    def is_configured(self) -> tuple[bool, str | None]:
        if get_settings().OPENROUTER_API_KEY:
            return True, None
        return False, "OPENROUTER_API_KEY が未設定です。backend/.env に設定すると利用できます"

    def _headers(self, *, required: bool = True, accept: str = "application/json") -> dict[str, str]:
        settings = get_settings()
        if required and not settings.OPENROUTER_API_KEY:
            raise ProviderError("OPENROUTER_API_KEY が未設定です", retryable=False, code="provider_auth_failed")
        return request_headers(settings, accept=accept)

    def _is_api_url(self, url: str) -> bool:
        return same_origin(url, self.api_base)

    def _allowed_download_url(self, url: str) -> bool:
        """SSRF guard for every download hop: the API origin, or https on a public host."""
        if self._is_api_url(url):
            return True
        parts = urlsplit(url)
        return parts.scheme == "https" and _is_public_host(parts.hostname or "")

    def _download_headers(self, url: str) -> dict[str, str]:
        # The key goes only to the API origin (download_file drops it on redirects to other hosts too).
        return self._headers(accept="video/mp4") if self._is_api_url(url) else {}

    # -- errors --------------------------------------------------------------------------------------------------

    def _http_error(self, response: httpx.Response, what: str) -> ProviderError:
        status = response.status_code
        detail = error_message(response)
        suffix = f"、HTTP {status}{': ' + detail if detail else ''}）"
        if status == 402:
            return ProviderError(
                f"{self.name}: {what}に失敗しました（OpenRouter のクレジットが不足しています。"
                f"OpenRouter のアカウントでクレジットを追加してください{suffix}",
                retryable=False, code="provider_payment_required",
            )  # fmt: skip
        if status == 401:
            return ProviderError(f"{self.name}: {what}に失敗しました（OPENROUTER_API_KEY が無効です{suffix}",
                                 retryable=False, code="provider_auth_failed")  # fmt: skip
        if status == 403:
            return ProviderError(
                f"{self.name}: {what}に失敗しました（モデレーション・ガードレール、またはアカウントの権限により"
                f"拒否されました{suffix}",
                retryable=False, code="provider_rejected",
            )  # fmt: skip
        if status == 413:
            return ProviderError(f"{self.name}: {what}に失敗しました（リクエストが大きすぎます。入力画像を小さくしてください{suffix}",
                                 retryable=False, code="provider_bad_input")  # fmt: skip
        retryable, code, summary = classify_http_status(status)
        return ProviderError(f"{self.name}: {what}に失敗しました（{summary}{suffix}", retryable=retryable, code=code)

    # -- requests ------------------------------------------------------------------------------------------------

    @staticmethod
    def _prompt_with_negative(req: ClipRequest) -> str:
        """No negative-prompt field [A]: negatives are appended as "Avoid: …" (the positive part stays whole)."""
        prompt = req.prompt.strip()
        negative = req.negative_prompt.strip().rstrip(".")
        if negative:
            room = _PROMPT_MAX_CHARS - len(prompt) - len(" Avoid: .")
            if room > 20:
                if len(negative) > room:
                    negative = negative[:room].rsplit(",", 1)[0].strip()
                prompt = f"{prompt} Avoid: {negative}."
        return prompt[:_PROMPT_MAX_CHARS]

    def build_request(self, req: ClipRequest, model: ProviderModel, duration: float) -> dict[str, Any]:
        """``POST /videos`` body [A] (videogenerationrequest.js outbound, snake_case wire names).

        Sent: ``model``, ``prompt``, ``duration`` (int), ``aspect_ratio``, ``resolution`` (when the listing
        names one), ``generate_audio: false`` (when the model generates audio: it is discarded anyway),
        ``seed`` (only when the listing says the model supports it), ``frame_images`` (first frame, as a
        data URI — whether data URIs are accepted is unverified). Never sent: ``size`` (interchangeable
        with resolution + aspect_ratio), ``provider`` (only per-vendor passthrough; no zdr/data_collection
        on video), ``callback_url`` (no public URL; polling is used), ``user``/``session_id``.
        """
        body: dict[str, Any] = {
            "model": model.params.get("api_model") or model.id,
            "prompt": self._prompt_with_negative(req),
            "duration": round(duration),
            "aspect_ratio": req.aspect_ratio,
        }
        if model.params.get("resolution"):
            body["resolution"] = model.params["resolution"]
        if model.params.get("generate_audio") is True:
            body["generate_audio"] = False
        if model.params.get("seed") is True and req.seed is not None:
            body["seed"] = int(req.seed) % _SEED_MODULUS
        if req.image_path:
            data, mime = read_image_base64(req.image_path, max_bytes=_MAX_IMAGE_BYTES,
                                           allowed=(".png", ".jpg", ".jpeg", ".webp"))  # fmt: skip
            body["frame_images"] = [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}, "frame_type": "first_frame"}
            ]
        return body

    def _prepare(self, req: ClipRequest) -> tuple[ProviderModel, float]:
        model, duration = super()._prepare(req)
        ratios = model.params.get("aspect_ratios")
        if isinstance(ratios, list) and req.aspect_ratio not in ratios:
            raise ProviderError(f"{model.name} はアスペクト比 {req.aspect_ratio} に対応していません", retryable=False,
                                code="provider_bad_input")  # fmt: skip
        return model, duration

    def _prepare_resume(self, req: ClipRequest, *, submitted_usd: float | None = None) -> tuple[ProviderModel, float]:
        """Follow an existing job whatever the listing says now (it only governs new submissions).

        The listed model is used while it is listed with a price. When it left the listing, lost its
        readable price or no longer qualifies (e.g. 9:16 dropped), a minimal unverified model is built
        from ``req.model`` instead — polling and downloading need only the job id — priced with
        ``submitted_usd`` (the job's estimate at submission), else the static research price. No
        submission checks apply: the job runs and is billed whether or not it is followed.
        """
        configured, reason = self.is_configured()
        if not configured:
            raise ProviderNotConfigured(reason or f"{self.name} は設定されていません")
        try:
            listed: ProviderModel | None = self.get_model(req.model)
        except ProviderError:
            listed = None
        if listed is not None and listed.price_known:
            return listed, listed.snap_duration(req.duration)
        model = self._resume_model(req, listed, submitted_usd)
        return model, model.snap_duration(req.duration)

    def _resume_model(
        self, req: ClipRequest, listed: ProviderModel | None, submitted_usd: float | None
    ) -> ProviderModel:
        model_id = req.model or (listed.id if listed is not None else "")
        api_model, _, variant = model_id.partition("@")
        duration = snap_duration([], 60.0, req.duration)
        submitted = _num(submitted_usd)
        if submitted is not None and submitted >= 0 and duration > 0:
            price: float | None = submitted / duration
            note = "ジョブ投入時の見積もり額"
        elif api_model in STATIC_PRICES:
            price = STATIC_PRICES[api_model]
            note = f"{OPENROUTER_RESEARCH_AS_OF} 時点の調査値"
        else:
            price = None
            note = "料金不明"
        logger.warning("openrouter: resuming a job of %s, which the listing no longer offers with a price (%s)",
                       model_id, note)  # fmt: skip
        return ProviderModel(
            id=model_id,
            name=listed.name if listed is not None else api_model,
            clip_durations=[],
            max_clip_duration=max(duration, 1.0),
            resolution=listed.resolution if listed is not None else _vertical_size(variant or None),
            supports_vertical=True,
            price_per_second_usd=price if price is not None else 0.0,
            price_note=(
                "モデル一覧にない（または料金を読み取れない）モデルの実行中のジョブです。"
                f"費用は usage.cost、なければ{note}で記録します。未検証"
            ),
            verified=False,
            default_duration=duration,
            source_url=f"https://openrouter.ai/{api_model}",
            as_of=OPENROUTER_RESEARCH_AS_OF,
            params={
                "api_model": api_model,
                "resolution": variant or None,
                "price_source": "resume",
                "price_basis": note,
                "catalogue": "resume",
            },
            price_known=price is not None,
        )

    def _submit(self, client: httpx.Client, req: ClipRequest, model: ProviderModel, duration: float) -> SubmittedJob:
        """``POST /videos``. It has no idempotency key [A] (and the SDK's own retry would resend it), so
        only failures that prove no job exists are retryable: connect-phase errors and 408/425/429/503/529.
        A read/write timeout, a dropped connection, 500/502/504/524 or an accepted answer without a usable
        job id may hide a created — billed — job: :class:`ProviderSubmitAmbiguous` (never resent; the
        pipeline books the estimate as presumed billed)."""
        what = "生成リクエスト"
        body = self.build_request(req, model, duration)
        headers = self._headers()
        response = self._submit_request(client, "POST", f"{self.api_base}/videos", what=what, json=body,
                                        headers=headers)  # fmt: skip
        try:
            data = self._json(response, what)
            job_id = safe_path_id(data.get("id"), provider_name=self.name, what="ジョブ ID")
        except ProviderError as exc:  # accepted (2xx), so a job may exist, but it cannot be identified
            raise self._submit_ambiguous(what, f"HTTP {response.status_code} の応答を読み取れませんでした") from exc
        polling_url = data.get("polling_url") if isinstance(data.get("polling_url"), str) else None
        if polling_url and not self._is_api_url(polling_url):
            logger.warning("openrouter: ignoring polling_url on another origin for job %s", job_id)
            polling_url = None
        return SubmittedJob(id=job_id, raw={"id": job_id, "status": data.get("status")},
                            data={"polling_url": polling_url})  # fmt: skip

    def _poll_url(self, job: SubmittedJob) -> str:
        url = job.data.get("polling_url")
        if isinstance(url, str) and self._is_api_url(url):
            return url
        return f"{self.api_base}/videos/{quote(job.id, safe='')}"

    def _content_url(self, job: SubmittedJob) -> str:
        return f"{self.api_base}/videos/{quote(job.id, safe='')}/content?index=0"

    def _poll(self, client: httpx.Client, job: SubmittedJob, model: ProviderModel) -> PollOutcome:
        response = self._request(client, "GET", self._poll_url(job), what="進捗の確認", headers=self._headers())
        data = self._json(response, "進捗の確認")
        status = str(data.get("status") or "").lower()
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        cost = _num(usage.get("cost"))
        cost = max(0.0, cost) if cost is not None else None
        byok = _flag(usage.get("is_byok"))
        raw = {"status": status, "cost": cost, "is_byok": byok, "generation_id": data.get("generation_id")}
        if cost is not None:  # kept on the job: a later re-read (output refresh) may omit usage
            job.data["reported_cost"] = cost
            job.data["is_byok"] = byok
        if status in ("pending", "queued"):
            return PollOutcome(state="queued", raw=raw)
        if status in ("in_progress", "processing", "running"):
            return PollOutcome(state="running", raw=raw)
        if status == "completed":
            urls = [u for u in data.get("unsigned_urls") or [] if isinstance(u, str) and self._allowed_download_url(u)]
            job.data["unsigned_urls"] = urls[:_MAX_UNSIGNED_URLS]
            return PollOutcome(state="succeeded", video_url=self._content_url(job), cost_usd=cost,
                               raw={**raw, "unsigned_urls": len(urls)})  # fmt: skip
        error = " ".join(str(data.get("error") or "").split())[:300]
        if status == "failed":
            lowered = error.lower()
            if any(hint in lowered for hint in _CONTENT_HINTS):
                raise ProviderTaskError(
                    f"{self.name}: 生成が拒否されました（{error}）。プロンプトを見直してください",
                    retryable=False, code="provider_content_filtered", external_job_id=job.id,
                    failure_code="failed", cost_usd=cost,
                )  # fmt: skip
            raise ProviderTaskError(
                f"{self.name}: 生成に失敗しました（{error or '理由の記載なし'}）",
                retryable=any(hint in lowered for hint in _TRANSIENT_HINTS), code="provider_failed",
                external_job_id=job.id, failure_code="failed", cost_usd=cost,
            )  # fmt: skip
        if status == "cancelled":
            raise ProviderTaskError(
                f"{self.name}: OpenRouter 側でジョブがキャンセルされました{f'（{error}）' if error else ''}",
                retryable=False, code="provider_failed", external_job_id=job.id, failure_code="cancelled",
                cost_usd=cost,
            )  # fmt: skip
        if status == "expired":
            # The job's outputs expired (it had finished, so it is presumed billed unless usage.cost says otherwise).
            raise ProviderTaskError(
                f"{self.name}: 生成した動画の保存期限が切れたため取得できません{f'（{error}）' if error else ''}",
                retryable=False, code="provider_bad_output", external_job_id=job.id, failure_code="expired",
                cost_usd=cost,
            )  # fmt: skip
        raise ProviderError(f"{self.name}: 想定外のジョブ状態です（{status or '空'}）", retryable=True,
                            code="provider_bad_response")  # fmt: skip

    def _download_output(
        self,
        dl: httpx.Client,
        job: SubmittedJob,
        outcome: PollOutcome,
        out_path: str,
        *,
        cancel_check: CancelCheck | None,
    ) -> int:
        """``/content`` first (with the key), then the https ``unsigned_urls`` (without it)."""
        assert outcome.video_url is not None
        candidates = [outcome.video_url]
        candidates += [u for u in job.data.get("unsigned_urls") or [] if u not in candidates]
        first_error: ProviderTaskError | None = None
        for url in candidates:
            try:
                return download_file(
                    dl, url, out_path, provider_name=self.name, headers=self._download_headers(url),
                    max_bytes=_max_download_bytes(), cancel_check=cancel_check, external_job_id=job.id,
                    allow_url=self._allowed_download_url,
                )  # fmt: skip
            except ProviderCancelled:
                raise
            except ProviderTaskError as exc:
                logger.warning("openrouter: downloading job %s from %s failed: %s", job.id,
                               "the content endpoint" if url == outcome.video_url else "an unsigned URL", exc)  # fmt: skip
                if first_error is None:
                    first_error = exc
        assert first_error is not None
        if first_error.http_status == 409:  # [C] the content is not ready yet: retried within the timeout
            raise ProviderTaskError(
                f"{self.name}: 生成した動画の準備がまだ整っていません（HTTP 409）",
                retryable=True, code="provider_not_ready", external_job_id=job.id, http_status=409,
            )  # fmt: skip
        if first_error.http_status == 410:  # [C] gone
            raise ProviderTaskError(
                f"{self.name}: 生成した動画はすでに削除されています（HTTP 410）",
                retryable=False, code="provider_bad_output", external_job_id=job.id, http_status=410,
            )  # fmt: skip
        raise first_error

    def _complete(
        self,
        client: httpx.Client,
        job: SubmittedJob,
        model: ProviderModel,
        out_path: str,
        *,
        duration: float,
        deadline: float,
        timeout: float,
        on_progress: ProgressCallback | None,
        cancel_check: CancelCheck | None,
    ) -> ClipResult:
        result = super()._complete(
            client, job, model, out_path, duration=duration, deadline=deadline, timeout=timeout,
            on_progress=on_progress, cancel_check=cancel_check,
        )  # fmt: skip
        # The cost OpenRouter reported for this job (any poll, also before an output refresh without usage).
        reported = _num(job.data.get("reported_cost"))
        basis = model.params.get("price_basis") or "料金表の単価 × 秒数"
        if reported is None:
            source, note = "estimate", f"OpenRouter が費用を返さなかったため推定額（{basis}）"
        elif job.data.get("is_byok") is True:
            # BYOK [B]: usage.cost may be OpenRouter's fee only while the vendor bills the user's own key.
            # Never book less than the estimate, so the batch budget keeps counting the real spend.
            estimate = model.cost_for(duration)
            if estimate > result.cost_usd:
                result = replace(result, cost_usd=estimate)
                source = "estimate"
                note = (f"BYOK のため推定額（{basis}）。OpenRouter の usage.cost（${reported:g}）は手数料のみで、"
                        "提供元の請求を含まない可能性があります")  # fmt: skip
            else:
                source, note = "usage.cost", "OpenRouter の実費（usage.cost）・BYOK（提供元の請求は含まれない可能性あり）"
        else:
            source, note = "usage.cost", "OpenRouter の実費（usage.cost）"
        result.raw["cost_source"] = source
        result.raw["cost_note"] = note
        return result
