"""No repeated footage inside one video (ARCHITECTURE.md §6.4): fitting a video's cuts to its clip pool.

A generated video whose beat cuts outnumber the clips it has is re-cut on beats into fewer, longer
shots (≤ 8 s at ≥ 0.75x, crossfade frames reserved) instead of showing a clip twice. Only when even
that cannot fill the video does a clip come back (never adjacent, punch-in framing, counted and
warned about). Fixtures and the invariant checker come from ``test_variation``.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import time
from typing import Any

import pytest
from test_variation import REUSES, check_plan, make_analysis, plan, settings, storyboard

from app.services import render, variation
from app.services.variation import fit_clip_floor, make_cache_key, pool_target_size

# The 8 samples: OpenRouter veo-3.1-lite (6 s clips), one 16–22 s video, economy, at most 3 new clips.
SAMPLE: dict[str, Any] = {
    "provider": "openrouter", "model": "google/veo-3.1-lite", "count": 1, "duration_sec": 20, "reuse": "economy",
    "max_new_clips": 3, "start_strategy": "custom", "start_sec": 10,
}  # fmt: skip
CLIP_SEC = 6.0
USABLE_SEC = CLIP_SEC - 2 * 0.1  # generated clips lose 0.1 s at each end
INNER_PAD, EDGE_PAD = 0.34, 0.23  # crossfade footage a fitted shot reserves (§3 _shot_pads)


def _analysis() -> dict[str, Any]:
    return make_analysis(duration=120, bpm=72, seed=1)


def _sample(**overrides: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(analysis, settings, plan) for the sample settings: ``conf`` overrides settings, the rest go to plan()."""
    analysis = _analysis()
    conf = settings(**{**SAMPLE, **overrides.pop("conf", {})})
    result = plan(analysis=analysis, settings=conf, provider_clip_duration=CLIP_SEC, **overrides)
    return analysis, conf, result


def _refs(video: dict[str, Any]) -> list[str]:
    return [str(s["clip_key"] or s["clip_id"]) for s in video["shots"]]


def _move(shot: dict[str, Any]) -> tuple[str, int]:
    return shot["pan"], 1 if shot["zoom_to"] > shot["zoom_from"] else -1


def _pads(video: dict[str, Any]) -> list[float]:
    n = len(video["shots"])
    if not video["fit"]["fitted"] or video["transition"] != "crossfade" or n <= 1:
        return [0.0] * n
    return [EDGE_PAD] + [INNER_PAD] * (n - 2) + [EDGE_PAD]


def _pace(fit: dict[str, Any]) -> str:
    slowest = fit["min_speed"]
    return f"最長{fit['max_shot_sec']:.1f}秒・" + ("等速" if slowest >= 0.995 else f"最も遅いショット{slowest:.2f}倍速")


def _beat_cuts(analysis: dict[str, Any], video: dict[str, Any], seed: int) -> list[int]:
    """The video's natural beat cuts (ms), drawn exactly as plan_batch draws them before any fitting."""
    song = variation._build_song(analysis, [])
    rng = random.Random(variation._stable_int("video", seed, video["index"]))
    return variation._cut_points(song, video["start"], round(video["duration"] * 1000), rng)


def _need_clips(duration_sec: float, clip_sec: float) -> int:
    """Clips one video needs with every shot as long as a clip allows (≤ 8 s at ≥ 0.75x, inner pad)."""
    limit_ms = min(8000, int(((clip_sec - 0.2) / 0.75 - INNER_PAD) * 1000))
    return math.ceil(duration_sec * 1000 / limit_ms)


def _assert_natural_speed(video: dict[str, Any], lengths: dict[str, float]) -> None:
    """A fitted shot plays as fast as its footage allows: no stylistic slow-down on top."""
    for shot, pad in zip(video["shots"], _pads(video), strict=True):
        usable = lengths[str(shot["clip_key"] or shot["clip_id"])] - 0.2
        assert shot["speed"] == pytest.approx(min(1.0, usable / (shot["duration"] + pad)), abs=0.0011), shot


# --- the sample settings --------------------------------------------------------------------------


def test_sample_settings_use_each_clip_once() -> None:
    # Regression: 3 clips for 5–7 beat shots brought every clip back 2–4 times in the 8 samples.
    analysis, conf, result = _sample()
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])
    (video,) = result["videos"]
    assert result["stats"]["new_clips"] == 3
    assert len(video["shots"]) == 3 and len(set(_refs(video))) == 3
    assert all(s["duration"] <= 8.0 and s["speed"] >= 0.75 for s in video["shots"])
    fit = video["fit"]
    assert fit["fitted"] and fit["repeat_shots"] == 0 and fit["shots"] == 3
    assert fit["beat_shots"] == len(_beat_cuts(analysis, video, seed=1)) > 3
    _assert_natural_speed(video, {s["key"]: s["duration"] for s in result["clip_specs"]})
    assert result["warnings"] == [
        "新規生成の上限（3本）により、素材数を3本に抑えました",
        f"素材が3本のため、同じ映像を繰り返さないよう、ショットを{fit['beat_shots']}→3にまとめました（{_pace(fit)}）",
    ]
    stats = result["stats"]
    assert stats["repeat_shots"] == 0 and stats["repeat_warning"] is None and stats["fitted_videos"] == 1
    assert stats["shots_total"] == 3 and stats["beat_shots_per_video"] == [fit["beat_shots"]]


def _brute_force_longest_ms(analysis: dict[str, Any], video: dict[str, Any], n: int, cap_ms: int) -> int:
    """The shortest possible longest shot when the video is split on beats into ``n`` shots (1.5 s ≤ shot ≤ cap)."""
    duration_ms = round(video["duration"] * 1000)
    beats = sorted({round((b - video["start"]) * 1000) for b in analysis["beats"]
                    if 0 < (b - video["start"]) * 1000 < duration_ms})  # fmt: skip
    best = None
    for inner in itertools.combinations(beats, n - 1):
        bounds = [0, *inner, duration_ms]
        lengths = [b - a for a, b in itertools.pairwise(bounds)]
        if all(1500 <= x <= cap_ms for x in lengths):
            best = max(lengths) if best is None else min(best, max(lengths))
    assert best is not None
    return best


@pytest.mark.parametrize("duration_sec", [16.5, 19.6, 20, 21.9])
def test_fitted_cuts_are_as_even_as_the_beat_grid_allows(duration_sec: float) -> None:
    analysis, _, result = _sample(conf={"duration_sec": duration_sec})
    (video,) = result["videos"]
    assert len(video["shots"]) == 3
    assert video["fit"]["fitted"] and video["transition"] == "cut"
    cap_ms = min(8000, int(USABLE_SEC / variation.FIT_MIN_SPEED * 1000))
    best = _brute_force_longest_ms(analysis, video, len(video["shots"]), cap_ms)
    longest = max(round(s["duration"] * 1000) for s in video["shots"])
    assert best <= longest <= best * 1.01 + 2, (best, [s["duration"] for s in video["shots"]])


@pytest.mark.parametrize("duration_sec", [16.5, 19.6, 21.9])
@pytest.mark.parametrize("seed", [101, 202])
def test_free_re_edit_from_three_ready_clips(duration_sec: float, seed: int) -> None:
    # A sample's video re-planned with its 3 ready clips and nothing new (the per-video re-edit).
    _, _, first = _sample()
    existing = [
        {"id": f"clip{i}", "scene_id": s["scene_id"], "cache_key": s["cache_key"], "duration": CLIP_SEC,
         "source": "openrouter", "status": "ready"}
        for i, s in enumerate(first["clip_specs"])
    ]  # fmt: skip
    analysis, conf, result = _sample(conf={"max_new_clips": 0, "duration_sec": duration_sec},
                                     existing_clips=existing, seed=seed)  # fmt: skip
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=existing)
    (video,) = result["videos"]
    ids = [s["clip_id"] for s in video["shots"]]
    assert result["stats"]["new_clips"] == 0
    assert set(ids) <= {c["id"] for c in existing} and len(ids) == len(set(ids)) == 3
    assert video["fit"]["fitted"] and video["fit"]["repeat_shots"] == 0
    assert all(s["speed"] >= variation.FIT_MIN_SPEED for s in video["shots"])
    by_id = {c["id"]: c["duration"] for c in existing}
    _assert_natural_speed(video, {s["key"]: by_id[s["existing_clip_id"]] for s in result["clip_specs"]})
    assert "生成済みの素材3本だけで構成します" in result["warnings"]
    assert not any("上限（0本）" in w for w in result["warnings"])
    assert result["stats"]["repeat_warning"] is None


# --- last resort ----------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 3])
def test_last_resort_repeats_are_few_apart_and_reframed(count: int) -> None:
    # Cap 2 for a 20 s video: 2 clips cover at most 2 × 7.7 s at 0.75x, so one clip must come back.
    analysis, conf, result = _sample(conf={"max_new_clips": 2, "count": count})
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])
    for video in result["videos"]:
        refs = _refs(video)
        assert len(set(refs)) == 2
        assert len(refs) == 3 and video["fit"]["repeat_shots"] == 1, "the fewest extra shots: one repeat"
        assert all(a != b for a, b in itertools.pairwise(refs)), "a repeat is never adjacent"
        assert all(s["speed"] >= variation.FIT_MIN_SPEED for s in video["shots"])
        seen: dict[str, list[tuple[str, int]]] = {}
        for shot in video["shots"]:
            earlier = seen.setdefault(str(shot["clip_key"]), [])
            if earlier:  # punch-in framing with a camera move this clip has not had in the video
                zooms = (shot["zoom_from"], shot["zoom_to"])
                assert 1.12 <= min(zooms) <= max(zooms) <= 1.19, shot
                assert _move(shot) not in earlier, (shot, earlier)
            earlier.append(_move(shot))
    stats = result["stats"]
    assert stats["repeat_shots"] == stats["videos_with_repeats"] == count
    where = "動画内で同じ素材が1ショット" if count == 1 else f"{count}本の動画で同じ素材が計{count}ショット"
    need = _need_clips(20, CLIP_SEC)
    assert need == 3
    assert stats["repeat_warning"] == (
        f"素材が足りないため、{where}再登場します（連続はせず、別の区間・寄りの構図で使います）。"
        f"新規生成の上限を{max(2 + 1, need)}本以上に引き上げると解消します"
    )
    assert "再登場" in stats["repeat_warning"] and "上限を" in stats["repeat_warning"]
    assert not any("まとめました" in w for w in result["warnings"]), "only repeat-free fitted videos are announced"


def test_without_a_beat_solution_the_beat_cuts_stay_and_the_plan_never_raises() -> None:
    # One 2 s clip (≤ 2.4 s per shot at 0.75x) cannot fill 20 s with fewer shots than the beat cuts: they stay.
    analysis = _analysis()
    conf = settings(**{**SAMPLE, "max_new_clips": 1})
    result = plan(analysis=analysis, settings=conf, provider_clip_duration=2.0)
    check_plan(result, analysis=analysis, conf=conf, clip_duration=2.0, existing=[])
    (video,) = result["videos"]
    durations_ms = [round(s["duration"] * 1000) for s in video["shots"]]
    assert not video["fit"]["fitted"] and durations_ms == _beat_cuts(analysis, video, seed=1)
    assert len(set(_refs(video))) == 1 and video["fit"]["repeat_shots"] == len(durations_ms) - 1
    need = _need_clips(20, 2.0)
    assert result["stats"]["repeat_warning"].endswith(f"新規生成の上限を{max(2, need)}本以上に引き上げると解消します")


def test_too_few_ready_clips_without_new_ones_advise_adding_clips() -> None:
    _, _, first = _sample(conf={"max_new_clips": 2})
    existing = [
        {"id": f"clip{i}", "scene_id": s["scene_id"], "cache_key": s["cache_key"], "duration": CLIP_SEC,
         "source": "openrouter", "status": "ready"}
        for i, s in enumerate(first["clip_specs"])
    ]  # fmt: skip
    analysis, conf, result = _sample(conf={"max_new_clips": 0}, existing_clips=existing, seed=5)
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=existing)
    (video,) = result["videos"]
    assert len(_refs(video)) - len(set(_refs(video))) == 1, "2 clips for 20 s: exactly one comes back"
    assert result["stats"]["new_clips"] == 0 and result["stats"]["repeat_shots"] == 1
    assert "生成済みの素材2本だけで構成します" in result["warnings"]
    assert result["stats"]["repeat_warning"] == (
        "素材が足りないため、動画内で同じ素材が1ショット再登場します（連続はせず、別の区間・寄りの構図で使います）。"
        f"生成済みの素材だけでは足りません。新しい素材を{_need_clips(20, CLIP_SEC) - 2}本以上追加すると解消します"
    )


# --- uncapped and long videos ---------------------------------------------------------------------


@pytest.mark.parametrize("bpm", [72.0, 128.0])
def test_uncapped_plans_never_repeat_a_clip_in_a_video(bpm: float) -> None:
    analysis = make_analysis(duration=120, bpm=bpm, seed=2)
    for reuse, count, n_scenes, duration in itertools.product(REUSES, (1, 3, 10), (3, 8), (15, 30, 60)):
        conf = settings(reuse=reuse, count=count, duration_sec=duration)
        result = plan(analysis=analysis, settings=conf, storyboard=storyboard(n_scenes=n_scenes), segment=(10.0, 70.0))
        check_plan(result, analysis=analysis, conf=conf, clip_duration=5.0, existing=[])
        case = (reuse, count, n_scenes, duration)
        for video in result["videos"]:
            assert len(set(_refs(video))) == len(video["shots"]), case
        assert result["stats"]["repeat_shots"] == 0 and result["stats"]["repeat_warning"] is None, case


def test_long_uncapped_video_gets_enough_clips_to_fit_near_natural_speed() -> None:
    # 60 s from an 8-scene storyboard: one clip per scene (8) would need 0.77x slow motion or repeats.
    analysis = _analysis()
    conf = settings(count=1, reuse="economy", duration_sec=60)
    result = plan(analysis=analysis, settings=conf, provider_clip_duration=CLIP_SEC, segment=(10.0, 70.0))
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])
    (video,) = result["videos"]
    assert len(set(_refs(video))) == len(video["shots"]) and min(s["speed"] for s in video["shots"]) >= 0.8
    stats = result["stats"]
    beat_shots = max(stats["beat_shots_per_video"])
    assert fit_clip_floor(60_000, CLIP_SEC) == stats["pool_fit_floor"] == 10
    assert stats["pool_target"] == min(beat_shots, max(8, fit_clip_floor(60_000, CLIP_SEC))) == 10
    assert stats["new_clips"] == 10 and stats["repeat_shots"] == 0 and stats["min_speed"] >= 0.8


def test_pool_floor_and_crossfade_pads() -> None:
    assert variation.XFADE_PAD_SEC == render.CROSSFADE_SEC
    assert variation._shot_pads(3, True) == pytest.approx([EDGE_PAD, INNER_PAD, EDGE_PAD])
    assert variation._shot_pads(4, False) == [0.0] * 4 and variation._shot_pads(1, True) == [0.0]
    # ceil(D / L), L = (clip − 0.2) / 0.9 − 0.34 s clamped to 1.5–8 s.
    assert fit_clip_floor(60_000, 6.0) == 10 and fit_clip_floor(20_000, 6.0) == 4
    assert fit_clip_floor(60_000, 10.0) == math.ceil(60 / 8) and fit_clip_floor(10_000, 1.0) == math.ceil(10 / 1.5)
    # The floor only raises the pool between the scene count and the shots of one video.
    assert pool_target_size("economy", 13, 13, 8) == 8
    assert pool_target_size("economy", 13, 13, 8, 10) == 10
    assert pool_target_size("economy", 6, 6, 8, 10) == 6
    assert pool_target_size("economy", 13, 13, 12, 10) == 12


# --- distinct scenes ------------------------------------------------------------------------------


def test_small_pool_shows_distinct_scenes_and_keeps_cached_takes() -> None:
    # Only scene 1 matches the chorus: by weight it would take every slot of a 3-clip pool as alternate takes.
    board = storyboard()
    for index, scene in enumerate(board["scenes"]):
        scene["section"] = "chorus" if index == 0 else "verse"
    conf = settings(count=1, max_new_clips=3)
    result = plan(settings=conf, storyboard=board, segment=(24.0, 39.0))
    check_plan(result, analysis=make_analysis(), conf=conf, clip_duration=5.0, existing=[])
    specs = result["clip_specs"]
    assert len(specs) == 3 and len({s["scene_id"] for s in specs}) == 3
    assert not any("Alternate take" in s["prompt"] for s in specs)
    assert result["stats"]["scene_repeat_shots"] == 0

    # A cached alternate take of that scene keeps its place: it is used, for free.
    chorus = board["scenes"][0]
    prompt = variation._variant_prompt(chorus["prompt_en"], 1)
    negative = chorus.get("negative_prompt_en") or ""
    key = make_cache_key("mock", "mock-v1", prompt, negative, 5.0, "9:16",
                         variation._stable_int("clip", prompt, negative, 1, ""))  # fmt: skip
    existing = [{"id": "cached-take", "scene_id": chorus["id"], "cache_key": key, "duration": 5.0, "source": "mock",
                 "status": "ready"}]  # fmt: skip
    cached = plan(settings=conf, storyboard=board, segment=(24.0, 39.0), existing_clips=existing)
    check_plan(cached, analysis=make_analysis(), conf=conf, clip_duration=5.0, existing=existing)
    assert any(s["existing_clip_id"] == "cached-take" for s in cached["clip_specs"])
    assert "cached-take" in {s["clip_id"] for s in cached["videos"][0]["shots"]}
    assert cached["stats"]["new_clips"] <= 3 and cached["stats"]["reused_clips"] == 1


# --- crossfades and the renderer ------------------------------------------------------------------


def _render_plans(result: dict[str, Any], video: dict[str, Any]) -> tuple[list[Any], int]:
    """What render._plan_shots makes of a planned video (clips as long as planned)."""
    specs = {s["key"]: s for s in result["clip_specs"]}
    shots = [
        render.RenderShot(clip_path=s["clip_key"], src_in=s["src_in"], duration=s["duration"], speed=s["speed"],
                          pan=s["pan"], zoom_from=s["zoom_from"], zoom_to=s["zoom_to"])
        for s in video["shots"]
    ]  # fmt: skip
    clips = {
        key: render._ClipInfo(path=key, duration=spec["duration"], width=720, height=1280, color_matrix="bt709")
        for key, spec in specs.items()
    }
    spec = render.RenderSpec(
        shots=shots, audio_path="", audio_start=0.0, duration=video["duration"], lyric_lines=[], lyric_style="simple",
        text_style={}, color_grade="natural", output_path="", work_dir="", transition=video["transition"],
    )  # fmt: skip
    return render._plan_shots(spec, clips)


def _drawn_crossfade(seed: int, index: int = 0) -> bool:
    """The soft-preset transition draw of plan_batch (before the pool, from the video-look value)."""
    return random.Random(variation._stable_int("video-look", seed, index)).random() < 0.5


def test_fitted_crossfade_shots_are_not_slowed_or_looped_by_the_renderer() -> None:
    analysis = _analysis()
    found = 0
    for seed in range(201):
        conf = settings(preset="dreamy", count=1, max_new_clips=3, duration_sec=20)
        result = plan(analysis=analysis, settings=conf, storyboard=storyboard("dreamy"),
                      provider_clip_duration=CLIP_SEC, segment=(10.0, 30.0), seed=seed)  # fmt: skip
        (video,) = result["videos"]
        fit = video.get("fit") or {}
        if not (fit.get("fitted") and fit["repeat_shots"] == 0 and video["transition"] == "crossfade"):
            continue
        found += 1
        check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])
        _assert_natural_speed(video, {s["key"]: s["duration"] for s in result["clip_specs"]})
        plans, xfade = _render_plans(result, video)
        assert xfade > 0
        for planned, shot in zip(plans, video["shots"], strict=True):
            assert not planned.loop, shot
            assert planned.speed == pytest.approx(shot["speed"], abs=1e-9), "the renderer slowed a fitted shot"
            assert planned.src_in == pytest.approx(shot["src_in"], abs=1e-9), "the renderer moved src_in"
        if found >= 6:
            break
    assert found


def test_crossfade_that_only_fits_without_padding_becomes_a_cut() -> None:
    # 22.5 s from 3 clips: padded shots cover at most 7.50 + 7.39 + 7.50 = 22.4 s, unpadded 3 × 7.73 s.
    analysis = _analysis()
    seeds = [seed for seed in range(40) if _drawn_crossfade(seed)][:4]
    assert seeds
    for seed in seeds:
        conf = settings(**{**SAMPLE, "preset": "dreamy", "duration_sec": 22.5})
        result = plan(analysis=analysis, settings=conf, storyboard=storyboard("dreamy"),
                      provider_clip_duration=CLIP_SEC, seed=seed)  # fmt: skip
        check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])
        (video,) = result["videos"]
        assert video["transition"] == "cut", "a cut beats a repeat"
        assert video["fit"]["fitted"] and video["fit"]["repeat_shots"] == 0 and len(video["shots"]) == 3
        plans, xfade = _render_plans(result, video)
        assert xfade == 0
        for planned, shot in zip(plans, video["shots"], strict=True):
            assert not planned.loop and planned.speed == pytest.approx(shot["speed"], abs=1e-9)
            assert planned.src_in == pytest.approx(shot["src_in"], abs=1e-9)


# --- determinism ----------------------------------------------------------------------------------


def test_plans_are_deterministic_and_videos_with_a_shared_start_differ() -> None:
    conf = settings(count=3, start_strategy="segment", max_new_clips=3)
    first = plan(settings=conf)
    assert first == plan(settings=conf)
    assert len({v["start"] for v in first["videos"]}) == 1
    shot_lists = [json.dumps(v["shots"], sort_keys=True) for v in first["videos"]]
    assert len(set(shot_lists)) == len(shot_lists)
    assert plan(settings=conf, seed=2) != first
    assert all(len(set(_refs(v))) == len(v["shots"]) for v in first["videos"])
    assert all(v["fit"]["fitted"] for v in first["videos"]) and first["stats"]["repeat_shots"] == 0


@pytest.mark.xfail(
    strict=True,
    reason="R8 promises different cuts for videos that share a start, but the minimax fit (R2, §3 step 4: 1 % "
    "tolerance, slow motion outweighs the per-video weights) gives every fitted video the same cuts, and with "
    "no source slack in a 6 s clip only the clip order can differ: check_plan's distinct-signature rule fails",
)
def test_fitted_videos_with_a_shared_start_are_different_edits() -> None:
    analysis, conf, result = _sample(conf={"count": 3})
    assert len({v["start"] for v in result["videos"]}) == 1
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])


@pytest.mark.parametrize("cap", [None, 3])
def test_transition_is_drawn_before_the_pool_from_the_video_look_value(cap: int | None) -> None:
    analysis = _analysis()
    conf = settings(count=12, preset="dreamy", max_new_clips=cap, duration_sec=20, start_strategy="varied")
    result = plan(analysis=analysis, settings=conf, storyboard=storyboard("dreamy"), provider_clip_duration=CLIP_SEC,
                  segment=(10.0, 30.0), seed=3)  # fmt: skip
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])
    for video in result["videos"]:
        drawn = "crossfade" if _drawn_crossfade(3, video["index"]) else "cut"
        assert video["transition"] == drawn or (drawn == "crossfade" and video.get("fit", {}).get("fitted"))
    assert {v["transition"] for v in result["videos"]} == {"cut", "crossfade"}
    assert sum(v["fit"]["fitted"] for v in result["videos"]) == (12 if cap else 0)


def test_many_videos_from_three_clips_are_all_fitted_without_repeats() -> None:
    analysis, conf, result = _sample(conf={"count": 10, "start_strategy": "varied"})
    check_plan(result, analysis=analysis, conf=conf, clip_duration=CLIP_SEC, existing=[])
    stats = result["stats"]
    assert stats["new_clips"] == 3 and stats["shots_per_video"] == [3] * 10
    assert stats["fitted_videos"] == 10 and stats["repeat_shots"] == 0 and stats["min_speed"] >= variation.FIT_MIN_SPEED
    pace = _pace({"max_shot_sec": stats["max_shot_sec"], "min_speed": stats["min_speed"]})
    fitted = f"素材が3本のため、同じ映像を繰り返さないよう、10本の動画でショットを長めにまとめました（{pace}）"
    assert fitted in result["warnings"]
    # Cross-video variety stays low with 3 clips for 10 videos, and the warning still says so.
    assert stats["variety_score"] < variation.VARIETY_WARNING_THRESHOLD and stats["variety_warning"]


# --- library --------------------------------------------------------------------------------------


def _upload(clip_id: str, duration: float) -> dict[str, Any]:
    return {"id": clip_id, "scene_id": None, "cache_key": None, "duration": duration, "source": "upload"}


def test_library_videos_keep_their_beat_cuts_and_never_raise() -> None:
    analysis = make_analysis()
    uploads = [_upload("u0", 6.0), _upload("u1", 8.0), _upload("u2", 5.0)]
    conf = settings(provider="library", model=None, count=3, duration_sec=15)
    result = plan(settings=conf, existing_clips=uploads, storyboard=None)
    check_plan(result, analysis=analysis, conf=conf, clip_duration=5.0, existing=uploads)
    for video in result["videos"]:
        assert [round(s["duration"] * 1000) for s in video["shots"]] == _beat_cuts(analysis, video, seed=1)
    assert result["stats"]["fitted_videos"] == 0 and result["stats"]["pool_fit_floor"] == 0

    # One short upload: a video shorter than two shots is one shot; a longer one reuses the upload
    # (its beat cuts are never re-cut) and an overlapping window is counted and warned about.
    single = [_upload("u0", 6.0)]
    for duration in (2.5, 5.0):
        conf = settings(provider="library", model=None, count=1, duration_sec=duration)
        result = plan(settings=conf, existing_clips=single, storyboard=None, segment=(24.0, 24.0 + duration))
        check_plan(result, analysis=analysis, conf=conf, clip_duration=5.0, existing=single)
        (video,) = result["videos"]
        assert {s["clip_id"] for s in video["shots"]} == {"u0"} and not video["fit"]["fitted"]
        assert [round(s["duration"] * 1000) for s in video["shots"]] == _beat_cuts(analysis, video, seed=1)
        repeats = video["fit"]["repeat_shots"]
        if duration == 2.5:
            assert len(video["shots"]) == 1 and repeats == 0
        assert result["stats"]["repeat_warning"] == (
            f"アップロード素材が足りないため、同じ映像が{repeats}ショット再登場します。素材を追加でアップロードすると解消します"
            if repeats
            else None
        )


def test_long_upload_gives_disjoint_windows() -> None:
    long_upload = [_upload("long", 40.0)]
    conf = settings(provider="library", model=None, count=1, duration_sec=15)
    result = plan(settings=conf, existing_clips=long_upload, storyboard=None)
    check_plan(result, analysis=make_analysis(), conf=conf, clip_duration=5.0, existing=long_upload)
    (video,) = result["videos"]
    assert len(video["shots"]) >= 3
    windows = sorted((s["src_in"], s["src_in"] + s["duration"] * s["speed"]) for s in video["shots"])
    for (_, end), (start, _) in itertools.pairwise(windows):
        assert start >= end + 0.5 - 1e-9, windows  # REPEAT_GAP_SEC apart
    assert variation.REPEAT_GAP_SEC == 0.5
    assert video["fit"]["repeat_shots"] == 0 and result["stats"]["repeat_warning"] is None


# --- speed ----------------------------------------------------------------------------------------


def test_planning_is_fast_and_prompts_are_memoised() -> None:
    _sample()  # warm-up
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        _sample()
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.2, timings
    assert hasattr(variation._variant_prompt, "cache_info")
