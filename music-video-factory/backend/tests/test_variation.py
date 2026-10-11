"""Batch variation planner (ARCHITECTURE.md §6.4)."""

from __future__ import annotations

import functools
import itertools
import json
import math
import random
from typing import Any

import pytest

from app import schemas
from app.services import variation
from app.services.errors import ServiceError
from app.services.presets import COLOR_GRADES, WORLD_PRESETS
from app.services.storyboard import generate_storyboard
from app.services.variation import (
    MAX_POOL,
    MAX_SHOT_SEC,
    MIN_SHOT_SEC,
    VARIETY_WARNING_JA,
    make_cache_key,
    plan_batch,
    pool_target_size,
)

REUSES = ("max_variety", "balanced", "economy")
STRATEGIES = ("segment", "chorus", "varied", "custom")
PANS = {"left", "right", "up", "down", "center"}


# --- fixtures -------------------------------------------------------------------------------------


def make_analysis(*, duration: float = 60.0, bpm: float = 120.0, seed: int = 0, jitter: float = 0.0) -> dict[str, Any]:
    """Analysis shaped like audio_analysis output; section layout scales the demo song's."""
    rng = random.Random(seed)
    step = 60.0 / bpm
    t = round(rng.uniform(0.0, step * 0.5), 3)
    beats: list[float] = []
    while t <= duration:
        beats.append(round(min(duration, max(0.0, t + (rng.uniform(-jitter, jitter) if jitter else 0.0))), 3))
        t += step
    beats = sorted(set(beats))
    layout = [(0.0, 0.133, "intro", 0.3), (0.133, 0.4, "verse", 0.55), (0.4, 0.667, "chorus", 0.85),
              (0.667, 0.8, "bridge", 0.35), (0.8, 1.0, "chorus", 0.8)]  # fmt: skip
    sections = [
        {"start": round(a * duration, 3), "end": round(b * duration, 3), "label": label, "energy": energy,
         "is_chorus_candidate": label == "chorus"}
        for a, b, label, energy in layout
    ]  # fmt: skip
    choruses = [s for s in sections if s["label"] == "chorus"]
    downbeats = beats[::4]

    def snap(x: float) -> float:
        return min(downbeats, key=lambda d: abs(d - x)) if downbeats else x

    suggested = [
        {"start": snap(choruses[0]["start"]) if choruses[0]["start"] + d <= duration else 0.0, "duration": d,
         "end": 0.0, "score": 0.9, "reason": "テスト"}
        for d in (15, 30, 60) if d <= duration
    ]  # fmt: skip
    return {
        "version": 1, "duration": duration, "bpm": bpm, "bpm_confidence": 0.9, "beats": beats, "downbeats": downbeats,
        "sections": sections,
        "chorus_candidates": [
            {"start": s["start"], "end": s["end"], "score": 0.9 - 0.1 * i} for i, s in enumerate(choruses)
        ],
        "highlights": [{"start": choruses[0]["start"] + 2, "end": choruses[0]["end"], "energy": 0.9}],
        "quiet_parts": [], "suggested_segments": suggested,
    }  # fmt: skip


@functools.cache
def _storyboard_json(preset: str, n_scenes: int) -> str:
    board = generate_storyboard(
        title="テスト曲", artist_name="Test Artist", mood=None, purpose=None, lyrics=[], analysis=make_analysis(),
        preset_id=preset, custom_world="雨の夜の街" if preset == "custom" else None, n_scenes=n_scenes, seed=5,
        use_claude=False,
    )  # fmt: skip
    return json.dumps(board)


def storyboard(preset: str = "cinematic", n_scenes: int = 8) -> dict[str, Any]:
    return json.loads(_storyboard_json(preset, n_scenes))


def settings(**overrides: Any) -> dict[str, Any]:
    base = schemas.GenerationSettings(provider="mock", model="mock-v1").model_dump()
    base.update(overrides)
    return base


def plan(**kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "analysis": make_analysis(), "lyric_lines": [], "settings": settings(), "storyboard": storyboard(),
        "segment": (24.0, 39.0), "provider_clip_duration": 5.0, "existing_clips": [], "seed": 1,
    }  # fmt: skip
    args.update(kwargs)
    return plan_batch(**args)


def _ref(shot: dict[str, Any]) -> str:
    return str(shot["clip_key"] or shot["clip_id"])


def _signature(video: dict[str, Any]) -> tuple[Any, ...]:
    return tuple((_ref(s), s["src_in"], s["duration"], s["hflip"]) for s in video["shots"])


def check_plan(
    result: dict[str, Any],
    *,
    analysis: dict[str, Any],
    conf: dict[str, Any],
    clip_duration: float,
    existing: list[dict[str, Any]],
) -> None:
    """Every §6.4 invariant that must hold for any input."""
    library = conf["provider"] == "library"
    song = analysis["duration"]
    beats = analysis["beats"]
    expected_duration = min(round(conf["duration_sec"] * 1000), math.floor(song * 1000 + 1e-6)) / 1000
    existing_by_id = {c["id"]: c for c in existing}
    specs = {spec["key"]: spec for spec in result["clip_specs"]}
    assert len(specs) == len(result["clip_specs"]), "clip keys are unique"
    assert len(result["videos"]) == conf["count"]

    used: dict[str, int] = {}
    for video in result["videos"]:
        schemas.VideoPlan.model_validate(video)
        duration = video["duration"]
        assert duration == expected_duration
        durations = [shot["duration"] for shot in video["shots"]]
        assert abs(math.fsum(durations) - duration) < 1e-6, "shots sum exactly to the duration"
        assert sum(round(d * 1000) for d in durations) == round(duration * 1000)
        assert video["start"] >= 0 and video["start"] + duration <= song + 1e-6
        if duration >= 2 * MIN_SHOT_SEC or duration > MAX_SHOT_SEC:
            assert all(MIN_SHOT_SEC - 1e-9 <= d <= MAX_SHOT_SEC + 1e-9 for d in durations), durations
        else:
            assert durations == [duration]
        t = video["start"]
        for d in durations[:-1]:
            t += d
            assert min(abs(t - b) for b in beats) <= 0.0015, f"cut at {t:.3f}s is not on a beat"
        if conf["color_grade"] == "auto":
            assert video["color_grade"] in WORLD_PRESETS[conf["preset"]]["color_grades"]
        else:
            assert video["color_grade"] == conf["color_grade"]
        assert video["lyric_style"] == conf["lyric_style"]
        assert video["title"] == f"{WORLD_PRESETS[conf['preset']]['name_en']} #{video['index'] + 1}"

        refs = [_ref(shot) for shot in video["shots"]]
        for shot in video["shots"]:
            if library:
                assert shot["clip_key"] is None
                clip = existing_by_id[shot["clip_id"]]
                assert clip["source"] == "upload"
                length = clip["duration"]
            else:
                spec = specs[shot["clip_key"]]
                assert shot["clip_id"] == spec["existing_clip_id"]
                length = (
                    existing_by_id[spec["existing_clip_id"]]["duration"]
                    if spec["existing_clip_id"]
                    else spec["duration"]
                )
            assert shot["src_in"] >= 0
            assert shot["src_in"] + shot["duration"] * shot["speed"] <= length + 1e-6, (shot, length)
            assert 0 < shot["speed"] <= 1.0
            assert 1.0 <= shot["zoom_from"] <= 1.2 and 1.0 <= shot["zoom_to"] <= 1.2
            assert shot["pan"] in PANS
            used[_ref(shot)] = used.get(_ref(shot), 0) + 1
        pool = len(specs) if not library else len({c["id"] for c in existing if c["source"] == "upload"})
        if pool > 1:
            assert all(a != b for a, b in itertools.pairwise(refs)), "same clip twice in a row"

    if not library:
        assert set(used) == set(specs), "every planned clip is used (no paid clip is wasted)"
        for spec in specs.values():
            assert spec["duration"] == clip_duration
            expected_key = make_cache_key(
                conf["provider"],
                conf["model"],
                spec["prompt"],
                spec["negative_prompt"],
                clip_duration,
                "9:16",
                spec["seed"],
            )
            assert spec["cache_key"] == expected_key
        if conf.get("max_new_clips") is not None:
            assert result["stats"]["new_clips"] <= conf["max_new_clips"]
    else:
        assert result["clip_specs"] == []

    stats = result["stats"]
    assert stats["videos"] == conf["count"]
    assert stats["shots_total"] == sum(len(v["shots"]) for v in result["videos"])
    assert stats["unique_clips"] == len(used)
    if not library:
        assert stats["new_clips"] + stats["reused_clips"] == len(specs)
        assert stats["new_clips"] == sum(1 for s in specs.values() if s["existing_clip_id"] is None)
    assert 0 < stats["variety_score"] <= 1
    assert (stats["variety_warning"] is not None) == (stats["variety_score"] < 0.35)

    if conf["count"] >= 2:
        full = [json.dumps(v["shots"], sort_keys=True) for v in result["videos"]]
        assert len(set(full)) == len(full), "no two videos have identical shot lists"
        if min(len(v["shots"]) for v in result["videos"]) >= 3 and len(used) >= 3:
            sigs = [_signature(v) for v in result["videos"]]
            assert len(set(sigs)) == len(sigs)


# --- property-style invariants ------------------------------------------------------------------------


@pytest.mark.parametrize("case", range(150))
def test_invariants_over_random_settings(case: int) -> None:
    rng = random.Random(1000 + case)
    song = rng.choice([3.0, 7.5, 12.0, 20.0, 45.0, 60.0, 95.0, 180.0, 240.0])
    bpm = rng.choice([62.0, 75.0, 90.0, 104.0, 120.0, 128.0, 140.0, 160.0, 174.0, 200.0])
    analysis = make_analysis(duration=song, bpm=bpm, seed=case, jitter=rng.choice([0.0, 0.0, 0.03]))
    provider = rng.choice(["mock", "mock", "kling", "library"])
    preset = rng.choice(list(WORLD_PRESETS))
    clip_duration = float(rng.choice([4, 5, 6, 8, 10]))
    duration = rng.choice([5, 8, 15, 15, 30, 60, 90])
    conf = settings(
        preset=preset, provider=provider, model="mock-v1" if provider == "mock" else "kling-3.0",
        duration_sec=duration, count=rng.choice([1, 2, 3, 5, 8, 12]), reuse=rng.choice(REUSES),
        start_strategy=rng.choice(STRATEGIES), start_sec=round(rng.uniform(0, song), 2),
        lyric_style=rng.choice(["simple", "cinematic", "minimal", "karaoke", "fade"]),
        color_grade=rng.choice(["auto", "auto", "mono_noir"]), max_new_clips=rng.choice([None, None, 2, 7, 40]),
    )  # fmt: skip
    existing: list[dict[str, Any]] = []
    if provider == "library":
        existing = [
            {"id": f"u{i}", "scene_id": None, "cache_key": None, "duration": rng.choice([1.0, 2.5, 4.0, 12.0, 45.0]),
             "source": "upload"}
            for i in range(rng.randint(1, 6))
        ]  # fmt: skip
    seg_start = rng.choice(analysis["downbeats"] or [0.0])
    segment = rng.choice([None, (seg_start, min(song, seg_start + duration))])
    board = storyboard(preset if preset != "custom" else "cinematic", rng.choice([1, 3, 6, 8, 12]))

    result = plan_batch(
        analysis=analysis, lyric_lines=[], settings=conf, storyboard=board, segment=segment,
        provider_clip_duration=clip_duration, existing_clips=existing, seed=case,
    )  # fmt: skip
    check_plan(result, analysis=analysis, conf=conf, clip_duration=clip_duration, existing=existing)
    if provider != "library":
        target = pool_target_size(conf["reuse"], result["stats"]["shots_total"],
                                  max(result["stats"]["shots_per_video"]), len(board["scenes"]))  # fmt: skip
        expected = target if conf["max_new_clips"] is None else min(target, conf["max_new_clips"])
        assert result["stats"]["new_clips"] == expected


def test_plan_is_deterministic_for_a_seed() -> None:
    conf = settings(count=6, start_strategy="varied", reuse="balanced")
    assert plan(settings=conf, seed=3) == plan(settings=conf, seed=3)
    assert plan(settings=conf, seed=3)["videos"] != plan(settings=conf, seed=4)["videos"]


def test_plan_does_not_depend_on_budget() -> None:
    cheap = plan(settings=settings(count=4, budget_limit_usd=0.01))
    rich = plan(settings=settings(count=4, budget_limit_usd=10_000))
    assert cheap == rich


# --- clip pool --------------------------------------------------------------------------------------------


def test_make_cache_key() -> None:
    key = make_cache_key("kling", "kling-3.0", "a prompt", "neg", 5, "9:16", 42)
    assert len(key) == 64 and int(key, 16) >= 0
    assert key == make_cache_key("kling", "kling-3.0", "a prompt", "neg", 5.0, "9:16", 42)
    variants = [
        ("runway", "kling-3.0", "a prompt", "neg", 5, "9:16", 42),
        ("kling", "kling-2", "a prompt", "neg", 5, "9:16", 42),
        ("kling", "kling-3.0", "a prompt!", "neg", 5, "9:16", 42),
        ("kling", "kling-3.0", "a prompt", "neg2", 5, "9:16", 42),
        ("kling", "kling-3.0", "a prompt", "neg", 10, "9:16", 42),
        ("kling", "kling-3.0", "a prompt", "neg", 5, "16:9", 42),
        ("kling", "kling-3.0", "a prompt", "neg", 5, "9:16", 43),
        ("kling", "kling-3.0", "a prompt", "neg", 5, "9:16", None),
    ]
    assert len({make_cache_key(*v) for v in variants} | {key}) == len(variants) + 1


def test_pool_size_per_reuse_policy() -> None:
    sizes = {}
    for reuse in REUSES:
        result = plan(settings=settings(count=10, reuse=reuse, duration_sec=30))
        stats = result["stats"]
        expected = pool_target_size(reuse, stats["shots_total"], max(stats["shots_per_video"]), 8)
        assert stats["new_clips"] == stats["unique_clips"] == expected
        sizes[reuse] = expected
    total = result["stats"]["shots_total"]
    assert sizes["max_variety"] == min(total, MAX_POOL)
    assert sizes["balanced"] == math.ceil(total / 3)
    assert sizes["economy"] == math.ceil(total / 6)
    assert sizes["max_variety"] > sizes["balanced"] > sizes["economy"]


def test_pool_has_at_least_one_clip_per_scene_or_shot() -> None:
    result = plan(settings=settings(count=1, reuse="economy", duration_sec=15), storyboard=storyboard(n_scenes=12))
    shots = result["stats"]["shots_total"]
    assert result["stats"]["new_clips"] == min(shots, 12)
    assert len(set(_ref(s) for s in result["videos"][0]["shots"])) == shots, "no repeats within a short video"


def test_max_variety_is_capped() -> None:
    result = plan(
        settings=settings(count=50, reuse="max_variety", duration_sec=90), analysis=make_analysis(duration=180)
    )
    assert result["stats"]["shots_total"] > MAX_POOL
    assert result["stats"]["new_clips"] == MAX_POOL


def test_pool_covers_all_scenes_before_alternate_takes() -> None:
    result = plan(settings=settings(count=8, reuse="max_variety", duration_sec=30))
    specs = result["clip_specs"]
    assert {s["scene_id"] for s in specs} == {f"s{i:02d}" for i in range(1, 9)}
    takes = [s for s in specs if s["variant"] > 0]
    assert takes and all("Alternate take" in s["prompt"] for s in takes)
    for spec in specs:
        assert spec["prompt"].rstrip(".").endswith("no text, no captions, no watermark")
        assert len(spec["prompt"]) <= 1000
    assert len({s["cache_key"] for s in specs}) == len(specs)


def test_cache_hits_are_reused_and_not_regenerated() -> None:
    first = plan(settings=settings(count=4, reuse="economy"))
    existing = [{"id": f"clip{i}", "scene_id": s["scene_id"], "cache_key": s["cache_key"], "duration": 5.0,
                 "source": "mock", "status": "ready"} for i, s in enumerate(first["clip_specs"])]  # fmt: skip
    again = plan(settings=settings(count=4, reuse="economy"), existing_clips=existing, seed=99)
    assert again["stats"]["new_clips"] == 0 and again["stats"]["reused_clips"] == len(existing)
    for spec in again["clip_specs"]:
        assert spec["existing_clip_id"] in {c["id"] for c in existing}
    for video in again["videos"]:
        assert all(shot["clip_id"] is not None for shot in video["shots"])
    check_plan(
        again, analysis=make_analysis(), conf=settings(count=4, reuse="economy"), clip_duration=5.0, existing=existing
    )

    # A bigger pool reuses the cached clips and only generates the rest.
    bigger = plan(settings=settings(count=4, reuse="balanced"), existing_clips=existing, seed=7)
    stats = bigger["stats"]
    assert stats["reused_clips"] == len(existing) and stats["new_clips"] == stats["unique_clips"] - len(existing) > 0

    # Not ready / other provider / fresh_clips → no hits.
    not_ready = [{**c, "status": "failed"} for c in existing]
    assert plan(settings=settings(count=4, reuse="economy"), existing_clips=not_ready)["stats"]["reused_clips"] == 0
    other = plan(
        settings=settings(count=4, reuse="economy", provider="kling", model="kling-3.0"), existing_clips=existing
    )
    assert other["stats"]["reused_clips"] == 0
    fresh = plan(settings=settings(count=4, reuse="economy", fresh_clips=True), existing_clips=existing)
    assert fresh["stats"]["reused_clips"] == 0


def test_free_cache_hits_beyond_the_pool_target_add_variety() -> None:
    rich = plan(settings=settings(count=6, reuse="max_variety"))
    existing = [{"id": f"clip{i}", "scene_id": s["scene_id"], "cache_key": s["cache_key"], "duration": 5.0,
                 "source": "mock"} for i, s in enumerate(rich["clip_specs"])]  # fmt: skip
    economy = plan(settings=settings(count=6, reuse="economy"), existing_clips=existing)
    target = pool_target_size("economy", economy["stats"]["shots_total"], max(economy["stats"]["shots_per_video"]), 8)
    assert economy["stats"]["new_clips"] == 0
    assert economy["stats"]["unique_clips"] > target


def test_max_new_clips_caps_generation() -> None:
    result = plan(settings=settings(count=5, reuse="max_variety", max_new_clips=3))
    assert result["stats"]["new_clips"] == 3 and len(result["clip_specs"]) == 3
    assert any("上限" in w for w in result["warnings"])
    with pytest.raises(ServiceError) as excinfo:
        plan(settings=settings(count=2, max_new_clips=0))
    assert excinfo.value.status_code == 422


def test_existing_clip_duration_bounds_in_points() -> None:
    first = plan(settings=settings(count=3, reuse="economy"))
    short = [{"id": f"clip{i}", "scene_id": s["scene_id"], "cache_key": s["cache_key"], "duration": 3.2,
              "source": "mock"} for i, s in enumerate(first["clip_specs"])]  # fmt: skip
    again = plan(settings=settings(count=3, reuse="economy"), existing_clips=short)
    check_plan(
        again, analysis=make_analysis(), conf=settings(count=3, reuse="economy"), clip_duration=5.0, existing=short
    )


def test_low_variety_warning() -> None:
    result = plan(settings=settings(count=30, reuse="economy", max_new_clips=2))
    assert result["stats"]["variety_score"] < 0.35
    assert result["stats"]["variety_warning"].startswith(VARIETY_WARNING_JA)
    rich = plan(settings=settings(count=3, reuse="max_variety"))
    assert rich["stats"]["variety_score"] == 1.0 and rich["stats"]["variety_warning"] is None


def _advice(**kwargs: Any) -> str:
    result = plan(**kwargs)
    warning = result["stats"]["variety_warning"]
    assert result["stats"]["variety_score"] < 0.35 and warning is not None
    assert warning.startswith(VARIETY_WARNING_JA)
    return warning.removeprefix(VARIETY_WARNING_JA)


def test_variety_warning_names_the_actual_cause() -> None:
    # Regression: the warning always told the user to change the reuse setting, even when
    # that could not help (library uploads, a max_new_clips cap, the pool limit).
    library = _advice(
        settings=settings(provider="library", model=None, count=10, reuse="balanced"), storyboard=None,
        existing_clips=[{"id": "u1", "duration": 3.0, "source": "upload"}, {"id": "u2", "duration": 2.5, "source": "upload"}],
    )  # fmt: skip
    assert "アップロード素材が 2 本" in library and "追加でアップロード" in library and "再利用設定" not in library

    capped = _advice(settings=settings(count=30, reuse="balanced", max_new_clips=2))
    assert "新規生成の上限（2 本）" in capped and "引き上げる" in capped
    assert "再利用設定" not in capped and "アップロード" not in capped

    economy = _advice(settings=settings(count=30, reuse="economy"), provider_clip_duration=2.0)
    assert economy == "再利用設定が「節約」です。「バランス」か「バリエーション重視」にすると改善します。"

    crowded = _advice(
        settings=settings(count=50, reuse="max_variety", duration_sec=90), analysis=make_analysis(duration=180),
        provider_clip_duration=2.0,
    )  # fmt: skip
    assert f"上限（{MAX_POOL} 本）" in crowded and "本数を減らす" in crowded and "再利用設定" not in crowded

    few_scenes = _advice(settings=settings(count=30, reuse="balanced", max_new_clips=3), storyboard=storyboard(n_scenes=2))
    assert "新規生成の上限（3 本）" in few_scenes and "絵コンテのシーンが 2 個" in few_scenes


def test_no_mirroring_unless_allowed() -> None:
    # Regression: shots were flipped by default, turning text in the footage into mirror writing.
    conf = settings(count=12, reuse="economy")
    assert "allow_mirror" not in conf or conf["allow_mirror"] is False
    shots = [s for v in plan(settings=conf)["videos"] for s in v["shots"]]
    assert not any(s["hflip"] for s in shots)
    mirrored = [s for v in plan(settings=settings(count=12, reuse="economy", allow_mirror=True))["videos"]
                for s in v["shots"]]  # fmt: skip
    assert any(s["hflip"] for s in mirrored) and not all(s["hflip"] for s in mirrored)


def test_repeated_footage_gets_a_different_camera_move() -> None:
    # Without flips, variety comes from the camera move: footage that comes back (same clip,
    # same in-point bucket) is framed with a pan / zoom direction it has not had yet.
    result = plan(settings=settings(count=30, reuse="economy"), provider_clip_duration=2.0)
    moves: dict[tuple[str, int], list[tuple[str, int]]] = {}
    fresh = repeats = 0
    for shot in (s for v in result["videos"] for s in v["shots"]):
        move = (shot["pan"], 1 if shot["zoom_to"] > shot["zoom_from"] else -1)
        earlier = moves.setdefault((_ref(shot), int(shot["src_in"] // 1.0)), [])
        if earlier:
            repeats += 1
            fresh += move not in earlier
        earlier.append(move)
    assert repeats > 50
    assert fresh / repeats >= 0.97  # random moves alone repeated one in ~7 of them


def test_variety_score_counts_a_new_camera_move_like_a_mirror() -> None:
    def shot(pan: str, zoom_to: float, hflip: bool = False, src_in: float = 0.2) -> dict[str, Any]:
        return {"clip_key": "c01", "clip_id": None, "src_in": src_in, "hflip": hflip, "pan": pan,
                "zoom_from": 1.0 if pan == "center" else 1.06, "zoom_to": zoom_to}  # fmt: skip

    score = variation._variety_score
    assert score([{"shots": [shot("left", 1.1), shot("left", 1.1)]}]) == 0.5  # same footage, same framing
    assert score([{"shots": [shot("left", 1.1), shot("right", 1.1)]}]) == 1.0  # another camera move
    assert score([{"shots": [shot("left", 1.1), shot("left", 1.1, hflip=True)]}]) == 1.0  # mirrored (opt-in)
    assert score([{"shots": [shot("left", 1.1), shot("up", 1.1), shot("down", 1.0)]}]) == round(2 / 3, 3)
    assert score([{"shots": [shot("left", 1.1), shot("left", 1.1, src_in=2.4)]}]) == 1.0  # other in-point


def test_requires_storyboard_scenes_for_generation() -> None:
    with pytest.raises(ServiceError):
        plan(storyboard={"scenes": []})


def test_shots_prefer_scenes_of_the_matching_section() -> None:
    result = plan(settings=settings(count=4, reuse="balanced", start_strategy="segment"), segment=(24.0, 39.0))
    specs = {s["key"]: s for s in result["clip_specs"]}
    shots = [s for v in result["videos"] for s in v["shots"]]
    assert all(s["section"] == "chorus" for s in shots)
    matching = sum(specs[s["clip_key"]]["section"] == "chorus" for s in shots)
    assert matching / len(shots) >= 0.5


# --- library mode -------------------------------------------------------------------------------------------


def _uploads() -> list[dict[str, Any]]:
    return [
        {"id": "up-long", "scene_id": None, "cache_key": None, "duration": 40.0, "source": "upload"},
        {"id": "up-mid", "scene_id": None, "cache_key": None, "duration": 9.0, "source": "upload"},
        {"id": "up-short", "scene_id": None, "cache_key": None, "duration": 2.5, "source": "upload"},
        {"id": "gen-1", "scene_id": "s01", "cache_key": "abc", "duration": 5.0, "source": "mock"},
    ]


def test_library_mode_uses_uploaded_clips_only() -> None:
    conf = settings(provider="library", model=None, count=6, reuse="economy")
    result = plan(settings=conf, existing_clips=_uploads(), storyboard=None)
    assert result["clip_specs"] == []
    check_plan(result, analysis=make_analysis(), conf=conf, clip_duration=5.0, existing=_uploads())
    shots = [s for v in result["videos"] for s in v["shots"]]
    assert {s["clip_id"] for s in shots} <= {"up-long", "up-mid", "up-short"}
    assert {s["clip_id"] for s in shots} >= {"up-long", "up-mid"}
    long_ins = {round(s["src_in"]) for s in shots if s["clip_id"] == "up-long"}
    assert len(long_ins) >= 4, "in-points spread across the long upload"
    assert max(long_ins) > 15
    stats = result["stats"]
    assert stats["new_clips"] == 0 and stats["reused_clips"] == stats["unique_clips"]


def test_library_mode_without_uploads_is_rejected() -> None:
    with pytest.raises(ServiceError) as excinfo:
        plan(settings=settings(provider="library", model=None), existing_clips=_uploads()[3:])
    assert excinfo.value.status_code == 422


# --- timing --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bpm", [62.0, 90.0, 120.0, 174.0, 200.0])
def test_shot_lengths_follow_bars(bpm: float) -> None:
    analysis = make_analysis(duration=120, bpm=bpm)
    result = plan(analysis=analysis, settings=settings(count=6, duration_sec=60))
    bar = 4 * 60 / bpm
    for video in result["videos"]:
        # The first shot starts at the (user-chosen, possibly off-beat) segment start: only shots
        # bounded by beat cuts on both sides must be whole bars.
        for shot in video["shots"][1:-1]:
            bars = shot["duration"] / bar
            whole_bar_lengths = [n for n in range(1, 9) if MIN_SHOT_SEC <= n * bar <= MAX_SHOT_SEC]
            if len(whole_bar_lengths) >= 2:
                assert abs(bars - round(bars)) < 0.02 and round(bars) in whole_bar_lengths, (bpm, shot["duration"])
                if bar >= MIN_SHOT_SEC:
                    assert round(bars) in (1, 2), "1 or 2 bars when a bar fits the 1.5-6 s range"
            else:  # slow song: half-bar multiples keep the rhythm varied
                assert abs(2 * bars - round(2 * bars)) < 0.04, (bpm, shot["duration"])
            assert MIN_SHOT_SEC <= shot["duration"] <= MAX_SHOT_SEC
    patterns = {tuple(round(s["duration"], 1) for s in v["shots"]) for v in result["videos"]}
    assert len(patterns) >= 2, "shot rhythm varies between videos"


def test_cuts_follow_irregular_beats() -> None:
    analysis = make_analysis(duration=90, bpm=97, jitter=0.04, seed=3)
    conf = settings(count=5, duration_sec=30, start_strategy="varied")
    check_plan(plan(analysis=analysis, settings=conf), analysis=analysis, conf=conf, clip_duration=5.0, existing=[])


def test_start_strategies() -> None:
    analysis = make_analysis()
    downbeats = analysis["downbeats"]
    seg = plan(settings=settings(count=3, start_strategy="segment"), segment=(10.0, 25.0))
    assert [v["start"] for v in seg["videos"]] == [10.0, 10.0, 10.0]

    chorus = plan(settings=settings(count=2, start_strategy="chorus"), segment=None)
    assert all(abs(v["start"] - 24.0) <= 0.5 for v in chorus["videos"])

    varied = plan(settings=settings(count=6, start_strategy="varied"), segment=(10.0, 25.0))
    starts = [v["start"] for v in varied["videos"]]
    assert len(set(starts)) >= 3
    assert all(min(abs(s - d) for d in downbeats) < 1e-6 for s in starts if s != 10.0)

    custom = plan(settings=settings(count=2, start_strategy="custom", start_sec=12.5))
    assert [v["start"] for v in custom["videos"]] == [12.5, 12.5]
    clamped = plan(settings=settings(count=1, start_strategy="custom", start_sec=58.0))
    assert clamped["videos"][0]["start"] + 15 <= 60 + 1e-6
    assert any("調整" in w for w in clamped["warnings"])

    near_end = plan(settings=settings(count=1, start_strategy="segment"), segment=(55.0, 60.0))
    assert near_end["videos"][0]["start"] <= 45.0 and any("調整" in w for w in near_end["warnings"])


def _demo_like_analysis(*, vocals: bool) -> dict[str, Any]:
    """Demo song layout with the landmarks that used to leak quiet starts into 'varied'."""
    analysis = make_analysis()
    analysis["suggested_segments"] = [
        {"start": 24.0, "end": 39.0, "duration": 15, "score": 0.9, "reason": "サビ"},
        {"start": 8.0, "end": 38.0, "duration": 30, "score": 0.8, "reason": "Aメロ〜サビ"},
        {"start": 0.0, "end": 60.0, "duration": 60, "score": 0.7, "reason": "曲全体"},  # the intro
    ]
    analysis["highlights"] = [{"start": 24.0, "end": 40.0, "energy": 0.9}, {"start": 44.0, "end": 60.0, "energy": 0.8}]
    if vocals:
        sung = [(8.0, 40.0), (48.0, 56.0)]
        values = [0.9 if any(a <= i * 0.1 < b for a, b in sung) else 0.05 for i in range(600)]
        analysis["vocal_activity"] = {"hop": 0.1, "values": values}
    return analysis


VARIED_LYRICS = [
    {"id": f"l{i + 1:03d}", "text": "歌詞", "start": float(t), "end": float(t + 4)}
    for i, t in enumerate([8, 12, 16, 20, 24, 28, 32, 36, 48, 52])
]


@pytest.mark.parametrize("source", ["lyrics", "vocal_activity"])
def test_varied_starts_avoid_quiet_intro_and_bridge(source: str) -> None:
    analysis = _demo_like_analysis(vocals=source == "vocal_activity")
    lyrics = VARIED_LYRICS if source == "lyrics" else []
    result = plan(analysis=analysis, lyric_lines=lyrics, segment=None,
                  settings=settings(count=30, duration_sec=15, start_strategy="varied"))  # fmt: skip
    starts = [v["start"] for v in result["videos"]]
    assert len(set(starts)) >= 2, starts
    assert all(s >= 8.0 - 0.35 and not 40.0 - 0.35 <= s < 46.0 for s in starts), sorted(set(starts))
    # Best-scoring start (the chorus: lyrics + highest energy) comes first.
    assert abs(starts[0] - 24.0) <= 0.35


def test_varied_without_vocal_data_and_with_a_user_segment() -> None:
    analysis = _demo_like_analysis(vocals=False)
    instrumental = plan(analysis=analysis, lyric_lines=[], segment=None,
                        settings=settings(count=6, duration_sec=15, start_strategy="varied"))  # fmt: skip
    assert all(not 0 <= v["start"] < 6.0 for v in instrumental["videos"])  # still no intro opening
    # The user's own segment is always used first, even if it is quiet.
    pinned = plan(analysis=analysis, lyric_lines=VARIED_LYRICS, segment=(0.0, 15.0),
                  settings=settings(count=4, duration_sec=15, start_strategy="varied"))  # fmt: skip
    starts = [v["start"] for v in pinned["videos"]]
    assert starts[0] < 0.5 and all(s >= 8.0 - 0.35 for s in starts[1:])


def test_song_shorter_than_duration() -> None:
    analysis = make_analysis(duration=9.6, bpm=120)
    conf = settings(count=3, duration_sec=15)
    result = plan(analysis=analysis, settings=conf, segment=None)
    assert all(v["duration"] == 9.6 and v["start"] == 0.0 for v in result["videos"])
    assert any("短いため" in w for w in result["warnings"])
    check_plan(result, analysis=analysis, conf=conf, clip_duration=5.0, existing=[])


@pytest.mark.parametrize("song", [1.0, 2.2, 3.4])
def test_very_short_songs(song: float) -> None:
    analysis = make_analysis(duration=song, bpm=120)
    conf = settings(count=2, duration_sec=15)
    result = plan(analysis=analysis, settings=conf, segment=None)
    check_plan(result, analysis=analysis, conf=conf, clip_duration=5.0, existing=[])
    assert all(len(v["shots"]) == 1 for v in result["videos"])


def test_analysis_without_beats_uses_bpm_grid() -> None:
    analysis = {"duration": 40.0, "bpm": 100.0}
    result = plan(analysis=analysis, settings=settings(count=2), segment=None)
    grid = 0.6
    for video in result["videos"]:
        t = video["start"]
        for shot in video["shots"][:-1]:
            t += shot["duration"]
            assert abs(t / grid - round(t / grid)) < 0.01


def test_missing_analysis_is_rejected() -> None:
    with pytest.raises(ServiceError):
        plan(analysis={})


# --- look ---------------------------------------------------------------------------------------------------


def test_auto_color_grade_rotates_preset_grades() -> None:
    result = plan(
        settings=settings(count=8, preset="urban_night", color_grade="auto"), storyboard=storyboard("urban_night")
    )
    grades = [v["color_grade"] for v in result["videos"]]
    assert set(grades) == set(WORLD_PRESETS["urban_night"]["color_grades"])
    assert all(a != b for a, b in itertools.pairwise(grades))
    fixed = plan(settings=settings(count=3, color_grade="mono_noir"))
    assert {v["color_grade"] for v in fixed["videos"]} == {"mono_noir"}
    assert set(grades) <= set(COLOR_GRADES)


def test_lyric_style_text_style_and_transitions() -> None:
    result = plan(settings=settings(count=12, preset="dreamy", lyric_style="karaoke",
                                    text_style={"font_size": 72, "position": "center"}),
                  storyboard=storyboard("dreamy"))  # fmt: skip
    for video in result["videos"]:
        assert video["lyric_style"] == "karaoke"
        assert video["text_style"] == {"font_size": 72, "position": "center"}
    assert {v["transition"] for v in result["videos"]} == {"cut", "crossfade"}
    assert not any(s["hflip"] for v in result["videos"] for s in v["shots"])  # mirroring is opt-in
    pans = {s["pan"] for v in result["videos"] for s in v["shots"]}
    assert len(pans) >= 4
    for shot in (s for v in result["videos"] for s in v["shots"]):
        if shot["pan"] != "center":
            assert min(shot["zoom_from"], shot["zoom_to"]) >= 1.05, "pans need zoom headroom"


def test_auto_lyric_style_rotates_preset_styles() -> None:
    result = plan(settings=settings(count=3, lyric_style="auto", preset="cinematic"))
    assert [v["lyric_style"] for v in result["videos"]] == WORLD_PRESETS["cinematic"]["lyric_styles"]
