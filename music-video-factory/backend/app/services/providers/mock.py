"""Mock provider: local FFmpeg (lavfi) placeholder footage, free and offline.

Each clip is an abstract, slowly moving "out of focus" shot built from the
scene's preset palette: a rotating 3-colour gradient, a drifting radial light
leak, soft bokeh orbs, light film grain and a vignette. Composition (sky,
glow, bokeh, water, diagonal) follows the storyboard scene type. A small
``MOCK`` tag is burned into the top-left corner (inside the Instagram Stories
safe area) so a mock clip can never pass for generated footage.

The look is a pure function of ``(prompt, negative_prompt, seed, scene hints)``
and the encode is bit-exact, so the same request always yields the same file.
Synthesis runs at half resolution and is upscaled (the image is soft by
design); ``-preset ultrafast`` keeps a 5 s 1080x1920 clip at ~1.5-2 s wall
time on 4 cores — the renderer re-encodes it anyway.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.services.errors import ProviderError
from app.services.providers.base import (
    PRICING_AS_OF,
    CancelCheck,
    ClipRequest,
    ClipResult,
    ProgressCallback,
    ProviderCancelled,
    ProviderModel,
    SubmitCallback,
    VideoProvider,
    notify_progress,
)

logger = logging.getLogger(__name__)

__all__ = ["MOCK_FPS", "MockLook", "MockProvider", "build_look"]

MOCK_FPS = 24
_FRAME_SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
_FALLBACK_FONT = "Noto Sans CJK JP"
_STDERR_TAIL = 2000

# Storyboard scene types (presets.SCENE_TYPES) -> composition.
_SCENE_COMPOSITION = {
    "sky_drift": "sky",
    "establishing_wide": "sky",
    "rooftop": "sky",
    "wind_hair": "sky",
    "light_abstract": "glow",
    "turn_to_light": "glow",
    "silhouette": "glow",
    "profile_window": "glow",
    "close_portrait": "glow",
    "spin_sway": "glow",
    "street_flow": "bokeh",
    "crowd_still": "bokeh",
    "train_window": "bokeh",
    "running": "bokeh",
    "water_reflection": "water",
    "lying_down": "water",
    "nature_macro": "water",
}
_COMPOSITIONS = ("sky", "glow", "bokeh", "water", "diagonal")
_NIGHT_PRESETS = frozenset({"urban_night", "dark"})
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


@dataclass
class _Orb:
    size: int  # diameter at synthesis resolution
    color: str  # RRGGBB
    alpha: float
    x: float
    y: float
    vx: float  # px/s at synthesis resolution
    vy: float
    wobble: float
    phase: float


@dataclass
class MockLook:
    composition: str
    palette: list[str]  # dark, mid, light (RRGGBB, normalised)
    base_speed: float  # gradient rotation, radians per frame
    base_points: tuple[int, int, int, int]  # x0, y0, x1, y1 at synthesis resolution
    leak_color: str
    leak_alpha: float
    leak_points: tuple[int, int, int, int]
    leak_speed: float
    orbs: list[_Orb] = field(default_factory=list)
    grain: int = 3
    grain_seed: int = 1
    vignette: float = 0.7  # radians
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- look ---------------------------------------------------------------------------------------


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def _luma(value: str) -> float:
    r, g, b = _hex_to_rgb(value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _adjust(value: str, *, lightness: tuple[float, float], max_saturation: float = 0.78, hue_shift: float = 0.0) -> str:
    h, lum, s = colorsys.rgb_to_hls(*_hex_to_rgb(value))
    lum = min(max(lum, lightness[0]), lightness[1])
    return _rgb_to_hex(colorsys.hls_to_rgb((h + hue_shift) % 1.0, lum, min(s, max_saturation)))


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _hex_to_rgb(a), _hex_to_rgb(b)
    return _rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb, strict=True)))  # type: ignore[arg-type]


def _scene_palette(scene: dict[str, Any], rng: random.Random) -> list[str]:
    colors = []
    for item in scene.get("palette") or []:
        match = _HEX.match(str(item).strip())
        if match:
            colors.append(match.group(1).upper())
    if len(colors) >= 3:
        return colors[:3]
    hue = rng.random()
    generated = [
        _rgb_to_hex(colorsys.hls_to_rgb(hue, 0.18, 0.45)),
        _rgb_to_hex(colorsys.hls_to_rgb((hue + 0.08) % 1, 0.55, 0.55)),
        _rgb_to_hex(colorsys.hls_to_rgb((hue - 0.1) % 1, 0.75, 0.4)),
    ]
    return (colors + generated)[:3]


def _label(scene: dict[str, Any]) -> str:
    raw = str(scene.get("scene_type") or "").replace("_", " ")
    return re.sub(r"[^a-z0-9 -]", "", raw.lower())[:24].strip()


def build_look(req: ClipRequest, width: int, height: int) -> MockLook:
    """Deterministic visual parameters for ``req`` at synthesis size ``width``x``height``."""
    scene = dict(req.scene or {})
    material = json.dumps(
        [req.prompt, req.negative_prompt, req.seed, scene.get("palette"), scene.get("scene_type"), scene.get("preset")],
        ensure_ascii=False,
        default=str,
    )
    rng = random.Random(int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big"))

    composition = _SCENE_COMPOSITION.get(str(scene.get("scene_type") or ""))
    if composition is None:
        composition = rng.choice(_COMPOSITIONS)
    night = str(scene.get("preset") or "") in _NIGHT_PRESETS

    hue_shift = rng.uniform(-0.015, 0.015)
    dark, mid, light = sorted(_scene_palette(scene, rng), key=_luma)
    dark = _adjust(dark, lightness=(0.13, 0.30), hue_shift=hue_shift)
    mid = _adjust(mid, lightness=(0.34, 0.60), hue_shift=hue_shift)
    light = _adjust(light, lightness=(0.58, 0.82), max_saturation=0.7, hue_shift=hue_shift)
    if night:
        dark = _adjust(dark, lightness=(0.10, 0.18))

    w, h = width, height
    cx = w // 2
    speed = rng.uniform(0.0015, 0.0045)
    if composition == "sky":
        base_points = (cx, 0, cx, h)
        leak_center = (rng.uniform(0.25, 0.75) * w, rng.uniform(0.08, 0.22) * h)
        leak_radius = rng.uniform(0.45, 0.65) * w
        n_orbs, orb_size = rng.randint(2, 3), (28, 70)
        palette = [light, mid, dark]
    elif composition == "glow":
        angle = rng.uniform(0.6, 1.0)
        base_points = (0, 0, round(w * angle), h)
        leak_center = (rng.uniform(0.35, 0.65) * w, rng.uniform(0.3, 0.55) * h)
        leak_radius = rng.uniform(0.55, 0.8) * w
        n_orbs, orb_size = rng.randint(3, 5), (50, 120)
        palette = [dark, mid, dark]
    elif composition == "bokeh":
        base_points = (w, 0, 0, h) if rng.random() < 0.5 else (0, 0, w, h)
        leak_center = (rng.uniform(0.1, 0.9) * w, rng.uniform(0.6, 0.85) * h)
        leak_radius = rng.uniform(0.4, 0.6) * w
        n_orbs, orb_size = rng.randint(6, 9), (60, 170)
        palette = [dark, mid, dark] if night else [dark, mid, light]
    elif composition == "water":
        base_points = (cx, 0, cx, h)
        leak_center = (rng.uniform(0.3, 0.7) * w, rng.uniform(0.62, 0.8) * h)
        leak_radius = rng.uniform(0.5, 0.7) * w
        n_orbs, orb_size = rng.randint(4, 6), (20, 55)
        palette = [mid, dark, light]
    else:
        base_points = (0, 0, w, h) if rng.random() < 0.5 else (w, 0, 0, h)
        leak_center = (rng.choice((0.15, 0.85)) * w, rng.uniform(0.1, 0.35) * h)
        leak_radius = rng.uniform(0.5, 0.75) * w
        n_orbs, orb_size = rng.randint(3, 5), (40, 110)
        palette = [dark, mid, light] if rng.random() < 0.5 else [light, mid, dark]

    leak_color = _mix(light, "FFE6C4", 0.35) if not night else _adjust(mid, lightness=(0.6, 0.72), max_saturation=0.9)
    lx, ly = leak_center
    leak_points = (round(lx), round(ly), round(lx + leak_radius), round(ly))

    orbs: list[_Orb] = []
    orb_colors = [light, _mix(light, "FFFFFF", 0.4), leak_color] + ([mid] if night else [])
    for _ in range(n_orbs):
        size = rng.randint(*orb_size)
        orbs.append(
            _Orb(
                size=size,
                color=rng.choice(orb_colors),
                alpha=round(rng.uniform(0.22, 0.5) if composition != "water" else rng.uniform(0.35, 0.6), 3),
                x=round(rng.uniform(-0.1, 0.95) * w, 1),
                y=round(rng.uniform(0.15, 0.95) * h, 1),
                vx=round(rng.uniform(-9, 9) if composition != "bokeh" else rng.uniform(-22, 22), 2),
                vy=round(rng.uniform(-14, -3), 2),
                wobble=round(rng.uniform(0, 8), 2),
                phase=round(rng.uniform(0, 6.28), 2),
            )
        )

    return MockLook(
        composition=composition,
        palette=palette,
        base_speed=round(speed, 5),
        base_points=tuple(int(v) for v in base_points),  # type: ignore[arg-type]
        leak_color=leak_color,
        leak_alpha=round(rng.uniform(0.55, 0.85), 3),
        leak_points=leak_points,
        leak_speed=round(rng.uniform(0.001, 0.003), 5),
        orbs=orbs,
        grain=2,
        grain_seed=rng.randint(1, 2**31 - 1),
        vignette=round(rng.uniform(0.55, 0.8), 3),
        label=_label(scene),
    )


# --- FFmpeg ------------------------------------------------------------------------------------


def _alpha_hex(alpha: float) -> str:
    return f"{max(0, min(255, round(alpha * 255))):02X}"


def _filtergraph(look: MockLook, *, width: int, height: int, seconds: float, has_image: bool) -> str:
    sw, sh = width // 2, height // 2
    d = f"{seconds:.3f}"
    r = MOCK_FPS
    chains: list[str] = []
    if has_image:
        chains.append(
            f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},setsar=1,"
            f"fps={r},format=gbrp[base]"
        )
    else:
        x0, y0, x1, y1 = look.base_points
        c0, c1, c2 = look.palette
        chains.append(
            f"gradients=s={sw}x{sh}:r={r}:d={d}:c0=0x{c0}:c1=0x{c1}:c2=0x{c2}:nb_colors=3:"
            f"x0={x0}:y0={y0}:x1={x1}:y1={y1}:speed={look.base_speed}:seed=1:type=linear,format=gbrp[base]"
        )
    lx0, ly0, lx1, ly1 = look.leak_points
    chains.append(
        f"gradients=s={sw}x{sh}:r={r}:d={d}:c0=0x{look.leak_color}{_alpha_hex(look.leak_alpha)}:"
        f"c1=0x{look.leak_color}00:nb_colors=2:x0={lx0}:y0={ly0}:x1={lx1}:y1={ly1}:"
        f"speed={look.leak_speed}:seed=2:type=radial,format=gbrap[leak]"
    )
    chains.append("[base][leak]overlay=format=gbrp[l0]")
    last = "l0"
    for index, orb in enumerate(look.orbs):
        s = orb.size
        half = s // 2
        chains.append(
            f"gradients=s={s}x{s}:r={r}:d={d}:c0=0x{orb.color}{_alpha_hex(orb.alpha)}:"
            f"c1=0x{orb.color}{_alpha_hex(orb.alpha * 0.82)}:c2=0x{orb.color}00:nb_colors=3:"
            f"x0={half}:y0={half}:x1={s}:y1={half}:speed=0.00001:seed=3:type=radial,format=gbrap[o{index}]"
        )
        x = f"{orb.x}+{orb.vx}*t+{orb.wobble}*sin(0.7*t+{orb.phase})"
        y = f"{orb.y}+{orb.vy}*t"
        chains.append(f"[{last}][o{index}]overlay=x='{x}':y='{y}':format=gbrp[l{index + 1}]")
        last = f"l{index + 1}"

    fs = round(0.028 * min(width, height) * (1.0 if height >= width else 1.1))
    box_w, box_h = round(fs * 4.4), round(fs * 1.75)
    tag_x = round(0.06 * width)
    tag_y = round(0.16 * height) if height > width else round(0.07 * height)
    font = _FALLBACK_FONT
    tail = (
        f"[{last}]vignette=angle={look.vignette},format=yuv420p,"
        f"noise=alls={look.grain}:allf=t:all_seed={look.grain_seed},"
        f"scale={width}:{height}:flags=bicubic,setsar=1,"
        f"drawbox=x={tag_x}:y={tag_y}:w={box_w}:h={box_h}:color=black@0.55:t=fill,"
        f"drawtext=font='{font}\\:style=Bold':fontsize={fs}:fontcolor=white@0.92:"
        f"x={tag_x}+({box_w}-tw)/2:y={tag_y}+({box_h}-th)/2:text='MOCK'"
    )
    if look.label:
        tail += (
            f",drawtext=font='{font}':fontsize={round(fs * 0.72)}:fontcolor=white@0.7:"
            f"shadowcolor=black@0.5:shadowx=1:shadowy=1:x={tag_x}:y={tag_y + box_h + round(fs * 0.45)}:"
            f"text='{look.label}'"
        )
    chains.append(tail + "[out]")
    return ";".join(chains)


class MockProvider(VideoProvider):
    id = "mock"
    name = "モック（ダミー映像）"
    kind = "mock"
    commercial_use_note = (
        "ローカルの FFmpeg で合成したテスト用のダミー映像です（画面左上に「MOCK」と表示）。"
        "AI 生成ではありません。動作確認用のため、公開する動画には実際の素材かAI生成プロバイダを使ってください。"
    )
    MODEL_ID = "mock-v1"

    def __init__(self, *, ffmpeg_bin: str | None = None, threads: int = 2, **kwargs: Any):
        super().__init__(**kwargs)
        self.ffmpeg_bin = ffmpeg_bin or "ffmpeg"
        self.threads = max(1, int(threads))

    def models(self) -> list[ProviderModel]:
        return [
            ProviderModel(
                id=self.MODEL_ID,
                name="Mock v1（グラデーション合成）",
                clip_durations=[],
                max_clip_duration=20.0,
                resolution="1080x1920",
                supports_vertical=True,
                price_per_second_usd=0.0,
                price_note="無料（ローカルの FFmpeg で合成。外部 API は呼び出しません）",
                verified=True,
                default_duration=5.0,
                text_to_video=True,
                image_to_video=True,
                as_of=PRICING_AS_OF,
            )
        ]

    def is_available(self) -> tuple[bool, str | None]:
        if shutil.which(self.ffmpeg_bin) is None:
            return False, "FFmpeg が見つからないため、モック映像を合成できません"
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
        model = self.get_model(req.model)
        available, reason = self.is_available()
        if not available:
            raise ProviderError(reason or "モック映像を合成できません", retryable=False, code="provider_unavailable")
        if req.aspect_ratio not in _FRAME_SIZES:
            raise ProviderError(
                f"モックはアスペクト比 {req.aspect_ratio} に対応していません", retryable=False, code="provider_bad_input"
            )
        if req.image_path and not Path(req.image_path).is_file():
            raise ProviderError("入力画像が見つかりません", retryable=False, code="provider_bad_input")
        if cancel_check is not None and cancel_check():
            raise ProviderCancelled()

        width, height = _FRAME_SIZES[req.aspect_ratio]
        frames = max(1, round(model.snap_duration(req.duration) * MOCK_FPS))
        seconds = frames / MOCK_FPS
        look = build_look(req, width // 2, height // 2)
        graph = _filtergraph(look, width=width, height=height, seconds=seconds + 0.25, has_image=bool(req.image_path))

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.stem}.part.mp4")
        inputs: list[str] = []
        if req.image_path:
            inputs = ["-loop", "1", "-framerate", str(MOCK_FPS), "-t", f"{seconds + 0.25:.3f}", "-i", req.image_path]
        cmd = [
            self.ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            *inputs,
            "-filter_complex", graph, "-map", "[out]", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-g", str(MOCK_FPS * 2), "-threads", str(self.threads), "-an",
            "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
            "-movflags", "+faststart", str(tmp),
        ]  # fmt: skip
        notify_progress(on_progress, 0.05, "running")
        started = time.monotonic()
        try:
            self._run(cmd, timeout=timeout, cancel_check=cancel_check)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
        elapsed = time.monotonic() - started
        notify_progress(on_progress, 1.0, "done")
        logger.info("mock: %s (%s, %.2fs) in %.2fs", out.name, look.composition, seconds, elapsed)
        return ClipResult(
            path=str(out),
            duration=round(seconds, 3),
            width=width,
            height=height,
            model=model.id,
            cost_usd=0.0,
            external_job_id=None,
            raw={
                "generator": "ffmpeg-lavfi",
                "fps": MOCK_FPS,
                "frames": frames,
                "elapsed_sec": round(elapsed, 3),
                "look": look.as_dict(),
            },
        )

    def _run(self, cmd: list[str], *, timeout: float, cancel_check: CancelCheck | None) -> None:
        deadline = time.monotonic() + float(timeout)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise ProviderError("FFmpeg が見つかりません", retryable=False, code="provider_unavailable") from exc
        try:
            while True:
                try:
                    _, stderr = proc.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_check is not None and cancel_check():
                        raise ProviderCancelled() from None
                    if time.monotonic() > deadline:
                        raise ProviderError(
                            f"モック映像の合成が {timeout:.0f} 秒以内に終わりませんでした",
                            retryable=True,
                            code="provider_timeout",
                        ) from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "replace")[-_STDERR_TAIL:]
            logger.error("mock: ffmpeg failed (%s): %s", proc.returncode, tail)
            raise ProviderError(
                "モック映像の合成に失敗しました（FFmpeg エラー）", retryable=False, code="provider_failed"
            )
