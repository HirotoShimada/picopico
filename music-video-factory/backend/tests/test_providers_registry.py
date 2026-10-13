"""Registry and ProviderInfo (§5.1) as served by the capabilities endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from app.schemas import ProviderInfo
from app.services.errors import ProviderNotConfigured
from app.services.providers import (
    PROVIDER_IDS,
    get_provider,
    list_providers,
    provider_info,
)
from app.services.providers.kling import KlingProvider
from app.services.providers.library import LibraryProvider
from app.services.providers.mock import MockProvider
from app.services.providers.openrouter import OpenRouterProvider
from app.services.providers.runway import RunwayProvider
from app.services.providers.veo import VeoProvider


def test_get_and_list_providers() -> None:
    assert PROVIDER_IDS == ("mock", "library", "veo", "runway", "kling", "openrouter")
    expected = {"mock": MockProvider, "library": LibraryProvider, "veo": VeoProvider, "runway": RunwayProvider,
                "kling": KlingProvider, "openrouter": OpenRouterProvider}  # fmt: skip
    for provider_id, cls in expected.items():
        provider = get_provider(provider_id)
        assert isinstance(provider, cls) and provider.id == provider_id
    assert [p.id for p in list_providers()] == list(PROVIDER_IDS)
    with pytest.raises(ProviderNotConfigured) as info:
        get_provider("sora")
    assert info.value.code == "provider_unknown" and info.value.status_code == 422


def test_provider_info_without_keys(set_env: Callable[..., Any]) -> None:
    set_env(GOOGLE_API_KEY="", RUNWAY_API_KEY="", KLING_API_KEY="", KLING_ACCESS_KEY="", KLING_SECRET_KEY="",
            OPENROUTER_API_KEY="")  # fmt: skip
    infos = {info["id"]: info for info in provider_info()}
    for info in infos.values():
        ProviderInfo.model_validate(info)
        assert info["commercial_use_note"]
        assert info["default_model"] in {m["id"] for m in info["models"]}
        for model in info["models"]:
            assert model["clip_durations"] or model["max_clip_duration"] > 0
            # A model without 9:16 output says so (OpenRouter only offers models listing 9:16).
            assert model["price_note"] and (model["supports_vertical"] or "9:16" in model["price_note"])
    assert infos["mock"]["configured"] and infos["mock"]["available"] and infos["mock"]["kind"] == "mock"
    assert infos["library"]["configured"] and infos["library"]["kind"] == "library"
    for provider_id in ("veo", "runway", "kling", "openrouter"):
        info = infos[provider_id]
        assert info["kind"] == "api" and info["configured"] is False and info["available"] is False
        assert info["reason"]
        # OpenRouter without a key: the built-in copy of the 2026-10-13 listing
        as_of = "2026-10-13" if provider_id == "openrouter" else "2026-10-11"
        assert all(m["verified"] is False and m["as_of"] == as_of and m["source_url"] for m in info["models"])
    assert infos["openrouter"]["models_source"] == "fallback" and "OPENROUTER_API_KEY" in infos["openrouter"]["models_note"]
    gen4_turbo = next(m for m in infos["runway"]["models"] if m["id"] == "gen4_turbo")
    assert gen4_turbo["text_to_video"] is False and "画像" in gen4_turbo["price_note"]


def test_provider_info_with_keys_never_leaks_them(set_env: Callable[..., Any]) -> None:
    secrets = {"GOOGLE_API_KEY": "g-secret-123", "RUNWAY_API_KEY": "r-secret-456", "KLING_API_KEY": "k-secret-789",
               "KLING_ACCESS_KEY": "ak-secret", "KLING_SECRET_KEY": "sk-secret",
               "OPENROUTER_API_KEY": "or-secret-012",
               # unreachable on purpose: the listing fetch fails fast and the built-in list is shown
               "OPENROUTER_API_BASE": "http://127.0.0.1:9/api/v1"}  # fmt: skip
    set_env(**secrets)
    infos = provider_info()
    assert all(info["configured"] and info["available"] for info in infos)
    dumped = json.dumps(infos, ensure_ascii=False)
    for value in secrets.values():
        assert value not in dumped


@pytest.mark.parametrize(
    ("provider_id", "model", "wanted", "expected"),
    [("mock", "mock-v1", 5, 5), ("veo", "veo-3.1-lite-generate-preview", 5, 6), ("runway", "gen4.5", 5, 5),
     ("kling", "kling-3.0", 5, 5), ("kling", "kling-v2-5-turbo", 7, 10), ("library", "library", 3.2, 3.2),
     ("openrouter", "google/veo-3.1-lite", 5, 6), ("openrouter", "kwaivgi/kling-video-o1", 7, 10)],
)  # fmt: skip
def test_clip_duration_for(provider_id: str, model: str, wanted: float, expected: float) -> None:
    assert get_provider(provider_id).clip_duration_for(model, wanted) == expected
