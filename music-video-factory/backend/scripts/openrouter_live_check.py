"""Live check of the OpenRouter integration — THIS SPENDS MONEY.

Runs, through the app's own code paths (no DB, no API server):
  1. one Claude storyboard via the OpenRouter chat route (few scenes);
  2. one short silent 9:16 clip via the OpenRouter video provider;
then probes the downloaded clip, extracts a frame, and compares the cost the
app booked with the account usage reported by GET /api/v1/key before/after.

It refuses to run without ``--yes-spend`` and aborts if the video estimate
exceeds ``--max-usd``. Requires OPENROUTER_API_KEY (any placeholder works when
an egress proxy injects the real key).

Usage:
  OPENROUTER_API_KEY=... .venv/bin/python scripts/openrouter_live_check.py --yes-spend \
      [--model google/veo-3.1-lite] [--duration 4] [--scenes 3] [--out var/openrouter_live]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.lyrics_parse import parse_lyrics  # noqa: E402
from app.services.openrouter import api_base, request_headers  # noqa: E402
from app.services.providers.base import ClipRequest  # noqa: E402
from app.services.providers.registry import get_provider  # noqa: E402
from app.services.storyboard import generate_storyboard  # noqa: E402

DEMO_LYRICS = Path(__file__).resolve().parents[1] / "var" / "demo" / "demo_lyrics_ja.txt"


def account_usage() -> float | None:
    """Cumulative account spend in USD from the free GET /key endpoint."""
    response = httpx.get(f"{api_base()}/key", headers=request_headers(), timeout=30)
    response.raise_for_status()
    data = response.json().get("data", {})
    usage = data.get("usage")
    return float(usage) if usage is not None else None


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration,size", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes-spend", action="store_true", help="confirm that real, billed requests may be sent")
    ap.add_argument("--model", default="google/veo-3.1-lite")
    ap.add_argument("--duration", type=float, default=4.0)
    ap.add_argument("--scenes", type=int, default=3)
    ap.add_argument("--max-usd", type=float, default=0.5, help="abort if the video estimate exceeds this")
    ap.add_argument("--skip-storyboard", action="store_true")
    ap.add_argument("--skip-video", action="store_true")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "var" / "openrouter_live")
    args = ap.parse_args(argv)
    if not args.yes_spend:
        print("refusing to run: pass --yes-spend to send billed requests", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict = {"model": args.model, "duration": args.duration}

    before = account_usage()
    report["account_usage_before_usd"] = before
    print(f"account usage before: ${before}")

    prompt, negative = None, ""
    if not args.skip_storyboard:
        lines, _ = parse_lyrics(DEMO_LYRICS.read_text(encoding="utf-8"), "txt")
        started = time.monotonic()
        board = generate_storyboard(
            title="夜明けのうた", artist_name="デモアーティスト", mood="切ない、夜明け前の静けさ",
            purpose="Instagram ストーリーズ", lyrics=lines, analysis=None, preset_id="urban_night",
            custom_world=None, n_scenes=args.scenes, seed=7, use_claude=True,
        )  # fmt: skip
        report["storyboard"] = {
            "seconds": round(time.monotonic() - started, 1),
            "source": board.get("source"), "route": board.get("route"), "model": board.get("model"),
            "usage": board.get("usage"), "cost_usd": board.get("cost_usd"), "warnings": board.get("warnings"),
            "scenes": len(board.get("scenes") or []),
        }  # fmt: skip
        print("storyboard:", json.dumps(report["storyboard"], ensure_ascii=False, indent=1))
        (args.out / "storyboard.json").write_text(json.dumps(board, ensure_ascii=False, indent=1), encoding="utf-8")
        if board.get("scenes"):
            prompt = board["scenes"][0].get("prompt_en")
            negative = board["scenes"][0].get("negative_prompt_en") or ""

    if not args.skip_video:
        provider = get_provider("openrouter")
        model = next((m for m in provider.models() if m.id == args.model), None)
        if model is None or not model.price_per_second_usd:
            print(f"model {args.model} not offered or unpriced", file=sys.stderr)
            return 1
        estimate = model.price_per_second_usd * args.duration
        print(f"video estimate: {args.duration}s x ${model.price_per_second_usd}/s = ${estimate:.4f}")
        if estimate > args.max_usd:
            print(f"estimate exceeds --max-usd {args.max_usd}; aborting", file=sys.stderr)
            return 1
        prompt = prompt or (
            "A young woman in a dark coat walks alone along a rain-soaked neon-lit city street before dawn, "
            "photorealistic, cinematic lighting, 35mm, shallow depth of field, vertical 9:16 composition"
        )
        req = ClipRequest(prompt=prompt, negative_prompt=negative, duration=args.duration, model=args.model,
                          aspect_ratio="9:16")  # fmt: skip
        out_path = args.out / "clip.mp4"
        started = time.monotonic()
        result = provider.generate(
            req, str(out_path), timeout=900,
            on_submitted=lambda job_id: print(f"  submitted job {job_id}"),
            on_progress=lambda fraction, stage: print(f"  {time.monotonic() - started:6.1f}s {stage} {fraction:.2f}"),
        )  # fmt: skip
        info = probe(out_path)
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        report["video"] = {
            "seconds": round(time.monotonic() - started, 1), "estimate_usd": round(estimate, 4),
            "booked_cost_usd": result.cost_usd, "external_job_id": result.external_job_id,
            "result_model": result.model, "width": video.get("width"), "height": video.get("height"),
            "fps": video.get("r_frame_rate"), "codec": video.get("codec_name"),
            "duration": float(info.get("format", {}).get("duration", 0)),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "size_bytes": int(info.get("format", {}).get("size", 0)),
        }  # fmt: skip
        print("video:", json.dumps(report["video"], ensure_ascii=False, indent=1))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(args.duration / 2), "-i", str(out_path),
                        "-frames:v", "1", str(args.out / "frame.png")], check=True)  # fmt: skip

    time.sleep(5)  # usage on /key can lag a little behind completion
    after = account_usage()
    report["account_usage_after_usd"] = after
    if before is not None and after is not None:
        report["account_delta_usd"] = round(after - before, 6)
    print(f"account usage after: ${after} (delta ${report.get('account_delta_usd')})")
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
