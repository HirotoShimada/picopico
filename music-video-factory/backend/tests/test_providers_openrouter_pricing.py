"""OpenRouter ``pricing_skus`` against the real ``GET /videos/models`` listing (2026-10-13).

``fixtures/openrouter_videos_models_2026-10-13.json`` is the listing as fetched from openrouter.ai
(30 models). The table below is the price this app must derive for each of them (text-to-video,
silent when the model allows it, 720p or the model's default resolution), straight from their SKUs.
Generation itself is still respx-only: no paid request was ever sent.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.services import cost
from app.services.providers.base import ClipRequest, ProviderModel
from app.services.providers.openrouter import (
    FALLBACK_MODELS,
    STATIC_PRICES,
    Catalogue,
    OpenRouterProvider,
    _parse_catalogue,
    price_from_skus,
)

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_videos_models_2026-10-13.json"
KEY = "or-test-key-not-real"
BASE = "https://openrouter.ai/api/v1"
MODELS_URL = f"{BASE}/videos/models"
SUBMIT_URL = f"{BASE}/videos"
JOB = "gen-vid-1791849600-pricing000000000001"
POLL_URL = f"{BASE}/videos/{JOB}"
CONTENT_URL = f"{BASE}/videos/{JOB}/content"
FETCHED_AT = 1_791_849_600.0  # 2026-10-13T00:00:00Z

# id → (USD/s, resolution requested, SKU used, USD/s of "<id>@1080p" or None, its SKU)
PRICED: dict[str, tuple[float, str, str, float | None, str | None]] = {
    "google/veo-3.1-lite": (0.03, "720p", "duration_seconds_without_audio_720p", None, None),
    "google/veo-3.1-fast": (0.08, "720p", "duration_seconds_without_audio_720p", None, None),
    "google/veo-3.1": (0.20, "720p", "duration_seconds_without_audio", None, None),
    "kwaivgi/kling-v3.0-std": (0.084, "720p", "text_to_video_duration_seconds_720p", None, None),
    "kwaivgi/kling-v3.0-pro": (0.112, "720p", "text_to_video_duration_seconds_720p", None, None),
    "kwaivgi/kling-video-o1": (0.112, "720p", "duration_seconds", None, None),
    "runway/gen-4.5": (0.12, "720p", "cents_per_second_output", None, None),
    "x-ai/grok-imagine-video": (0.07, "720p", "cents_per_video_output_second_720p", None, None),
    "x-ai/grok-imagine-video-1.5": (0.14, "720p", "cents_per_video_output_second_720p",
                                    0.25, "cents_per_video_output_second_1080p"),
    "x-ai/grok-imagine-video-1.5-lite": (0.03, "720p", "cents_per_video_output_second_720p",
                                         0.14, "cents_per_video_output_second_1080p"),
    "black-forest-labs/flux-3-video": (0.17, "720p", "cents_per_second_output_720p",
                                       0.29, "cents_per_second_output_1080p"),
    "heygen/heygen-video-1": (0.03, "768p", "duration_seconds_768p", None, None),
    "minimax/hailuo-3-max": (0.08, "768p", "duration_seconds_768p", None, None),
    "minimax/hailuo-3": (0.13, "2K", "duration_seconds", None, None),
    "alibaba/wan-2.6": (0.08, "720p", "text_to_video_duration_seconds_720p",
                        0.12, "text_to_video_duration_seconds_1080p"),
    "alibaba/wan-2.7": (0.10, "720p", "duration_seconds", None, None),
    "alibaba/wan-3.0": (0.10, "720p", "duration_seconds_720p", 0.20, "duration_seconds_1080p"),
    "alibaba/wan-3.0-prime": (0.14, "720p", "duration_seconds_720p", 0.28, "duration_seconds_1080p"),
    "alibaba/happyhorse-1.0": (0.0988, "720p", "duration_seconds_720p", 0.1694, "duration_seconds_1080p"),
    "alibaba/happyhorse-1.1": (0.0988, "720p", "duration_seconds_720p", 0.1278, "duration_seconds_1080p"),
}  # fmt: skip
# Listed (durations + 9:16) but priced per token: no per-second price is guessed, so they cannot be used.
TOKEN_PRICED = {
    "bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "bytedance/seedance-2.0-mini", "bytedance/seedance-2.5",
    "bytedance/seedance-1-5-pro",
}  # fmt: skip
NOT_OFFERED = {
    "minimax/hailuo-2.3",  # 16:9 only
    "black-forest-labs/flux-video-edit",  # edit model: no durations, no aspect ratios
    "black-forest-labs/flux-video-upscale",  # upscaler
    "runway/aleph-2",  # video-to-video: no durations
    "heygen/avatar-iv",  # avatar model: no durations
}


def _fixture_entries() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]


def _parse(entries: list[dict[str, Any]], source: str = "live") -> list[ProviderModel]:
    return _parse_catalogue(Catalogue(tuple(entries), source, FETCHED_AT if source != "fallback" else None))


def _entry(model_id: str = "vendor/model", **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": model_id, "name": "Model", "supported_durations": [4, 6, 8],
                             "supported_aspect_ratios": ["16:9", "9:16"], "supported_resolutions": ["720p", "1080p"],
                             "supported_frame_images": ["first_frame"], "generate_audio": True, "seed": True,
                             "pricing_skus": {"duration_seconds": "0.05"}}  # fmt: skip
    entry.update(overrides)
    return entry


# --- the real listing -----------------------------------------------------------------------------------------------


def test_the_fixture_is_the_full_real_listing() -> None:
    ids = [entry["id"] for entry in _fixture_entries()]
    assert len(ids) == len(set(ids)) == 30
    assert set(ids) == set(PRICED) | TOKEN_PRICED | NOT_OFFERED


@respx.mock
def test_real_listing_prices_every_model_from_its_own_skus(set_env: Callable[..., Any]) -> None:
    set_env(OPENROUTER_API_KEY=KEY)
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text("utf-8"))))
    provider = OpenRouterProvider()
    models = {m.id: m for m in provider.models()}

    expected_ids = set(PRICED) | TOKEN_PRICED | {f"{i}@1080p" for i, row in PRICED.items() if row[3] is not None}
    assert set(models) == expected_ids
    assert not NOT_OFFERED & {m.params["api_model"] for m in models.values()}

    actual = {}
    for model_id in PRICED:
        base = models[model_id]
        hd = models.get(f"{model_id}@1080p")
        actual[model_id] = (base.price_per_second_usd, base.params["resolution"], base.params["price_sku"],
                            hd.price_per_second_usd if hd else None, hd.params["price_sku"] if hd else None)  # fmt: skip
        assert base.price_known and base.supports_vertical and base.verified is False
        assert base.min_clip_usd == 0 and base.params["price_source"] == "listing" and base.as_of == "2026-10-13"
        assert "9:16" in base.params["aspect_ratios"] and base.clip_durations
        if hd is not None:
            assert hd.params["api_model"] == model_id and hd.params["resolution"] == "1080p"
            assert hd.resolution == "1080x1920" and hd.price_known
    assert actual == PRICED

    for model_id in TOKEN_PRICED:
        model = models[model_id]
        assert model.price_known is False and model.params["price_source"] == "unknown"
        assert "トークン単位の料金" in model.price_note and "使用できません" in model.price_note
        usable, reason = provider.check_model(model_id)
        assert usable is False and "料金を確認できない" in (reason or "")

    info = provider.info()
    listed = {m["id"]: m for m in info["models"]}
    assert all(listed[i]["price_per_second_usd"] is None for i in TOKEN_PRICED)
    assert listed["runway/gen-4.5"]["price_per_second_usd"] == 0.12
    assert "5 件は表示していません" in info["models_note"] and "5 件は選べません" in info["models_note"]
    assert info["default_model"] == "google/veo-3.1-lite"


@pytest.mark.parametrize(
    ("model_id", "text"),
    [
        ("runway/gen-4.5", "cents_per_second_output: 12¢ = $0.12/秒"),
        ("x-ai/grok-imagine-video", "cents_per_video_output_second_720p: 7¢ = $0.07/秒"),
        ("black-forest-labs/flux-3-video@1080p", "cents_per_second_output_1080p: 29¢ = $0.29/秒"),
        ("google/veo-3.1-lite", "duration_seconds_without_audio_720p: $0.03/秒"),
        ("alibaba/happyhorse-1.0", "duration_seconds_720p: $0.0988/秒"),
        ("kwaivgi/kling-video-o1", "duration_seconds: $0.112/秒"),
    ],
)
def test_price_notes_show_the_sku_and_its_original_unit(model_id: str, text: str) -> None:
    models = {m.id: m for m in _parse(_fixture_entries())}
    assert text in models[model_id].price_note
    assert "2026-10-13 取得" in models[model_id].price_note and "未検証" in models[model_id].price_note


def test_audio_is_turned_off_and_priced_silent_where_the_listing_allows_it() -> None:
    models = {m.id: m for m in _parse(_fixture_entries())}
    for model_id in ("google/veo-3.1", "kwaivgi/kling-v3.0-std", "black-forest-labs/flux-3-video"):
        assert models[model_id].params["generate_audio"] is True
        assert "generate_audio: false" in models[model_id].price_note
        assert "音声ありの料金" not in models[model_id].price_note
    # Runway Gen-4.5 / Grok: the listing has no audio switch and no audio-dependent price.
    assert "generate_audio" not in models["runway/gen-4.5"].price_note
    assert models["x-ai/grok-imagine-video"].params["generate_audio"] is None


def test_image_to_video_prices_come_from_their_own_skus() -> None:
    models = {m.id: m for m in _parse(_fixture_entries())}
    wan = models["alibaba/wan-2.6"]
    assert wan.image_to_video and wan.params["image_to_video_price_per_second"] == 0.10
    assert wan.params["image_to_video_price_sku"] == "image_to_video_duration_seconds_720p"
    assert "image_to_video_duration_seconds_720p: $0.1/秒" in wan.price_note
    assert models["alibaba/wan-2.6@1080p"].params["image_to_video_price_per_second"] == 0.15
    grok = models["x-ai/grok-imagine-video"]
    assert grok.params["image_to_video_price_per_second"] == 0.07 and grok.params["image_input_usd"] == 0.002
    assert "cents_per_image_input: 0.2¢ = $0.002/枚" in grok.price_note
    # Text-to-video (all the pipeline does) never carries the input-image charge.
    assert grok.extra_clip_usd == 0 and grok.cost_for(5) == pytest.approx(0.35)


def test_built_in_list_is_a_verbatim_copy_of_the_real_listing() -> None:
    real = {entry["id"]: entry for entry in _fixture_entries()}
    for entry in FALLBACK_MODELS:
        for field, value in entry.items():
            assert real[entry["id"]][field] == value, (entry["id"], field)
    assert STATIC_PRICES == {model_id: PRICED[model_id][0] for model_id in STATIC_PRICES}
    assert set(STATIC_PRICES) == {entry["id"] for entry in FALLBACK_MODELS}
    built_in = {m.id: m for m in _parse(list(FALLBACK_MODELS), "fallback")}
    assert all(m.price_known and m.params["price_source"] == "built_in" for m in built_in.values())
    assert "組み込みの料金" in built_in["google/veo-3.1-lite"].price_note
    assert built_in["google/veo-3.1-lite"].as_of == "2026-10-13"


# --- SKU families -----------------------------------------------------------------------------------------------------

VEO_LITE = {"duration_seconds_with_audio": "0.08", "duration_seconds_without_audio": "0.05",
            "duration_seconds_with_audio_720p": "0.05", "duration_seconds_without_audio_720p": "0.03"}  # fmt: skip
KLING = {"duration_seconds": "0.112", "duration_seconds_with_audio": "0.168",
         "text_to_video_duration_seconds_720p": "0.112", "image_to_video_duration_seconds_720p": "0.112"}  # fmt: skip
WAN_26 = {"text_to_video_duration_seconds_480p": "0.04", "text_to_video_duration_seconds_720p": "0.08",
          "image_to_video_duration_seconds_720p": "0.10", "text_to_video_duration_seconds_1080p": "0.12",
          "image_to_video_duration_seconds_1080p": "0.15"}  # fmt: skip
HEYGEN = {"duration_seconds_2k": "0.09", "duration_seconds_480p": "0.02", "duration_seconds_768p": "0.03",
          "reference_duration_seconds_2k": "0.18", "reference_duration_seconds_480p": "0.04",
          "reference_duration_seconds_768p": "0.06"}  # fmt: skip
FLUX = {"cents_per_second_output": "17", "cents_per_second_output_720p": "17", "cents_per_second_output_1080p": "29",
        "cents_per_second_video_continuation_720p": "41", "cents_per_second_video_continuation_1080p": "53"}  # fmt: skip
SEEDANCE = {"video_tokens": "0.000007", "video_tokens_4k": "0.000004", "video_tokens_1080p": "0.0000077",
            "video_tokens_without_audio": "0.000007", "video_tokens_with_video_input": "0.0000043"}  # fmt: skip


@pytest.mark.parametrize(
    ("skus", "resolution", "options", "usd", "sku", "specific"),
    [
        # duration_seconds[_<res>] (USD/s): the requested resolution, else the SKU naming none — never another one
        ({"duration_seconds": "0.08", "duration_seconds_480p": "0.05", "duration_seconds_768p": "0.08"}, "768p", {},
         0.08, "duration_seconds_768p", True),
        ({"duration_seconds": "0.13", "reference_images": "0.04"}, "2K", {}, 0.13, "duration_seconds", False),
        ({"duration_seconds_480p": "0.05", "duration_seconds_1080p": "0.2"}, "720p", {}, None, None, False),
        ({"duration_seconds": "0.1120"}, None, {}, 0.112, "duration_seconds", False),
        # duration_seconds_with_audio / _without_audio[_<res>]: silent when audio can be turned off
        (VEO_LITE, "720p", {}, 0.03, "duration_seconds_without_audio_720p", True),
        (VEO_LITE, "1080p", {}, 0.05, "duration_seconds_without_audio", False),
        ({"duration_seconds_with_audio": "0.40", "duration_seconds_without_audio": "0.20"}, "720p", {}, 0.20,
         "duration_seconds_without_audio", False),
        ({"duration_seconds": "0.084", "duration_seconds_with_audio": "0.126"}, "720p", {}, 0.084,
         "duration_seconds", False),
        ({"duration_seconds_with_audio": "0.40"}, "720p", {}, 0.40, "duration_seconds_with_audio", False),
        # ... and the with-audio price when audio cannot be turned off (no generate_audio switch)
        ({"duration_seconds_with_audio": "0.40", "duration_seconds_without_audio": "0.20"}, "720p",
         {"generate_audio": None}, 0.40, "duration_seconds_with_audio", False),
        ({"duration_seconds_without_audio": "0.20"}, "720p", {"generate_audio": False}, None, None, False),
        # text_to_video_ / image_to_video_duration_seconds_<res>: by mode
        (WAN_26, "720p", {}, 0.08, "text_to_video_duration_seconds_720p", True),
        (WAN_26, "720p", {"image_to_video": True}, 0.10, "image_to_video_duration_seconds_720p", True),
        (WAN_26, "1080p", {}, 0.12, "text_to_video_duration_seconds_1080p", True),
        (WAN_26, "480p", {"image_to_video": True}, None, None, False),
        (KLING, "720p", {}, 0.112, "text_to_video_duration_seconds_720p", True),
        (KLING, "1080p", {}, 0.112, "duration_seconds", False),
        # reference_duration_seconds_<res> / reference_images: reference inputs are never sent → ignored
        (HEYGEN, "768p", {}, 0.03, "duration_seconds_768p", True),
        ({"reference_duration_seconds_768p": "0.06", "reference_images": "0.04"}, "768p", {}, None, None, False),
        # cents_per_second_output[_<res>] / cents_per_video_output_second_<res>: US cents → USD
        ({"cents_per_second_output": "12"}, "720p", {}, 0.12, "cents_per_second_output", False),
        ({"cents_per_video_output_second_480p": "5", "cents_per_video_output_second_720p": "7"}, "720p", {}, 0.07,
         "cents_per_video_output_second_720p", True),
        # cents_per_second_video_continuation_<res>: continuing an earlier video → ignored
        (FLUX, "720p", {}, 0.17, "cents_per_second_output_720p", True),
        (FLUX, "1080p", {}, 0.29, "cents_per_second_output_1080p", True),
        ({"cents_per_second_video_continuation_720p": "41"}, "720p", {}, None, None, False),
        # cents_per_megapixel_second_*: upscalers
        ({"cents_per_megapixel_second_precise": "7.5"}, None, {}, None, None, False),
        # video_tokens*: token pricing is not supported
        (SEEDANCE, "720p", {}, None, None, False),
        ({"video_tokens": "0.000007", "duration_seconds": "0.1"}, "720p", {}, None, None, False),
        # unknown families (incl. the documentation's example names) and bad values are never guessed
        ({"per-video-second": "0.50", "per-video-second-1080p": "0.75"}, "720p", {}, None, None, False),
        ({"duration_seconds_per_frame": "0.01", "audio_seconds": "0.2"}, "720p", {}, None, None, False),
        ({"duration_seconds": "n/a"}, "720p", {}, None, None, False),
        ({"duration_seconds": "-0.1"}, "720p", {}, None, None, False),
        ({"duration_seconds": True}, "720p", {}, None, None, False),
        ({}, "720p", {}, None, None, False),
        (["duration_seconds"], "720p", {}, None, None, False),
    ],
)  # fmt: skip
def test_sku_families(
    skus: Any, resolution: str | None, options: dict[str, Any], usd: float | None, sku: str | None, specific: bool
) -> None:
    price = price_from_skus(skus, resolution, **options)
    assert (price.usd_per_second, price.sku, price.resolution_specific) == (usd, sku, specific)
    assert price.known is (usd is not None)
    if usd is None:
        assert price.problem


def test_unknown_prices_say_why() -> None:
    tokens = price_from_skus(SEEDANCE, "720p")
    assert "video_tokens: $0.000007/トークン" in (tokens.problem or "") and "対応していません" in (tokens.problem or "")
    # A token SKU for video inputs only does not apply; the model is then simply unpriced for this request.
    assert "トークン" not in (price_from_skus({"video_tokens_with_video_input": "0.0000043"}, "720p").problem or "")
    missing = price_from_skus({"duration_seconds_480p": "0.05"}, "720p")
    assert "duration_seconds_480p" in (missing.problem or "") and "720p・テキスト→動画" in (missing.problem or "")
    silent_only = price_from_skus({"duration_seconds_without_audio": "0.2"}, "720p", generate_audio=None)
    assert "音声をオフにする指定" in (silent_only.problem or "")
    assert "pricing_skus" in (price_from_skus(None, "720p").problem or "")


def test_cents_are_converted_exactly_and_shown_in_their_original_unit() -> None:
    for cents, usd in (("12", 0.12), ("7", 0.07), ("17", 0.17), ("29", 0.29), ("14", 0.14), ("2", 0.02)):
        price = price_from_skus({"cents_per_second_output": cents}, None)
        assert price.usd_per_second == usd
    grok = {"cents_per_video_output_second_720p": "7", "cents_per_image_input": "0.2"}
    assert price_from_skus(grok, "720p", image_to_video=True).image_input_usd == 0.002
    assert price_from_skus({"cents_per_second_output": "12"}, None).text == "cents_per_second_output: 12¢ = $0.12/秒"
    assert price_from_skus(VEO_LITE, "720p").text == "duration_seconds_without_audio_720p: $0.03/秒"
    assert price_from_skus({"duration_seconds": "0.1120"}, None).text == "duration_seconds: $0.112/秒"


def test_minimum_per_generation_and_image_input_are_per_clip() -> None:
    aleph = {"cents_per_second_output": "28", "minimum_cents_per_generation": "56"}
    price = price_from_skus(aleph, "720p")
    assert price.usd_per_second == 0.28 and price.min_clip_usd == 0.56
    assert price.min_text == "minimum_cents_per_generation: 56¢ = $0.56/本"
    grok = {"cents_per_image_input": "1", "cents_per_video_output_second_720p": "3"}
    assert price_from_skus(grok, "720p").image_input_usd == 0  # text-to-video: no input image
    i2v = price_from_skus(grok, "720p", image_to_video=True)
    assert (i2v.usd_per_second, i2v.image_input_usd) == (0.03, 0.01)
    assert i2v.image_text == "cents_per_image_input: 1¢ = $0.01/枚"


def test_unreadable_skus_next_to_a_known_price_are_noted() -> None:
    price = price_from_skus({"duration_seconds": "0.1", "mystery_fee": "3"}, "720p")
    assert price.usd_per_second == 0.1 and price.unreadable == ("mystery_fee",)
    (model,) = _parse([_entry(pricing_skus={"duration_seconds": "0.1", "mystery_fee": "3"})])
    assert "読み取れない料金項目（mystery_fee）は見積もりに含めていません" in model.price_note


def test_audio_notes_when_only_with_audio_prices_apply() -> None:
    (switchable,) = _parse([_entry(pricing_skus={"duration_seconds_with_audio": "0.4"})])
    assert switchable.price_per_second_usd == 0.4
    assert "無音の料金がモデル一覧にないため、音声ありの料金で見積もります" in switchable.price_note
    (fixed,) = _parse([_entry(generate_audio=None, pricing_skus={"duration_seconds_with_audio": "0.4",
                                                                 "duration_seconds_without_audio": "0.2"})])  # fmt: skip
    assert fixed.price_per_second_usd == 0.4 and "generate_audio）にモデルが対応していない" in fixed.price_note


# --- values that cannot be used (never replaced by another, possibly lower, SKU) --------------------------------------


def _unusable(skus: dict[str, Any], resolution: str | None = "720p", **options: Any) -> str:
    price = price_from_skus(skus, resolution, **options)
    assert price.usd_per_second is None and price.known is False and price.broken is True
    assert price.min_clip_usd == 0 and price.image_input_usd == 0
    return price.problem or ""


def test_an_unreadable_value_of_the_requested_resolution_never_falls_back_to_a_cheaper_sku() -> None:
    skus = {"duration_seconds_720p": "$0.30", "duration_seconds": "0.05"}
    problem = _unusable(skus)
    assert 'duration_seconds_720p の値 "$0.30" を読み取れません' in problem and "推測しません" in problem
    # Nor is another resolution (here 1080p, priced by the generic $0.05 SKU) requested in its place.
    (model,) = _parse([_entry(pricing_skus=skus)])
    assert model.id == "vendor/model" and model.params["resolution"] == "720p"
    assert model.price_known is False and model.params["price_source"] == "unknown"
    assert "料金を確認できません" in model.price_note and "$0.30" in model.price_note
    # Every SKU that could be the chosen (highest) one must be readable, e.g. two spellings of the silent 720p price.
    _unusable({"duration_seconds_without_audio_720p": "0.03", "duration_seconds_720p_without_audio": "abc"})
    for bad in ("-0.1", "n/a", "", None, True, {"usd": "0.1"}):
        assert price_from_skus({"duration_seconds": bad}, "720p").broken, bad


@respx.mock
def test_a_model_with_an_unusable_price_cannot_be_selected(set_env: Callable[..., Any]) -> None:
    set_env(OPENROUTER_API_KEY=KEY)
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json={"data": [
        _entry("vendor/unreadable", pricing_skus={"duration_seconds_720p": "$0.30", "duration_seconds": "0.05"}),
        _entry("vendor/zero", pricing_skus={"duration_seconds": "-0"}),
        _entry("vendor/cents", pricing_skus={"cents_per_second_output": "0.12"}),
        _entry("google/veo-3.1-lite", pricing_skus=VEO_LITE),
    ]}))  # fmt: skip
    provider = OpenRouterProvider()
    models = {m.id: m for m in provider.models()}
    for model_id in ("vendor/unreadable", "vendor/zero", "vendor/cents"):
        assert models[model_id].price_known is False and models[model_id].price_per_second_usd == 0
        usable, reason = provider.check_model(model_id)
        assert usable is False and "料金を確認できない" in (reason or "")
    listed = {m["id"]: m for m in provider.info()["models"]}
    assert listed["vendor/zero"]["price_per_second_usd"] is None
    assert "3 件は選べません" in provider.info()["models_note"]
    assert provider.default_model() == "google/veo-3.1-lite"


def test_unreadable_values_that_would_not_be_used_are_only_noted() -> None:
    # Another resolution, a with-audio price next to a silent one, a both-modes SKU next to a mode-specific one
    # and the generic SKU next to a resolution-specific one never price this request anyway.
    cases = [
        ({"duration_seconds_720p": "0.08", "duration_seconds_1080p": "$0.30"}, 0.08),
        ({"duration_seconds_without_audio_720p": "0.03", "duration_seconds_with_audio_720p": "?"}, 0.03),
        ({"text_to_video_duration_seconds_720p": "0.08", "duration_seconds_720p": "?"}, 0.08),
        ({"duration_seconds_720p": "0.08", "duration_seconds": "?"}, 0.08),
    ]
    for skus, usd in cases:
        price = price_from_skus(skus, "720p")
        assert price.usd_per_second == usd and not price.broken
        assert len(price.unreadable) == 1
    models = _parse([_entry(pricing_skus={"duration_seconds_720p": "0.08", "duration_seconds_1080p": "$0.30"})])
    assert [m.id for m in models] == ["vendor/model"]  # no "@1080p": its own price is unreadable
    assert "読み取れない料金項目（duration_seconds_1080p）は見積もりに含めていません" in models[0].price_note


def test_an_unreadable_minimum_or_image_charge_is_never_dropped() -> None:
    problem = _unusable({"cents_per_second_output": "10", "minimum_cents_per_generation": "56 cents"})
    assert "minimum_cents_per_generation" in problem
    grok = {"cents_per_video_output_second_720p": "7", "cents_per_image_input": "n/a"}
    assert "cents_per_image_input" in _unusable(grok, image_to_video=True)
    # Text-to-video never pays for an input image: its price stands, but image-to-video is not offered.
    assert price_from_skus(grok, "720p").usd_per_second == 0.07
    (model,) = _parse([_entry(generate_audio=None, pricing_skus=grok)])
    assert model.price_known and model.price_per_second_usd == 0.07
    assert model.image_to_video is False and model.params["image_to_video_price_per_second"] is None
    assert "入力画像からの生成には使えません" in model.price_note and "cents_per_image_input" in model.price_note
    (unpriced,) = _parse([_entry(pricing_skus={"cents_per_second_output": "10", "minimum_cents_per_generation": "x"})])
    assert unpriced.price_known is False and unpriced.min_clip_usd == 0


@pytest.mark.parametrize(
    ("skus", "options", "shown"),
    [
        # cents named, dollars meant: 100x too low
        ({"cents_per_second_output": "0.12"}, {}, "cents_per_second_output: 0.12¢ = $0.0012/秒"),
        ({"cents_per_video_output_second_720p": "0.07"}, {}, "$0.0007/秒"),
        # dollars named, cents meant: 100x too high
        ({"duration_seconds": "12"}, {}, "duration_seconds: $12/秒"),
        ({"duration_seconds_without_audio_720p": "8"}, {}, "$8/秒"),
        ({"cents_per_second_output": "10", "minimum_cents_per_generation": "5600"}, {}, "$56/本"),
        ({"cents_per_second_output": "10", "minimum_cents_per_generation": "0.56"}, {}, "$0.0056/本"),
        ({"cents_per_video_output_second_720p": "7", "cents_per_image_input": "200"}, {"image_to_video": True}, "$2/枚"),
        ({"cents_per_video_output_second_720p": "7", "cents_per_image_input": "0.002"}, {"image_to_video": True},
         "$0.00002/枚"),
    ],
)  # fmt: skip
def test_prices_outside_the_plausible_range_are_not_used(
    skus: dict[str, Any], options: dict[str, Any], shown: str
) -> None:
    problem = _unusable(skus, **options)
    assert shown in problem and "想定の範囲" in problem and "ドルとセントの取り違え" in problem


def test_plausible_range_bounds_and_the_real_listing() -> None:
    assert price_from_skus({"duration_seconds": "0.005"}, None).usd_per_second == 0.005
    assert price_from_skus({"duration_seconds": "3"}, None).usd_per_second == 3.0
    assert price_from_skus({"cents_per_second_output": "300"}, None).usd_per_second == 3.0
    for value in ("0.0049", "3.01"):
        assert price_from_skus({"duration_seconds": value}, None).broken
    # Every price the real listing quotes (incl. 4K and with-audio ones, $0.02–$0.60/s) is in range.
    for entry in _fixture_entries():
        skus = entry.get("pricing_skus") or {}
        for key in skus:
            price = price_from_skus({key: skus[key]}, None)
            assert not price.broken, (entry["id"], key, price.problem)
    models = {m.id: m for m in _parse(_fixture_entries())}
    assert not any(m.params["price_source"] == "unknown" and "想定の範囲" in m.price_note for m in models.values())


@pytest.mark.parametrize("zero", ["0", "-0", "0.00", "-0.0", 0])
def test_a_zero_price_per_second_is_not_free(zero: Any) -> None:
    problem = _unusable({"duration_seconds": zero})
    assert "duration_seconds が 0 と記載されていますが、無料かどうかを確認できません" in problem
    assert "-0" not in problem and "$0" not in problem
    # The requested resolution's $0 is not replaced by the generic price either (nor by another resolution).
    _unusable({"duration_seconds_720p": zero, "duration_seconds": "0.05"})
    (model,) = _parse([_entry(pricing_skus={"duration_seconds_720p": zero, "duration_seconds": "0.05"})])
    assert model.params["resolution"] == "720p" and model.price_known is False
    assert "予算を確保できないため使用できません" in model.price_note


@pytest.mark.parametrize("zero", ["0", "-0"])
def test_a_zero_minimum_or_image_charge_is_no_charge(zero: str) -> None:
    price = price_from_skus({"cents_per_second_output": "10", "minimum_cents_per_generation": zero}, "720p")
    assert price.usd_per_second == 0.1 and price.min_clip_usd == 0 and price.min_text is None
    i2v = price_from_skus({"cents_per_video_output_second_720p": "7", "cents_per_image_input": zero}, "720p",
                          image_to_video=True)  # fmt: skip
    assert i2v.usd_per_second == 0.07 and i2v.image_input_usd == 0 and i2v.image_text is None
    (model,) = _parse([_entry(pricing_skus={"cents_per_second_output": "10", "minimum_cents_per_generation": zero})])
    assert "最低料金" not in model.price_note and "$-0" not in model.price_note


def test_the_with_audio_note_names_the_silent_price_it_did_not_use() -> None:
    # 720p is priced with audio only, a silent price exists only without a resolution: the 720p SKU wins
    # (the higher, safe choice), and the note must not claim the listing has no silent price.
    skus = {"duration_seconds_with_audio_720p": "0.10", "duration_seconds_without_audio": "0.05"}
    price = price_from_skus(skus, "720p")
    assert (price.usd_per_second, price.sku, price.audio) == (0.10, "duration_seconds_with_audio_720p", "with")
    (model,) = _parse([_entry(supported_resolutions=["720p"], pricing_skus=skus)])
    assert model.price_per_second_usd == 0.10
    assert "無音の料金がモデル一覧にない" not in model.price_note
    assert ("解像度を指定しない無音の料金（duration_seconds_without_audio: $0.05/秒）はありますが、"
            "720p 専用の料金を優先するため、720p の音声ありの料金で見積もります") in model.price_note  # fmt: skip
    # ... also when another resolution has a silent price too.
    both = price_from_skus({**skus, "duration_seconds_without_audio_1080p": "0.08"}, "720p")
    assert both.usd_per_second == 0.10 and "解像度を指定しない無音の料金" in (both.audio_note or "")
    # A silent price for another resolution only.
    other = price_from_skus({"duration_seconds_with_audio": "0.4", "duration_seconds_without_audio_1080p": "0.3"}, "720p")
    assert other.usd_per_second == 0.4 and "無音の料金がモデル一覧にない" not in (other.audio_note or "")
    assert "ほかの解像度または生成方法のもの（duration_seconds_without_audio_1080p）" in (other.audio_note or "")
    # No silent price at all, and a model whose audio cannot be turned off: the notes stay as they were.
    assert price_from_skus({"duration_seconds_with_audio": "0.4"}, "720p").audio_note == (
        "無音の料金がモデル一覧にないため、音声ありの料金で見積もります"
    )
    fixed = price_from_skus(skus, "720p", generate_audio=None)
    assert fixed.audio_note and "generate_audio）にモデルが対応していない" in fixed.audio_note
    assert price_from_skus(VEO_LITE, "720p").audio_note is None


# --- which models are offered ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"supported_durations": None},
        {"supported_durations": []},
        {"supported_durations": [0, 120]},
        {"supported_aspect_ratios": None},
        {"supported_aspect_ratios": ["16:9", "1:1"]},
        {"upscale_factor": {"min": 1.5, "max": 3}},
    ],
)
def test_models_need_durations_and_9_16_to_be_offered(overrides: dict[str, Any]) -> None:
    assert _parse([_entry(**overrides)]) == []


def test_resolution_falls_back_to_one_the_listing_prices() -> None:
    (model,) = _parse([_entry(supported_resolutions=["720p", "1080p"], pricing_skus={"duration_seconds_1080p": "0.2"})])
    assert model.id == "vendor/model" and model.params["resolution"] == "1080p" and model.price_per_second_usd == 0.2
    models = _parse([_entry(pricing_skus={"duration_seconds": "0.1", "duration_seconds_1080p": "0.2"})])
    assert [(m.id, m.price_per_second_usd) for m in models] == [("vendor/model", 0.1), ("vendor/model@1080p", 0.2)]
    # 1080p without its own SKU: no "@1080p" variant (its price would be a guess).
    assert [m.id for m in _parse([_entry(pricing_skus={"duration_seconds": "0.1"})])] == ["vendor/model"]


# --- per-clip costs -------------------------------------------------------------------------------------------------


def test_provider_model_cost_applies_the_minimum_and_per_clip_extras() -> None:
    (model,) = _parse([_entry(pricing_skus={"cents_per_second_output": "28", "minimum_cents_per_generation": "56"})])
    assert model.min_clip_usd == 0.56 and "minimum_cents_per_generation: 56¢ = $0.56/本" in model.price_note
    assert model.cost_for(1) == pytest.approx(0.56)  # 0.28 < minimum
    assert model.cost_for(4) == pytest.approx(1.12)
    pricing = cost.ModelPricing.from_model("openrouter", model)
    assert pricing.min_clip_usd == 0.56 and pricing.clip_cost_usd(1) == pytest.approx(0.56)
    assert pricing.clip_cost_usd(4) == pytest.approx(1.12)


@respx.mock
def test_estimate_books_the_minimum_per_clip(set_env: Callable[..., Any]) -> None:
    set_env(OPENROUTER_API_KEY=KEY)
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json={"data": [
        _entry("vendor/minimum", supported_durations=[2, 4], supported_resolutions=["720p"],
               pricing_skus={"cents_per_second_output": "10", "minimum_cents_per_generation": "56"}),
        _entry("google/veo-3.1-lite", pricing_skus=VEO_LITE),
    ]}))  # fmt: skip
    stats = {"videos": 3, "shots_total": 12, "unique_clips": 5, "new_clips": 5, "reused_clips": 0,
             "variety_score": 0.5, "variety_warning": None, "clip_duration": 4.0}  # fmt: skip
    estimate = cost.estimate({}, stats, "openrouter", "vendor/minimum", cached_clips=0, llm_estimate_usd=0,
                             budget_limit_usd=None)  # fmt: skip
    # 4 s × $0.10 = $0.40 < the $0.56 minimum per generation
    assert estimate["video_generation_usd"] == pytest.approx(5 * 0.56)
    assert estimate["naive_total_usd"] == pytest.approx(12 * 0.56)
    assert any("最低料金 $0.56" in note for note in estimate["notes"])
    rows = {(r["provider"], r["model"]): r for r in estimate["comparison"]}
    assert rows[("openrouter", "vendor/minimum")]["total_usd"] == pytest.approx(5 * 0.56)
    assert rows[("openrouter", "google/veo-3.1-lite")]["total_usd"] == pytest.approx(5 * 6 * 0.03)
    assert cost.clip_cost_usd("openrouter", "vendor/minimum", 2) == pytest.approx(0.56)


@respx.mock
@pytest.mark.parametrize("keyed", [True, False])
def test_estimate_note_cites_the_listing_not_researched_values(set_env: Callable[..., Any], keyed: bool) -> None:
    """The built-in list is a copy of the real listing now: no "（または調査値）" (or researched values)."""
    set_env(OPENROUTER_API_KEY=KEY if keyed else "")
    route = respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text("utf-8"))))
    stats = {"videos": 1, "shots_total": 4, "unique_clips": 4, "new_clips": 4, "reused_clips": 0,
             "variety_score": 0.5, "variety_warning": None}  # fmt: skip
    estimate = cost.estimate({}, stats, "openrouter", "kwaivgi/kling-v3.0-std", cached_clips=0, llm_estimate_usd=0,
                             budget_limit_usd=None)  # fmt: skip
    assert route.called is keyed
    if not keyed:  # the built-in copy of the 2026-10-13 listing
        assert estimate["as_of"] == "2026-10-13"
    assert estimate["price_per_second_usd"] == 0.084
    notes = "\n".join(estimate["notes"])
    assert "調査値" not in notes
    assert (f"料金は {estimate['as_of']} 時点の OpenRouter のモデル一覧（最新の一覧を取得できないときは組み込みの写し）の"
            "料金に基づく概算です。実際の請求額は、生成の完了時に OpenRouter が返す実費（usage.cost）で記録します") in notes  # fmt: skip


# --- requests ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("openrouter-pricing") / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=360x640:r=24:d=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=60,
    )  # fmt: skip
    return path.read_bytes()


def _mock_job(mp4: bytes, *, cost: float | None = None) -> respx.Route:
    usage = {"usage": {"cost": cost, "is_byok": False}} if cost is not None else {}
    submit = respx.post(SUBMIT_URL).mock(return_value=httpx.Response(202, json={"id": JOB, "status": "pending"}))
    respx.get(POLL_URL).mock(return_value=httpx.Response(200, json={"id": JOB, "status": "completed", **usage}))
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, content=mp4))
    return submit


@respx.mock
def test_generate_for_a_1080p_variant_sends_the_real_model_id_and_resolution(
    set_env: Callable[..., Any], mp4_bytes: bytes, tmp_path: Path
) -> None:
    set_env(OPENROUTER_API_KEY=KEY)
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text("utf-8"))))
    submit = _mock_job(mp4_bytes)
    provider = OpenRouterProvider(sleep=lambda _seconds: None)
    req = ClipRequest(prompt="a quiet harbour at dawn", negative_prompt="", duration=5.0,
                      model="alibaba/wan-2.6@1080p", seed=3)  # fmt: skip
    result = provider.generate(req, str(tmp_path / "clip.mp4"))
    body = json.loads(submit.calls.last.request.content)
    assert body["model"] == "alibaba/wan-2.6" and body["resolution"] == "1080p"
    assert body["aspect_ratio"] == "9:16" and body["duration"] == 5 and body["generate_audio"] is False
    assert "@" not in json.dumps(body)
    # No usage.cost reported → the 1080p text-to-video SKU: 5 s × $0.12.
    assert result.model == "alibaba/wan-2.6@1080p" and result.cost_usd == pytest.approx(0.60)
    assert result.raw["cost_source"] == "estimate"


@respx.mock
def test_image_to_video_requests_are_priced_with_their_own_skus(
    set_env: Callable[..., Any], mp4_bytes: bytes, tmp_path: Path
) -> None:
    set_env(OPENROUTER_API_KEY=KEY)
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text("utf-8"))))
    image = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1", str(image)],
                   check=True, timeout=30)  # fmt: skip
    submit = _mock_job(mp4_bytes)
    provider = OpenRouterProvider(sleep=lambda _seconds: None)
    wan = provider.generate(ClipRequest(prompt="p", negative_prompt="", duration=5.0, model="alibaba/wan-2.6",
                                        image_path=str(image)), str(tmp_path / "wan.mp4"))  # fmt: skip
    assert json.loads(submit.calls.last.request.content)["frame_images"][0]["frame_type"] == "first_frame"
    assert wan.cost_usd == pytest.approx(5 * 0.10)  # image_to_video_duration_seconds_720p
    grok = provider.generate(ClipRequest(prompt="p", negative_prompt="", duration=5.0,
                                         model="x-ai/grok-imagine-video", image_path=str(image)),
                             str(tmp_path / "grok.mp4"))  # fmt: skip
    assert grok.cost_usd == pytest.approx(5 * 0.07 + 0.002)  # + cents_per_image_input (0.2¢)
