"""Pricing table and batch cost estimate (ARCHITECTURE.md §6.5).

``PRICING`` is derived from the providers' ``ProviderModel`` entries
(``app/services/providers``), which are the single source of truth for prices
(researched in docs/VIDEO_PROVIDERS.md; all hosted-API prices are unverified
until a real key test passes). Providers with a ``dynamic_catalogue``
(OpenRouter: models and prices from its ``/videos/models`` listing, cached)
are priced live by :func:`get_pricing`; ``PRICING`` only holds an offline
snapshot of them. Models without a known price (``price_known=False``) are
left out of the comparison and cannot be selected (``check_model``).

Cost model: ``new clips x max(clip seconds x $/s, minimum per clip)`` (+ the
storyboard LLM call; the minimum is OpenRouter's ``minimum_cents_per_generation``,
0 elsewhere). The pipeline only generates text-to-video, so per-image input
charges (image-to-video) are not part of these estimates.
``naive_total_usd`` prices the same batch with one freshly generated clip per
shot (no reuse inside the batch, no cache), so ``savings_usd`` is what reuse
and the clip cache save. Both totals include ``llm_usd``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from app.services.errors import ServiceError
from app.services.providers.base import ProviderModel, VideoProvider, snap_duration
from app.services.providers.registry import get_provider, list_providers

__all__ = [
    "PRICING",
    "TARGET_CLIP_SEC",
    "ModelPricing",
    "build_pricing",
    "clip_cost_usd",
    "estimate",
    "get_pricing",
]

# Clip length the pipeline asks providers for (each model rounds it up to a
# supported duration, see ProviderModel.default_duration).
TARGET_CLIP_SEC = 5.0


@dataclass(frozen=True)
class ModelPricing:
    provider: str
    model: str
    name: str
    price_per_second_usd: float
    clip_durations: tuple[float, ...]
    default_duration: float
    max_clip_duration: float
    source_url: str
    as_of: str
    verified: bool
    text_to_video: bool
    price_note: str
    price_known: bool = True
    min_clip_usd: float = 0.0  # minimum per generated clip (0 = none)

    @classmethod
    def from_model(cls, provider_id: str, model: ProviderModel) -> ModelPricing:
        return cls(
            provider=provider_id,
            model=model.id,
            name=model.name,
            price_per_second_usd=float(model.price_per_second_usd),
            clip_durations=tuple(float(d) for d in model.clip_durations),
            default_duration=float(model.default_duration),
            max_clip_duration=float(model.max_clip_duration),
            source_url=model.source_url,
            as_of=model.as_of,
            verified=model.verified,
            text_to_video=model.text_to_video,
            price_note=model.price_note,
            price_known=model.price_known,
            min_clip_usd=float(model.min_clip_usd),
        )

    def clip_duration_for(self, wanted: float) -> float:
        return snap_duration(self.clip_durations, self.max_clip_duration, wanted)

    def clip_usd(self, seconds: float) -> float:
        """One generated clip, unrounded: ``max(seconds x $/s, min_clip_usd)``."""
        return _per_clip_usd(self.price_per_second_usd, self.min_clip_usd, seconds)

    def clip_cost_usd(self, seconds: float) -> float:
        return _usd(self.clip_usd(seconds))

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["clip_durations"] = list(self.clip_durations)
        return data


def _offline_models(provider: VideoProvider) -> list[ProviderModel]:
    """The provider's models without network access (dynamic catalogues use their cache / built-in list)."""
    cached = getattr(provider, "cached_models", None)
    return list(cached()) if callable(cached) else provider.models()


def build_pricing(providers: Sequence[VideoProvider] | None = None) -> dict[str, dict[str, ModelPricing]]:
    """``{provider_id: {model_id: ModelPricing}}`` from the provider catalogue (never fetches)."""
    table: dict[str, dict[str, ModelPricing]] = {}
    for provider in providers if providers is not None else list_providers():
        table[provider.id] = {m.id: ModelPricing.from_model(provider.id, m) for m in _offline_models(provider)}
    return table


PRICING: dict[str, dict[str, ModelPricing]] = build_pricing()


def get_pricing(provider: str, model: str | None = None) -> ModelPricing:
    """Pricing for ``provider``/``model`` (the provider's default model when ``model`` is empty)."""
    models = PRICING.get(provider)
    if models is None:
        raise ServiceError(f"不明な動画生成プロバイダです: {provider}", status_code=422, code="provider_unknown")
    source = get_provider(provider)
    if getattr(source, "dynamic_catalogue", False):
        models = {m.id: ModelPricing.from_model(provider, m) for m in source.models()}
    model_id = model or source.default_model()
    pricing = models.get(model_id)
    if pricing is None:
        raise ServiceError(
            f"{provider} に「{model_id}」というモデルはありません", status_code=422, code="provider_unknown_model"
        )
    return pricing


def clip_cost_usd(provider: str, model: str | None, seconds: float) -> float:
    return get_pricing(provider, model).clip_cost_usd(seconds)


def _usd(value: float) -> float:
    return round(value + 0.0, 4)


def _per_clip_usd(price_per_second: float, min_clip_usd: float, seconds: float) -> float:
    return max(float(price_per_second) * max(0.0, float(seconds)), float(min_clip_usd))


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def estimate(
    settings: Mapping[str, Any],
    plan_stats: Mapping[str, Any],
    provider: str,
    model: str | None,
    *,
    cached_clips: int,
    llm_estimate_usd: float,
    budget_limit_usd: float | None,
) -> dict[str, Any]:
    """Estimate document (§6.5) for a planned batch.

    ``plan_stats`` is ``BatchPlan.stats`` (§6.4; ``clip_duration`` is read when
    present, else the model's default duration). ``cached_clips`` = planned
    clips served from the cache (free). ``budget_limit_usd`` is the resolved
    cap (``None`` falls back to ``settings["budget_limit_usd"]``; still
    ``None`` = no cap).
    """
    pricing = get_pricing(provider, model)
    source = get_provider(provider)
    usable, unusable_reason = source.check_model(pricing.model)

    videos = _int(plan_stats.get("videos"), _int(settings.get("count")))
    shots_total = _int(plan_stats.get("shots_total"))
    unique_clips = _int(plan_stats.get("unique_clips"))
    new_clips = 0 if source.kind == "library" else _int(plan_stats.get("new_clips"))
    cached = _int(cached_clips)
    clip_duration = _float(plan_stats.get("clip_duration")) or pricing.default_duration
    pps = pricing.price_per_second_usd
    per_clip = pricing.clip_usd(clip_duration)
    minimum_applies = per_clip > pps * clip_duration + 1e-12
    llm_usd = max(0.0, _float(llm_estimate_usd) or 0.0)

    generation_usd = new_clips * per_clip
    total_usd = generation_usd + llm_usd
    naive_generation_usd = 0.0 if source.kind == "library" else shots_total * per_clip
    naive_total_usd = naive_generation_usd + llm_usd
    savings_usd = max(0.0, naive_total_usd - total_usd)

    limit = budget_limit_usd if budget_limit_usd is not None else _float(settings.get("budget_limit_usd"))
    within_budget = limit is None or total_usd <= limit + 1e-9
    variety_score = _float(plan_stats.get("variety_score"))
    variety_warning = plan_stats.get("variety_warning") or None

    comparison = _comparison(
        selected=pricing, selected_generation_usd=generation_usd, clip_duration=clip_duration,
        clips_needed=unique_clips, llm_usd=llm_usd,
    )  # fmt: skip

    notes: list[str] = []
    if per_clip <= 0:
        if source.kind == "mock":
            notes.append("モックはテスト用のダミー映像のため、動画生成の費用はかかりません（映像に「MOCK」と表示されます）")
        elif source.kind == "library":
            notes.append("アップロード済みの素材だけを使うため、動画生成の費用はかかりません")
    else:
        if minimum_applies:
            notes.append(
                f"新規生成 {new_clips} 本 × 1 本あたりの最低料金 ${per_clip:g}（{clip_duration:g} 秒 × ${pps:g}/秒 を"
                f"上回るため）= {_money(generation_usd)}"
            )
        else:
            notes.append(f"新規生成 {new_clips} 本 × {clip_duration:g} 秒 × ${pps:g}/秒 = {_money(generation_usd)}")
        if cached:
            notes.append(f"生成済みの素材 {cached} 本を再利用するため、その分の費用はかかりません")
        if savings_usd >= 0.005:
            percent = round(savings_usd / naive_total_usd * 100) if naive_total_usd > 0 else 0
            notes.append(
                f"素材を動画間で使い回すため、全 {shots_total} ショットを新規生成する場合（{_money(naive_total_usd)}）"
                f"より {_money(savings_usd)}（{percent}%）安くなります"
            )
    if llm_usd > 0:
        notes.append(f"絵コンテ生成（Claude）の概算 {_money(llm_usd)} を含みます")
    if variety_warning:
        notes.append(str(variety_warning))
    if not within_budget and limit is not None:
        notes.append(
            f"予算上限 {_money(limit)} を {_money(total_usd - limit)} 超えています。"
            "再利用設定を「節約」にする、本数や新規生成数の上限を減らす、または安いモデルを選んでください"
        )
    if not usable:
        notes.append(f"{source.name} はこのままでは生成を開始できません: {unusable_reason}")
    if source.kind == "api" and getattr(source, "dynamic_catalogue", False):
        notes.append(
            f"料金は {pricing.as_of} 時点の OpenRouter のモデル一覧（最新の一覧を取得できないときは組み込みの写し）の"
            "料金に基づく概算です。実際の請求額は、生成の完了時に OpenRouter が返す実費（usage.cost）で記録します"
            + ("" if pricing.verified else "（実際の API キーでの検証前のため未検証）")
        )
    elif source.kind == "api":
        notes.append(
            f"料金は {pricing.as_of} 時点の公式料金表に基づく概算で、実際の請求額とは異なる場合があります"
            + ("" if pricing.verified else "（実際の API キーでの検証前のため未検証）")
        )

    return {
        "provider": provider,
        "model": pricing.model,
        "clip_duration": clip_duration,
        "videos": videos,
        "shots_total": shots_total,
        "unique_clips": unique_clips,
        "new_clips": new_clips,
        "cached_clips": cached,
        "price_per_second_usd": pps,
        "video_generation_usd": _usd(generation_usd),
        "llm_usd": _usd(llm_usd),
        "total_usd": _usd(total_usd),
        "naive_total_usd": _usd(naive_total_usd),
        "savings_usd": _usd(savings_usd),
        "budget_limit_usd": limit,
        "within_budget": within_budget,
        "variety_score": variety_score,
        "variety_warning": variety_warning,
        "comparison": comparison,
        "notes": notes,
        # additive fields
        "configured": usable,
        "as_of": pricing.as_of,
        "pricing_verified": pricing.verified,
        "pricing_source_url": pricing.source_url,
    }


def _comparison(
    *,
    selected: ModelPricing,
    selected_generation_usd: float,
    clip_duration: float,
    clips_needed: int,
    llm_usd: float,
) -> list[dict[str, Any]]:
    """The same plan priced on every text-to-video model (+ the selected one), in registry order.

    The selected model keeps its cache-aware cost; every other model generates
    all ``clips_needed`` clips at its own default clip length (clips cached for
    one model are not reusable by another). Models without a known price are
    left out (they cannot be used).
    """
    rows: list[dict[str, Any]] = []
    for provider in list_providers():
        for model in provider.models():
            is_selected = provider.id == selected.provider and model.id == selected.model
            if not is_selected and (not model.text_to_video or not model.price_known):
                continue
            if is_selected:
                duration, generation = clip_duration, selected_generation_usd
            else:
                duration = model.default_duration
                generation = clips_needed * _per_clip_usd(model.price_per_second_usd, model.min_clip_usd, duration)
            rows.append(
                {
                    "provider": provider.id,
                    "model": model.id,
                    "total_usd": _usd(generation + llm_usd),
                    "configured": provider.check_model(model.id)[0],
                    # additive fields
                    "name": f"{provider.name} / {model.name}",
                    "selected": is_selected,
                    "clip_duration": duration,
                    "price_per_second_usd": model.price_per_second_usd,
                    "video_generation_usd": _usd(generation),
                    "verified": model.verified,
                }
            )
    return rows
