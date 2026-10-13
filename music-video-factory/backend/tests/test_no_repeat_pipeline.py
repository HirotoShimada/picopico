"""No repeated footage inside one video, end to end: estimate fields and notes, the budget-fit
suggestion, the free re-edit's pinned clip length, estimate == run, and render-time substitution.

Fixtures come from ``conftest.py`` (``fake_clips``, ``fake_render``, ``sync_tasks``, ``priced_mock``,
``add_upload_clips``); OpenRouter is respx-mocked as in ``test_pipeline_openrouter`` (never the real API).
"""

from __future__ import annotations

import math
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_pipeline_openrouter import BASE, MODEL, _listing, _no_zdr_claude, openrouter  # noqa: F401 (fixture)

from app import pipeline
from app.models import Batch, Clip, Project, Video
from app.schemas import Estimate, GenerationSettings
from app.services import cost as cost_service
from app.services.errors import BudgetExceeded, ServiceError
from app.services.providers.mock import MockProvider

OVER_BUDGET_ADVICE = (
    "新規生成数の上限を下げる（素材が少ない分はショットを長めにまとめます）、本数や動画の長さを減らす、"
    "または安いモデルを選んでください"
)


def _reload(db: Session, obj: Any) -> Any:
    db.expire_all()
    return db.get(type(obj), obj.id)


def _videos(db: Session, batch: Batch) -> list[Video]:
    db.expire_all()
    return list(db.scalars(select(Video).where(Video.batch_id == batch.id).order_by(Video.index)))


def _clip_count(db: Session) -> int:
    db.expire_all()
    return int(db.scalar(select(func.count()).select_from(Clip)) or 0)


def _stats(**overrides: Any) -> dict[str, Any]:
    """Plan stats of one video of 5 shots from 3 clips (5 s): 2 shots repeat a clip."""
    stats = {
        "videos": 1, "shots_total": 5, "unique_clips": 3, "new_clips": 3, "reused_clips": 0, "clip_duration": 5.0,
        "variety_score": 0.3, "variety_warning": "V", "shots_per_video": [5], "beat_shots_per_video": [7],
        "fitted_videos": 1, "repeat_shots": 2, "overlap_repeat_shots": 1, "adjacent_repeat_shots": 0,
        "scene_repeat_shots": 1, "videos_with_repeats": 1, "max_shot_sec": 6.4, "min_speed": 0.77, "repeat_warning": "X",
    }  # fmt: skip
    stats.update(overrides)
    return stats


def _estimate(stats: dict[str, Any], *, limit: float | None = None) -> dict[str, Any]:
    return cost_service.estimate({"count": stats["videos"]}, stats, "mock", None, cached_clips=0,
                                 llm_estimate_usd=0.0, budget_limit_usd=limit)  # fmt: skip


# --- cost.estimate --------------------------------------------------------------------------------


def test_estimate_reports_shots_and_in_video_repeats(priced_mock: Any) -> None:
    priced_mock(0.1)  # $0.50 per 5 s clip
    estimate = _estimate(_stats(), limit=0.5)
    Estimate.model_validate(estimate)
    assert (estimate["shots_per_video_min"], estimate["shots_per_video_max"], estimate["beat_shots_per_video_max"]) == (
        5, 5, 7
    )  # fmt: skip
    assert estimate["fitted_videos"] == 1 and estimate["max_shot_sec"] == 6.4 and estimate["min_speed"] == 0.77
    assert (estimate["repeat_shots"], estimate["overlap_repeat_shots"], estimate["scene_repeat_shots"]) == (2, 1, 1)
    assert estimate["adjacent_repeat_shots"] == 0  # the panel's hint names only what holds (連続しない here)
    assert estimate["videos_with_repeats"] == 1 and estimate["repeat_warning"] == "X"
    notes = estimate["notes"]
    assert notes.index("V") < notes.index("X"), "the repeat warning follows the variety warning"
    # One video whose clips come back inside it: the reuse is not "between videos".
    assert "素材を使い回すため、全 5 ショットを新規生成する場合（$2.50）より $1.00（40%）安くなります" in notes
    assert not any("動画間で" in n for n in notes)
    # Over budget: the pinned prefix, then advice that no longer suggests 節約.
    assert f"予算上限 $0.50 を $1.00 超えています。{OVER_BUDGET_ADVICE}" in notes
    assert not any("節約" in n for n in notes)
    assert estimate["video_generation_usd"] == pytest.approx(1.5) and estimate["total_usd"] == pytest.approx(1.5)


def test_estimate_savings_note_names_reuse_between_videos_only_without_repeats(priced_mock: Any) -> None:
    priced_mock(0.1)
    between = _estimate(_stats(videos=2, shots_total=10, shots_per_video=[5, 5], repeat_shots=0,
                               videos_with_repeats=0, repeat_warning=None))  # fmt: skip
    assert any(n.startswith("素材を動画間で使い回すため、全 10 ショット") for n in between["notes"])
    assert between.get("repeat_warning") is None and "X" not in between["notes"]
    repeated = _estimate(_stats(videos=2, shots_total=10, shots_per_video=[5, 5]))
    assert any(n.startswith("素材を使い回すため、全 10 ショット") for n in repeated["notes"])
    # A fitted single video uses each clip once: with only new clips nothing is saved against one clip per shot.
    fitted = _stats(shots_total=3, shots_per_video=[3], repeat_shots=0, overlap_repeat_shots=0, scene_repeat_shots=0,
                    videos_with_repeats=0, repeat_warning=None)  # fmt: skip
    new_only = _estimate(fitted)
    assert new_only["savings_usd"] == 0 and not any("使い回す" in n for n in new_only["notes"])
    # Cached clips in its pool still count as savings: 4 shots from 1 new clip and 3 ready ones.
    cached = cost_service.estimate({"count": 1}, {**fitted, "shots_total": 4, "shots_per_video": [4], "new_clips": 1,
                                                  "reused_clips": 3, "unique_clips": 4}, "mock", None, cached_clips=3,
                                   llm_estimate_usd=0.0, budget_limit_usd=None)  # fmt: skip
    assert cached["naive_total_usd"] == pytest.approx(2.0) and cached["total_usd"] == pytest.approx(0.5)
    assert cached["savings_usd"] == pytest.approx(1.5)
    savings = "素材を使い回すため、全 4 ショットを新規生成する場合（$2.00）より $1.50（75%）安くなります"
    assert savings in cached["notes"]


def test_estimate_of_old_plan_stats_keeps_its_shape(priced_mock: Any) -> None:
    priced_mock(0.1)
    new_keys = ("shots_per_video", "beat_shots_per_video", "fitted_videos", "repeat_shots", "overlap_repeat_shots",
                "adjacent_repeat_shots", "scene_repeat_shots", "videos_with_repeats", "max_shot_sec", "min_speed",
                "repeat_warning")  # fmt: skip
    old = {k: v for k, v in _stats().items() if k not in new_keys}
    estimate = _estimate(old)
    Estimate.model_validate(estimate)
    for key in ("shots_per_video_min", "shots_per_video_max", "beat_shots_per_video_max", "repeat_shots",
                "adjacent_repeat_shots", "repeat_warning", "budget_fit", "min_speed"):  # fmt: skip
        assert key not in estimate, key


# --- budget fit -----------------------------------------------------------------------------------


def _money(value: float) -> str:
    return f"${value:,.2f}"


@pytest.mark.usefixtures("openrouter")
def test_budget_fit_suggests_a_cap_that_reproduces_its_estimate(db: Session, ready_project: Project) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing("0.07"))
        _no_zdr_claude(router)
        base = {"provider": "openrouter", "model": MODEL, "count": 1, "duration_sec": 20, "seed": 5,
                "reuse": "economy"}  # fmt: skip
        natural = pipeline.preview_batch(db, ready_project, GenerationSettings(**base)).estimate
        assert natural["within_budget"] and "budget_fit" not in natural
        per_clip = 6 * 0.07  # a 6 s veo-3.1-lite clip at $0.07/s
        assert natural["new_clips"] >= 3
        assert natural["video_generation_usd"] == pytest.approx(natural["new_clips"] * per_clip)
        limit = round(natural["total_usd"] - 0.5, 2)

        over = GenerationSettings(**base, budget_limit_usd=limit)
        estimate = pipeline.preview_batch(db, ready_project, over).estimate
        Estimate.model_validate(estimate)
        assert not estimate["within_budget"]
        fit = estimate["budget_fit"]
        cap = math.floor((limit - estimate["llm_usd"] + 1e-9) / per_clip)
        assert fit["max_new_clips"] == cap and 1 <= cap < natural["new_clips"]
        assert fit["total_usd"] <= limit and fit["new_clips"] <= cap and isinstance(fit["repeat_shots"], int)
        assert fit["shots_per_video"] < natural["shots_per_video_max"] or fit["repeat_shots"] == 0
        pace = "等速" if fit["min_speed"] >= 0.995 else f"最も遅いショット{fit['min_speed']:.2f}倍速"
        reuse = (f"動画内で同じ素材が{fit['repeat_shots']}ショット再登場" if fit["repeat_shots"]
                 else "同じ素材の繰り返しなし")  # fmt: skip
        assert fit["note"] == (
            f"新規生成の上限を{cap}本にすると {_money(fit['total_usd'])} で予算内に収まります"
            f"（1本あたり{fit['shots_per_video']}ショット・最長{fit['max_shot_sec']:.1f}秒・{pace}・{reuse}）"
        )
        assert fit["note"] in estimate["notes"]

        # Applying the cap with the same seed reproduces exactly that estimate.
        applied = pipeline.preview_batch(
            db, ready_project, GenerationSettings(**base, budget_limit_usd=limit, max_new_clips=cap)
        ).estimate
        assert applied["within_budget"] and "budget_fit" not in applied
        assert applied["total_usd"] == fit["total_usd"] and applied["new_clips"] == fit["new_clips"]
        assert applied["shots_per_video_max"] == fit["shots_per_video"]
        assert applied["beat_shots_per_video_max"] == fit["beat_shots_per_video"]
        assert (applied["max_shot_sec"], applied["min_speed"]) == (fit["max_shot_sec"], fit["min_speed"])
        assert applied["repeat_shots"] == fit["repeat_shots"]

        with pytest.raises(BudgetExceeded) as info:
            pipeline.create_batch(db, ready_project, over)
        assert info.value.message == (
            f"見積もり {_money(estimate['total_usd'])} が予算上限 {_money(limit)} を超えています。{fit['note']}。"
        )
        assert "新規生成の上限を" in info.value.message
        assert db.scalars(select(Batch)).all() == []


def test_estimate_endpoint_returns_the_fit_and_the_budget_fit(
    client: TestClient, ready_project: Project, priced_mock: Any
) -> None:
    priced_mock(0.1)  # $0.50 per 5 s mock clip, no LLM cost (no keys)
    url = f"/api/projects/{ready_project.id}/estimate"
    capped = client.post(url, json={"provider": "mock", "count": 1, "duration_sec": 15, "seed": 7, "max_new_clips": 2})
    assert capped.status_code == 200, capped.text
    body = capped.json()
    assert body["repeat_shots"] >= 1 and body["repeat_warning"] in body["notes"]
    assert body["shots_per_video_max"] < body["beat_shots_per_video_max"] and body["fitted_videos"] == 1

    natural = client.post(url, json={"provider": "mock", "count": 1, "duration_sec": 15, "seed": 7}).json()
    assert natural["repeat_shots"] == 0 and natural["new_clips"] >= 3
    limit = natural["total_usd"] - 0.6  # two clips fewer fit
    over = client.post(url, json={"provider": "mock", "count": 1, "duration_sec": 15, "seed": 7,
                                  "budget_limit_usd": limit}).json()  # fmt: skip
    fit = over["budget_fit"]
    assert not over["within_budget"] and fit["max_new_clips"] == natural["new_clips"] - 2
    assert fit["total_usd"] <= limit and fit["note"] in over["notes"]


@pytest.mark.usefixtures("openrouter")
def test_no_budget_fit_when_no_cap_can_help(db: Session, ready_project: Project) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/videos/models").mock(return_value=_listing("0.07"))
        _no_zdr_claude(router)
        base = {"provider": "openrouter", "model": MODEL, "count": 1, "duration_sec": 20, "seed": 5}
        natural = pipeline.preview_batch(db, ready_project, GenerationSettings(**base)).estimate
        limit = round(natural["llm_usd"] + 0.1, 2)  # not even one clip: 0 new clips and nothing cached cannot plan
        over = GenerationSettings(**base, budget_limit_usd=limit)
        estimate = pipeline.preview_batch(db, ready_project, over).estimate
        assert not estimate["within_budget"] and "budget_fit" not in estimate
        with pytest.raises(BudgetExceeded) as info:
            pipeline.create_batch(db, ready_project, over)
        assert info.value.message.endswith("本数・新規生成数・再利用設定を見直すか、予算上限を引き上げてください。")


# --- the free re-edit -----------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [1, 2])
def test_regenerate_without_new_clips_keeps_the_batch_clip_length(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any,
    monkeypatch: pytest.MonkeyPatch, cap: int,
) -> None:  # fmt: skip
    # The canned render is 5 s, so the video is 5 s: 2 beat shots; with 1 clip they are fitted into 1 shot.
    batch = pipeline.create_batch(db, ready_project, GenerationSettings(provider="mock", count=1, duration_sec=5,
                                                                        seed=7, max_new_clips=cap))  # fmt: skip
    pipeline.run_batch(batch.id)
    (video,) = _videos(db, batch)
    assert video.status == "done"
    batch = _reload(db, batch)
    assert batch.plan["stats"]["clip_duration"] == 5.0
    ready = {c.id for c in db.scalars(select(Clip).where(Clip.status == "ready"))}
    before = _clip_count(db)

    # The model's clip length changes after the batch (snapping, a new OpenRouter listing).
    monkeypatch.setattr(MockProvider, "clip_duration_for", lambda self, model, wanted: 9.0)
    data = {**batch.settings, "max_new_clips": 0}
    existing = pipeline.existing_clip_refs(db, ready_project.id)
    with pytest.raises(ServiceError) as info:  # unpinned, no ready clip would be a cache hit
        pipeline._plan(ready_project, data, batch.storyboard, MockProvider(), existing, 1)
    assert info.value.code == "no_clips"

    for _ in range(3):  # a new random seed each time
        pipeline.regenerate_video_plan(db, db.get(Video, video.id), new_clips=False)
        assert _clip_count(db) == before, "the free re-edit creates no clips"
        plan = _reload(db, video).plan
        ids = [s["clip_id"] for s in plan["shots"]]
        assert set(ids) <= ready
        if len(ready) >= len(ids):
            assert len(ids) == len(set(ids)) and plan["fit"]["repeat_shots"] == 0
        assert len(ready) == cap and plan["fit"]["fitted"] == (plan["fit"]["beat_shots"] > cap)
        if cap == 1:
            assert len(ids) == 1 and plan["fit"]["fitted"]
        pipeline.render_video(video.id)
        assert _reload(db, video).status == "done"


# --- estimate == run ------------------------------------------------------------------------------


def test_estimate_matches_the_run_and_its_charge(
    db: Session, ready_project: Project, sync_tasks: list, fake_clips: Any, fake_render: Any, priced_mock: Any
) -> None:
    priced_mock(0.1)
    # 15 s from 2 clips of 5 s: even 2 shots at 0.75x cannot fill it, so one clip comes back (reported up front).
    batch = pipeline.create_batch(db, ready_project, GenerationSettings(provider="mock", count=1, duration_sec=15,
                                                                        seed=7, max_new_clips=2))  # fmt: skip
    estimate = batch.estimate
    pipeline.run_batch(batch.id)
    batch = _reload(db, batch)
    stats = batch.plan["stats"]
    assert estimate["new_clips"] == stats["new_clips"] == 2
    assert estimate["shots_total"] == stats["shots_total"]
    assert estimate["repeat_shots"] == stats["repeat_shots"] >= 1
    assert estimate["repeat_warning"] == stats["repeat_warning"] and stats["repeat_warning"] in estimate["notes"]
    assert estimate["shots_per_video_max"] == max(stats["shots_per_video"])
    assert estimate["beat_shots_per_video_max"] == max(stats["beat_shots_per_video"])
    assert estimate["fitted_videos"] == stats["fitted_videos"]
    assert (estimate["max_shot_sec"], estimate["min_speed"]) == (stats["max_shot_sec"], stats["min_speed"])
    # Estimate = plan = charge: the 2 planned clips are what the batch paid for.
    assert estimate["video_generation_usd"] == pytest.approx(2 * 5 * 0.1)
    assert batch.cost_usd == pytest.approx(estimate["video_generation_usd"])


# --- render-time substitution ---------------------------------------------------------------------


def _shot(clip_id: str, scene_id: str | None) -> dict[str, Any]:
    return {"clip_id": clip_id, "duration": 1.0, "speed": 1.0, "src_in": 0.0, "scene_id": scene_id}


def test_substitution_avoids_a_clip_already_in_the_video_but_keeps_the_own_pool_first(
    db: Session, ready_project: Project, add_upload_clips: Any
) -> None:
    names = ("clip_01.mp4", "clip_02.mp4", "clip_03.mp4", "clip_01.mp4", "clip_02.mp4")
    a, b, c, d, e = add_upload_clips(ready_project, names)
    for clip, scene in zip((a, b, c, d, e), ("s01", "s02", "s03", "s04", "s01"), strict=True):
        clip.scene_id = scene
    db.commit()

    def resolve(shots: list[dict[str, Any]], own: list[Clip]) -> list[dict[str, Any]]:
        plan = {"shots": shots}
        ok, substitutions = pipeline.resolve_plan_clips(
            db, plan, ready_project.id, preferred=[clip.id for clip in own], provider="library", model=None
        )
        assert ok and substitutions == 1
        return plan["shots"]

    # The missing shot showed scene s01, which clip a (already two shots earlier) also shows; c is unused.
    shots = resolve([_shot(a.id, "s01"), _shot(b.id, "s02"), _shot("gone", "s01"), _shot(d.id, "s04")], [a, b, c, d])
    assert shots[2]["clip_id"] == c.id and shots[2]["substituted_from"] == "gone"

    # The batch's own pool still comes first: an own clip already in the video beats an unused foreign one.
    shots = resolve([_shot(a.id, "s01"), _shot(b.id, "s02"), _shot("gone", "s03"), _shot(d.id, "s04")], [a, b, d])
    assert shots[2]["clip_id"] == a.id

    # ... and an unused own clip beats a foreign clip of the right scene.
    shots = resolve([_shot(a.id, "s01"), _shot("gone", "s01"), _shot(d.id, "s04")], [a, d, b])
    assert shots[1]["clip_id"] == b.id
