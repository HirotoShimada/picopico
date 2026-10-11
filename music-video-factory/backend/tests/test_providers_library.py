"""Library provider: always configured, never generates."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.errors import ProviderError
from app.services.providers.base import ClipRequest
from app.services.providers.library import LibraryProvider


def test_always_configured_and_free() -> None:
    provider = LibraryProvider()
    assert provider.is_configured() == (True, None)
    assert provider.check_model(None) == (True, None)
    info = provider.info()
    assert info["kind"] == "library" and info["configured"] and info["available"]
    assert info["models"][0]["price_per_second_usd"] == 0.0
    assert info["commercial_use_note"]


def test_generate_raises_non_retryable_provider_error(tmp_path: Path) -> None:
    with pytest.raises(ProviderError) as info:
        LibraryProvider().generate(ClipRequest("p", "", 5.0, "library"), str(tmp_path / "x.mp4"))
    assert info.value.retryable is False and info.value.code == "provider_no_generation"
    assert not (tmp_path / "x.mp4").exists()


def test_clip_duration_is_passthrough() -> None:
    provider = LibraryProvider()
    assert provider.clip_duration_for("library", 4.5) == 4.5
    assert provider.clip_duration_for("library", 1200) == 600.0
    assert provider.check_model("other")[0] is False
