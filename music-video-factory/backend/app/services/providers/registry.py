"""Provider registry (ARCHITECTURE.md §6.7).

Order = display order: free sources first, then the hosted APIs in the
recommended implementation order (docs/VIDEO_PROVIDERS.md §2.1).
"""

from __future__ import annotations

from typing import Any

from app.services.errors import ProviderNotConfigured
from app.services.providers.base import VideoProvider
from app.services.providers.kling import KlingProvider
from app.services.providers.library import LibraryProvider
from app.services.providers.mock import MockProvider
from app.services.providers.runway import RunwayProvider
from app.services.providers.veo import VeoProvider

__all__ = ["PROVIDER_CLASSES", "PROVIDER_IDS", "get_provider", "list_providers", "provider_info"]

PROVIDER_CLASSES: dict[str, type[VideoProvider]] = {
    MockProvider.id: MockProvider,
    LibraryProvider.id: LibraryProvider,
    VeoProvider.id: VeoProvider,
    RunwayProvider.id: RunwayProvider,
    KlingProvider.id: KlingProvider,
}
PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDER_CLASSES)


def get_provider(provider_id: str) -> VideoProvider:
    """A provider instance; raises :class:`ProviderNotConfigured` (422) for an unknown id."""
    cls = PROVIDER_CLASSES.get(provider_id)
    if cls is None:
        raise ProviderNotConfigured(f"不明な動画生成プロバイダです: {provider_id}", code="provider_unknown")
    return cls()


def list_providers() -> list[VideoProvider]:
    return [cls() for cls in PROVIDER_CLASSES.values()]


def provider_info() -> list[dict[str, Any]]:
    """``ProviderInfo`` dicts for ``GET /api/system/capabilities`` (never contains secrets)."""
    return [provider.info() for provider in list_providers()]
