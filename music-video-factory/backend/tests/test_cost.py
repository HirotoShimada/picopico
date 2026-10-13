"""cost.PRICING (derived from the provider catalogue) and estimate() (§6.5)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.schemas import Estimate
from app.services import cost
from app.services.errors import ServiceError
from app.services.providers import list_providers

VEO = "veo-3.1-lite-generate-preview"


def _stats(**overrides: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {"videos": 30, "shots_total": 180, "unique_clips": 60, "new_clips": 60, "reused_clips": 0,
                             "variety_score": 0.62, "variety_warning": None, "clip_duration": 6.0}  # fmt: skip
    stats.update(overrides)
    return stats


@pytest.fixture(autouse=True)
def _no_keys(set_env: Callable[..., Any]) -> None:
    set_env(GOOGLE_API_KEY="", RUNWAY_API_KEY="", KLING_API_KEY="", KLING_ACCESS_KEY="", KLING_SECRET_KEY="")


def test_pricing_is_derived_from_provider_models() -> None:
    providers = list_providers()
    assert set(cost.PRICING) == {p.id for p in providers}
    for provider in providers:
        assert set(cost.PRICING[provider.id]) == {m.id for m in provider.models()}
        for model in provider.models():
            pricing = cost.PRICING[provider.id][model.id]
            assert pricing.price_per_second_usd == model.price_per_second_usd
            assert pricing.default_duration == model.default_duration
            assert list(pricing.clip_durations) == list(model.clip_durations)
            assert pricing.verified == model.verified
            if provider.kind == "api":
                # OpenRouter without a key: the built-in copy of the 2026-10-13 listing
                as_of = "2026-10-13" if provider.id == "openrouter" else "2026-10-11"
                assert pricing.verified is False and pricing.as_of == as_of
                assert pricing.source_url.startswith("https://")
            else:
                assert pricing.price_per_second_usd == 0
    assert cost.get_pricing("veo").model == VEO
    assert cost.get_pricing("runway", "gen4.5").clip_cost_usd(5) == pytest.approx(0.6)
    assert cost.clip_cost_usd("kling", "kling-v2-5-turbo", 10) == pytest.approx(0.42)
    assert cost.get_pricing("veo", VEO).clip_duration_for(cost.TARGET_CLIP_SEC) == 6
    with pytest.raises(ServiceError) as unknown_provider:
        cost.get_pricing("sora")
    assert unknown_provider.value.status_code == 422
    with pytest.raises(ServiceError):
        cost.get_pricing("veo", "veo-2.0-generate-001")


def test_estimate_math_with_reuse_savings() -> None:
    estimate = cost.estimate({"count": 30}, _stats(), "veo", VEO, cached_clips=0, llm_estimate_usd=0.02,
                             budget_limit_usd=50)  # fmt: skip
    Estimate.model_validate(estimate)
    assert estimate["video_generation_usd"] == pytest.approx(18.0)  # 60 clips x 6 s x $0.05
    assert estimate["llm_usd"] == pytest.approx(0.02)
    assert estimate["total_usd"] == pytest.approx(18.02)
    assert estimate["naive_total_usd"] == pytest.approx(54.02)  # 180 shots x 6 s x $0.05 + LLM
    assert estimate["savings_usd"] == pytest.approx(36.0)
    assert estimate["within_budget"] is True and estimate["budget_limit_usd"] == 50
    assert (estimate["videos"], estimate["shots_total"], estimate["unique_clips"], estimate["new_clips"]) == (30, 180, 60, 60)
    assert estimate["clip_duration"] == 6.0 and estimate["price_per_second_usd"] == 0.05
    assert estimate["configured"] is False and estimate["pricing_verified"] is False
    notes = "\n".join(estimate["notes"])
    assert "$36.00（67%）" in notes and "未検証" in notes and "2026-10-11" in notes
    assert "GOOGLE_API_KEY" in notes  # provider not configured yet


def test_cached_clips_are_free() -> None:
    estimate = cost.estimate({}, _stats(new_clips=40, reused_clips=20), "runway", "gen4.5", cached_clips=20,
                             llm_estimate_usd=0, budget_limit_usd=None)  # fmt: skip
    assert estimate["video_generation_usd"] == pytest.approx(40 * 6 * 0.12)
    assert estimate["cached_clips"] == 20
    assert estimate["naive_total_usd"] == pytest.approx(180 * 6 * 0.12)
    assert estimate["savings_usd"] == pytest.approx((180 - 40) * 6 * 0.12)
    assert estimate["within_budget"] is True and estimate["budget_limit_usd"] is None
    assert any("20 本を再利用" in note for note in estimate["notes"])


def test_budget_flag_and_fallback_to_settings() -> None:
    over = cost.estimate({}, _stats(new_clips=180, unique_clips=180), "veo", VEO, cached_clips=0, llm_estimate_usd=0,
                         budget_limit_usd=50)  # fmt: skip
    assert over["total_usd"] == pytest.approx(54.0) and over["within_budget"] is False
    assert any("予算上限 $50.00 を $4.00 超えています" in note for note in over["notes"])
    from_settings = cost.estimate({"budget_limit_usd": 10}, _stats(), "veo", VEO, cached_clips=0, llm_estimate_usd=0,
                                  budget_limit_usd=None)  # fmt: skip
    assert from_settings["budget_limit_usd"] == 10 and from_settings["within_budget"] is False
    exact = cost.estimate({}, _stats(), "veo", VEO, cached_clips=0, llm_estimate_usd=0, budget_limit_usd=18.0)
    assert exact["within_budget"] is True


def test_mock_and_library_cost_nothing_but_the_llm() -> None:
    mock = cost.estimate({}, _stats(clip_duration=5.0), "mock", None, cached_clips=0, llm_estimate_usd=0.01,
                         budget_limit_usd=50)  # fmt: skip
    assert mock["model"] == "mock-v1" and mock["video_generation_usd"] == 0 and mock["total_usd"] == pytest.approx(0.01)
    assert mock["naive_total_usd"] == pytest.approx(0.01) and mock["savings_usd"] == 0
    assert mock["configured"] is True and any("MOCK" in n for n in mock["notes"])
    assert not any("未検証" in n for n in mock["notes"])
    library = cost.estimate({}, _stats(new_clips=12, unique_clips=12, clip_duration=5.0), "library", None,
                            cached_clips=0, llm_estimate_usd=0, budget_limit_usd=None)  # fmt: skip
    assert library["new_clips"] == 0 and library["total_usd"] == 0 and library["naive_total_usd"] == 0
    assert any("アップロード" in n for n in library["notes"])
    Estimate.model_validate(library)


def test_missing_clip_duration_uses_model_default_and_variety_warning_passes_through() -> None:
    warning = "素材の再利用率が高いため、動画間の映像バリエーションが限定的です"
    stats = _stats(variety_score=0.2, variety_warning=warning)
    del stats["clip_duration"]
    estimate = cost.estimate({}, stats, "kling", "kling-3.0", cached_clips=0, llm_estimate_usd=0, budget_limit_usd=None)
    assert estimate["clip_duration"] == 5.0
    assert estimate["video_generation_usd"] == pytest.approx(60 * 5 * 0.112)
    assert estimate["variety_score"] == 0.2 and estimate["variety_warning"] == warning
    assert warning in estimate["notes"]


def test_comparison_prices_the_plan_on_every_text_to_video_model(set_env: Callable[..., Any]) -> None:
    set_env(GOOGLE_API_KEY="test-google-key")
    estimate = cost.estimate({}, _stats(new_clips=50, reused_clips=10), "veo", VEO, cached_clips=10,
                             llm_estimate_usd=0.0, budget_limit_usd=None)  # fmt: skip
    rows = {(r["provider"], r["model"]): r for r in estimate["comparison"]}
    expected_models = {(p.id, m.id) for p in list_providers() for m in p.models() if m.text_to_video}
    assert set(rows) == expected_models
    assert ("runway", "gen4_turbo") not in rows and ("library", "library") not in rows
    selected = rows[("veo", VEO)]
    assert selected["selected"] is True and selected["total_usd"] == estimate["total_usd"] == pytest.approx(15.0)
    assert selected["configured"] is True and estimate["configured"] is True
    # Other models generate every clip of the plan at their own default length.
    assert rows[("runway", "gen4.5")]["total_usd"] == pytest.approx(60 * 5 * 0.12)
    assert rows[("kling", "kling-v2-5-turbo")]["total_usd"] == pytest.approx(60 * 5 * 0.042)
    assert rows[("veo", f"{VEO}@1080p")]["total_usd"] == pytest.approx(60 * 8 * 0.08)
    assert rows[("mock", "mock-v1")]["total_usd"] == 0 and rows[("mock", "mock-v1")]["configured"] is True
    assert rows[("runway", "gen4.5")]["configured"] is False and rows[("kling", "kling-3.0")]["configured"] is False
    assert list(dict.fromkeys(r["provider"] for r in estimate["comparison"])) == [
        "mock", "veo", "runway", "kling", "openrouter"
    ]
    # OpenRouter models (built-in list without a key) are priced at their own default length
    # (Veo 3.1 Lite: duration_seconds_without_audio_720p = $0.03/s in the 2026-10-13 listing).
    assert rows[("openrouter", "google/veo-3.1-lite")]["total_usd"] == pytest.approx(60 * 6 * 0.03)
    assert rows[("openrouter", "google/veo-3.1-lite")]["configured"] is False


def test_selected_image_only_model_is_still_listed() -> None:
    estimate = cost.estimate({}, _stats(clip_duration=5.0), "runway", "gen4_turbo", cached_clips=0,
                             llm_estimate_usd=0, budget_limit_usd=None)  # fmt: skip
    rows = [r for r in estimate["comparison"] if r["selected"]]
    assert [(r["provider"], r["model"]) for r in rows] == [("runway", "gen4_turbo")]
    assert estimate["configured"] is False
    assert any("画像" in note or "RUNWAY_API_KEY" in note for note in estimate["notes"])


def _synthetic_analysis() -> dict[str, Any]:
    return {
        "duration": 60.0, "bpm": 120.0,
        "beats": [round(0.5 * i, 3) for i in range(120)], "downbeats": [round(2.0 * i, 3) for i in range(30)],
        "sections": [{"start": 0, "end": 8, "label": "intro", "energy": 0.2},
                     {"start": 8, "end": 24, "label": "verse", "energy": 0.5},
                     {"start": 24, "end": 40, "label": "chorus", "energy": 0.9},
                     {"start": 40, "end": 60, "label": "verse", "energy": 0.5}],
        "chorus_candidates": [{"start": 24, "end": 40, "score": 0.9}], "suggested_segments": [],
    }  # fmt: skip


def test_estimate_consumes_real_planner_stats() -> None:
    from app.services.providers import get_provider
    from app.services.storyboard import generate_storyboard
    from app.services.variation import plan_batch

    analysis = _synthetic_analysis()
    storyboard = generate_storyboard(title="t", artist_name="a", mood="切ない", purpose="", lyrics=[], analysis=analysis,
                                     preset_id="cinematic", custom_world=None, n_scenes=8, seed=1, use_claude=False)  # fmt: skip
    provider = get_provider("veo")
    model = provider.default_model()
    clip_duration = provider.clip_duration_for(model, cost.TARGET_CLIP_SEC)
    settings = {"provider": "veo", "model": model, "count": 10, "duration_sec": 15, "reuse": "balanced",
                "preset": "cinematic", "start_strategy": "varied"}  # fmt: skip
    plan = plan_batch(analysis=analysis, lyric_lines=[], settings=settings, storyboard=storyboard, segment=(24.0, 39.0),
                      provider_clip_duration=clip_duration, existing_clips=[], seed=5)  # fmt: skip
    stats = plan["stats"]
    estimate = cost.estimate(settings, stats, "veo", model, cached_clips=stats["reused_clips"], llm_estimate_usd=0.0,
                             budget_limit_usd=50)  # fmt: skip
    Estimate.model_validate(estimate)
    assert estimate["clip_duration"] == clip_duration == 6.0
    assert estimate["new_clips"] == len([s for s in plan["clip_specs"] if s["existing_clip_id"] is None])
    assert estimate["video_generation_usd"] == pytest.approx(estimate["new_clips"] * 6.0 * 0.05)
    assert estimate["naive_total_usd"] == pytest.approx(stats["shots_total"] * 6.0 * 0.05)
    assert estimate["savings_usd"] > 0 and estimate["within_budget"] is True
