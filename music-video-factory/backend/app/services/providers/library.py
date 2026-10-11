"""Library provider: no generation; the planner maps scenes onto uploaded clips (§6.4)."""

from __future__ import annotations

from app.services.errors import ProviderError
from app.services.providers.base import (
    PRICING_AS_OF,
    CancelCheck,
    ClipRequest,
    ClipResult,
    ProgressCallback,
    ProviderModel,
    SubmitCallback,
    VideoProvider,
    snap_duration,
)

__all__ = ["LibraryProvider"]

# Uploads are 1 s – 10 min (§5.5); the value only bounds clip_duration_for().
_MAX_UPLOAD_SEC = 600.0


class LibraryProvider(VideoProvider):
    id = "library"
    name = "アップロード素材"
    kind = "library"
    commercial_use_note = (
        "アップロードした素材だけを使います（新規生成なし）。"
        "撮影者・出演者の許諾や映り込み（人物・ロゴ・音声）など、素材の権利は利用者が確認してください。"
    )
    MODEL_ID = "library"

    def models(self) -> list[ProviderModel]:
        return [
            ProviderModel(
                id=self.MODEL_ID,
                name="アップロード素材ライブラリ",
                clip_durations=[],
                max_clip_duration=_MAX_UPLOAD_SEC,
                resolution="素材のまま",
                supports_vertical=True,
                price_per_second_usd=0.0,
                price_note="生成しないため無料（アップロード済みの素材をカット割りして使います）",
                verified=True,
                default_duration=5.0,
                text_to_video=False,
                image_to_video=False,
                as_of=PRICING_AS_OF,
            )
        ]

    def clip_duration_for(self, model: str, wanted: float) -> float:
        # Any length works: the planner reads the real duration of each upload.
        return snap_duration([], _MAX_UPLOAD_SEC, wanted)

    def check_model(self, model: str | None, *, has_image: bool = False) -> tuple[bool, str | None]:
        try:
            self.get_model(model)
        except ProviderError as exc:
            return False, str(exc)
        return True, None

    def generate(
        self,
        req: ClipRequest,
        out_path: str,
        *,
        timeout: float = 900,
        on_progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        on_submitted: SubmitCallback | None = None,  # no remote job: never called
    ) -> ClipResult:
        raise ProviderError(
            "アップロード素材モードでは新しい映像を生成しません。素材タブから動画をアップロードしてください",
            retryable=False,
            code="provider_no_generation",
        )
