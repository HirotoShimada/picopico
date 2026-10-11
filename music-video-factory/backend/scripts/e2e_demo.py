#!/usr/bin/env python3
"""End-to-end demo against a RUNNING API (HTTP only): the MVP acceptance test.

    # terminal 1 (inline tasks)            # or celery: TASK_MODE=celery for API and worker
    cd backend && .venv/bin/uvicorn app.main:app --port 8000
    # terminal 2
    cd backend && .venv/bin/python scripts/e2e_demo.py [--api http://localhost:8000]

Flow: health → create project → upload demo song → wait for analysis → Whisper
check (503 expected without a local model) → lyrics (demo TXT) → heuristic
auto-time → rights (then re-saving the same lyrics must keep ids, timing and
rights) → upload the 3 demo clips → estimate → batch A (mock, 3 × 15 s, lyric
style "auto" so the styles differ) → batch B (library, 3 × 15 s) → edit one
video (style + lyric timing offset + clip swap) and wait for the re-render →
download every MP4 and both ZIPs (batch A through its signed zip-link without
the API token, batch B through the API) → validate each file with ffprobe
(1080×1920, H.264, 30 fps, AAC, 15.0 s) and cross-correlate its audio with the
source song segment → summary table. Exits 1 on any failed check.

Demo assets are created with ``scripts/make_demo_assets.py`` when missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEMO_DIR = BACKEND_DIR / "var" / "demo"
RIGHTS = ("audio_rights", "lyrics_rights", "no_real_person_likeness", "model_terms_checked", "platform_terms")
TERMINAL_BATCH = {"completed", "partial", "failed", "cancelled", "budget_exceeded"}
EXPECTED = {"width": 1080, "height": 1920, "vcodec": "h264", "fps": "30/1", "acodec": "aac", "duration": 15.0}


class E2EFailure(Exception):
    pass


@dataclass
class Checks:
    failures: list[str] = field(default_factory=list)

    def expect(self, ok: bool, message: str) -> bool:
        if not ok:
            self.failures.append(message)
            print(f"  ✗ {message}")
        return ok


@dataclass
class VideoReport:
    label: str
    index: int
    video_id: str
    style: str
    grade: str
    start: float
    shots: int
    file: Path | None = None
    size_mb: float = 0.0
    resolution: str = "-"
    vcodec: str = "-"
    fps: str = "-"
    acodec: str = "-"
    duration: float = 0.0
    lag_ms: float | None = None
    corr: float | None = None
    quality: bool = False
    ok: bool = False


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# --- HTTP helpers -------------------------------------------------------------------------------------------------


class Api:
    def __init__(self, base: str, token: str | None, timeout: float):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=base.rstrip("/"), headers=headers, timeout=timeout)

    def call(self, method: str, path: str, *, expect: int | tuple[int, ...] = (200, 201, 202), **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        allowed = (expect,) if isinstance(expect, int) else expect
        if response.status_code not in allowed:
            raise E2EFailure(f"{method} {path} → {response.status_code}: {response.text[:500]}")
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response

    def wait(self, path: str, done: Any, *, timeout: float, what: str, interval: float = 2.0) -> Any:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            body = self.call("GET", path)
            state = done(body)
            if state is True:
                return body
            if state != last:
                log(f"  {what}: {state}")
                last = state
            time.sleep(interval)
        raise E2EFailure(f"timed out after {timeout:.0f}s waiting for {what}")


# --- media checks -------------------------------------------------------------------------------------------------


def ffprobe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return json.loads(out)


def decode_mono(path: Path, start: float | None, duration: float, rate: int = 22050) -> Any:
    import numpy as np

    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-t", f"{duration:.3f}", "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    return np.frombuffer(raw, dtype=np.float32)


def audio_alignment(output: Path, source: Path, start: float, duration: float) -> tuple[float, float]:
    """(lag in ms, normalized correlation) between the video's audio and the source song segment."""
    import numpy as np

    rate = 22050
    out = decode_mono(output, None, duration, rate)
    ref = decode_mono(source, start, duration, rate)
    n = min(len(out), len(ref))
    # Skip the fades at both ends (afade in 0.15 s / out 0.8 s).
    a = out[int(0.3 * rate) : n - int(1.0 * rate)].astype(np.float64)
    b = ref[int(0.3 * rate) : n - int(1.0 * rate)].astype(np.float64)
    a -= a.mean()
    b -= b.mean()
    size = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    xcorr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    max_lag = int(0.5 * rate)
    lags = np.concatenate([xcorr[-max_lag:], xcorr[: max_lag + 1]])
    best = int(np.argmax(lags)) - max_lag
    corr = float(lags.max() / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
    return best / rate * 1000.0, corr


def validate_file(report: VideoReport, path: Path, source: Path, checks: Checks) -> None:
    info = ffprobe(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    report.file = path
    report.size_mb = path.stat().st_size / 1e6
    report.resolution = f"{video.get('width')}x{video.get('height')}"
    report.vcodec = str(video.get("codec_name"))
    report.fps = str(video.get("r_frame_rate"))
    report.acodec = str(audio.get("codec_name", "-"))
    report.duration = float(info["format"].get("duration", 0.0))
    name = f"{report.label}#{report.index + 1}"
    ok = all(
        [
            checks.expect(
                video.get("width") == EXPECTED["width"] and video.get("height") == EXPECTED["height"],
                f"{name}: resolution {report.resolution}",
            ),
            checks.expect(report.vcodec == EXPECTED["vcodec"], f"{name}: video codec {report.vcodec}"),
            checks.expect(video.get("pix_fmt") == "yuv420p", f"{name}: pix_fmt {video.get('pix_fmt')}"),
            checks.expect(report.fps == EXPECTED["fps"], f"{name}: fps {report.fps}"),
            checks.expect(report.acodec == EXPECTED["acodec"], f"{name}: audio codec {report.acodec}"),
            checks.expect(abs(report.duration - EXPECTED["duration"]) <= 0.1, f"{name}: duration {report.duration}"),
        ]
    )
    try:
        report.lag_ms, report.corr = audio_alignment(path, source, report.start, EXPECTED["duration"])
        ok = (
            checks.expect(
                abs(report.lag_ms) <= 5 and report.corr >= 0.9,
                f"{name}: audio vs source lag {report.lag_ms:.1f} ms corr {report.corr:.3f}",
            )
            and ok
        )
    except ImportError:
        log("  numpy not available: audio cross-correlation skipped")
    report.ok = ok and report.quality


# --- flow ---------------------------------------------------------------------------------------------------------


def ensure_demo_assets() -> None:
    needed = ["demo_song.mp3", "demo_lyrics_ja.txt", "clip_01.mp4", "clip_02.mp4", "clip_03.mp4"]
    if not all((DEMO_DIR / n).is_file() for n in needed):
        log("creating demo assets …")
        subprocess.run([sys.executable, str(BACKEND_DIR / "scripts" / "make_demo_assets.py")], check=True)


def wait_batch(api: Api, batch_id: str, timeout: float) -> dict[str, Any]:
    def done(body: dict[str, Any]) -> Any:
        c = body["counts"]
        if body["status"] in TERMINAL_BATCH:
            return True
        return f"{body['status']} (done {c['done']}/{c['total']}, rendering {c['rendering']}, failed {c['failed']})"

    return api.wait(f"/api/batches/{batch_id}", done, timeout=timeout, what=f"batch {batch_id[:8]}", interval=3.0)


def run_batch(
    api: Api, project_id: str, label: str, settings: dict[str, Any], timeout: float, checks: Checks
) -> tuple[dict[str, Any], float]:
    log(f"batch {label}: {json.dumps(settings, ensure_ascii=False)}")
    started = time.monotonic()
    created = api.call("POST", f"/api/projects/{project_id}/batches", json=settings, expect=201)
    batch = wait_batch(api, created["id"], timeout)
    elapsed = time.monotonic() - started
    log(f"batch {label}: {batch['status']} in {elapsed:.1f}s, cost ${batch['cost_usd']:.4f}")
    log(f"batch {label}: clips {batch['clip_counts']}")
    checks.expect(batch["status"] == "completed", f"batch {label} status {batch['status']} ({batch.get('error')})")
    for video in batch["videos"]:
        checks.expect(
            video["status"] == "done", f"batch {label} video {video['index']}: {video['status']} {video['error']}"
        )
    return batch, elapsed


def download(api: Api, video_id: str, dest: Path, checks: Checks, expected_name: str | None = None) -> Path:
    response = api.client.get(f"/api/videos/{video_id}/download", follow_redirects=False)
    if response.status_code != 302:
        raise E2EFailure(f"download {video_id} → {response.status_code}")
    location = response.headers["location"]
    with api.client.stream("GET", location) as media:
        if media.status_code != 200:
            raise E2EFailure(f"signed media {video_id} → {media.status_code}")
        disposition = media.headers.get("content-disposition", "")
        checks.expect(disposition.startswith("attachment"), f"download {video_id}: no attachment disposition")
        if expected_name:
            from urllib.parse import quote

            checks.expect(quote(expected_name, safe="") in disposition, f"download name {disposition!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in media.iter_bytes():
                fh.write(chunk)
    return dest


def download_zip(
    api: Api, batch_id: str, dest: Path, files: list[Path], checks: Checks, *, signed_link: bool = False
) -> None:
    """The batch ZIP via ``GET /api/batches/{id}/zip`` or, with ``signed_link``, via the signed
    ``POST /api/batches/{id}/zip-link`` URL fetched like a browser would: without the API token."""
    if signed_link:
        link = api.call("POST", f"/api/batches/{batch_id}/zip-link")
        log(f"zip link for {batch_id[:8]}: …{link['url'][-40:]} (expires {link['expires_at']})")
        anonymous = httpx.Client(timeout=api.client.timeout)
        request = anonymous.stream("GET", link["url"])
    else:
        anonymous = None
        request = api.client.stream("GET", f"/api/batches/{batch_id}/zip")
    try:
        with request as response:
            if response.status_code != 200:
                raise E2EFailure(f"zip {batch_id} → {response.status_code}")
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
    finally:
        if anonymous is not None:
            anonymous.close()
    with zipfile.ZipFile(dest) as archive:
        checks.expect(archive.testzip() is None, f"zip {dest.name}: CRC error")
        digests = sorted(hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist())
    expected = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in files)
    checks.expect(digests == expected, f"zip {dest.name}: contents differ from the individual downloads")
    if shutil.which("unzip"):
        test = subprocess.run(["unzip", "-tq", str(dest)], capture_output=True, text=True)
        checks.expect(test.returncode == 0, f"unzip -t {dest.name}: {test.stdout.strip()} {test.stderr.strip()}")
    log(f"zip {dest.name}: {len(files)} files, {dest.stat().st_size / 1e6:.1f} MB, verified")


def print_table(reports: list[VideoReport]) -> None:
    headers = [
        "video",
        "style",
        "grade",
        "start",
        "shots",
        "MB",
        "resolution",
        "vcodec",
        "fps",
        "acodec",
        "dur(s)",
        "lag(ms)",
        "corr",
        "quality",
        "result",
    ]
    rows = [
        [
            f"{r.label}#{r.index + 1}",
            r.style,
            r.grade,
            f"{r.start:.2f}",
            str(r.shots),
            f"{r.size_mb:.1f}",
            r.resolution,
            r.vcodec,
            r.fps,
            r.acodec,
            f"{r.duration:.3f}",
            "-" if r.lag_ms is None else f"{r.lag_ms:.1f}",
            "-" if r.corr is None else f"{r.corr:.3f}",
            "pass" if r.quality else "FAIL",
            "OK" if r.ok else "FAIL",
        ]
        for r in reports
    ]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    line = "+".join("-" * (w + 2) for w in widths)
    print("+" + line + "+")
    print("| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)) + " |")
    print("+" + line + "+")
    for row in rows:
        print("| " + " | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) + " |")
    print("+" + line + "+")


def collect(
    api: Api, batch: dict[str, Any], label: str, out: Path, checks: Checks, artist: str, title: str
) -> list[VideoReport]:
    reports = []
    for video in batch["videos"]:
        plan = video["plan"]
        report = VideoReport(
            label,
            video["index"],
            video["id"],
            plan["lyric_style"],
            plan["color_grade"],
            float(plan["start"]),
            len(plan["shots"]),
        )
        report.quality = bool((video.get("quality") or {}).get("passed"))
        if video["status"] == "done":
            name = f"{artist}_{title}_{video['index'] + 1:02d}.mp4"
            path = download(api, video["id"], out / label / f"{video['index'] + 1:02d}.mp4", checks, name)
            validate_file(report, path, DEMO_DIR / "demo_song.mp3", checks)
        reports.append(report)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--token", default=None, help="Bearer token (AUTH_MODE=token)")
    parser.add_argument("--out", type=Path, default=None, help="download directory (default var/e2e/<timestamp>)")
    parser.add_argument("--batch-timeout", type=float, default=900.0)
    parser.add_argument("--skip-edit", action="store_true", help="skip the edit / re-render step")
    args = parser.parse_args()

    ensure_demo_assets()
    out = args.out or BACKEND_DIR / "var" / "e2e" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    api = Api(args.api, args.token, timeout=120.0)
    checks = Checks()
    reports: list[VideoReport] = []
    timings: dict[str, float] = {}
    t0 = time.monotonic()

    try:
        health = api.call("GET", "/api/health")
        log(f"health: {health}")
        checks.expect(health["status"] == "ok", f"health status {health['status']}")
        caps = api.call("GET", "/api/system/capabilities")
        log(f"providers: {[(p['id'], p['configured']) for p in caps['providers']]}, whisper: {caps['whisper']}")

        title, artist = "夜明けのうた（E2E）", "デモアーティスト"
        project = api.call(
            "POST",
            "/api/projects",
            json={"title": title, "artist_name": artist, "mood": "切なくも前向き", "world_preset": "cinematic"},
        )
        pid = project["id"]
        log(f"project {pid}")

        started = time.monotonic()
        with (DEMO_DIR / "demo_song.mp3").open("rb") as fh:
            api.call(
                "POST", f"/api/projects/{pid}/audio", files={"file": ("demo_song.mp3", fh, "audio/mpeg")}, expect=201
            )
        analysis = api.wait(
            f"/api/projects/{pid}/analysis",
            lambda b: True if b["status"] in ("done", "failed") else b["status"],
            timeout=300,
            what="analysis",
        )
        timings["analysis"] = time.monotonic() - started
        if not checks.expect(analysis["status"] == "done", f"analysis {analysis['status']}: {analysis.get('error')}"):
            raise E2EFailure("analysis failed")
        a = analysis["analysis"]
        log(f"analysis: {a['bpm']:.1f} BPM, segment {analysis['segment']}, chorus {a['chorus_candidates'][:1]}")
        checks.expect(115 <= a["bpm"] <= 125, f"bpm {a['bpm']}")

        lyrics = api.call(
            "PUT",
            f"/api/projects/{pid}/lyrics",
            json={"text": (DEMO_DIR / "demo_lyrics_ja.txt").read_text(encoding="utf-8")},
        )
        log(f"lyrics: {lyrics['format']}, {len(lyrics['lines'])} lines")

        if caps["whisper"]["available"]:
            log("whisper available on this server: alignment check skipped (heuristic timing is used below)")
        else:
            whisper = api.client.post(f"/api/projects/{pid}/lyrics/auto-time", json={"method": "whisper"})
            checks.expect(
                whisper.status_code == 503 and whisper.json().get("code") == "whisper_unavailable",
                f"whisper without model → {whisper.status_code} {whisper.text[:120]}",
            )
            log(f"whisper unavailable → {whisper.status_code} {whisper.json().get('detail')}")
        timed = api.call("POST", f"/api/projects/{pid}/lyrics/auto-time", json={"method": "heuristic"})
        starts = [line["start"] for line in timed["lines"]]
        log(f"auto-time ({timed['method_used']}): starts {starts}")
        expected_starts = [8, 12, 16, 20, 24, 28, 32, 36, 48, 52]
        checks.expect(
            all(abs(s - e) < 0.5 for s, e in zip(starts, expected_starts, strict=True)), f"auto-time starts {starts}"
        )

        guard = api.client.post(f"/api/projects/{pid}/batches", json={"provider": "mock"})
        checks.expect(
            guard.status_code == 422 and guard.json().get("code") == "rights_unconfirmed",
            f"batch before rights → {guard.status_code} {guard.text[:120]}",
        )
        api.call("PUT", f"/api/projects/{pid}/rights", json={"items": dict.fromkeys(RIGHTS, True)})
        # Re-saving the same lyrics keeps line ids, timing and the confirmed rights (only new text resets them).
        resaved = api.call(
            "PUT",
            f"/api/projects/{pid}/lyrics",
            json={"text": (DEMO_DIR / "demo_lyrics_ja.txt").read_text(encoding="utf-8")},
        )
        checks.expect(resaved["lines"] == timed["lines"], "re-saved lyrics lost their ids or timing")
        checks.expect(resaved["warnings"] == [], f"re-saved lyrics warnings {resaved['warnings']}")
        checks.expect(
            api.call("GET", f"/api/projects/{pid}")["rights_confirmed_at"] is not None,
            "re-saving identical lyrics reset the rights checklist",
        )
        guard = api.client.post(f"/api/projects/{pid}/batches", json={"provider": "veo"})
        if not next(p for p in caps["providers"] if p["id"] == "veo")["configured"]:
            checks.expect(
                guard.status_code == 422 and guard.json().get("code") == "provider_not_configured",
                f"veo without key → {guard.status_code}",
            )
        guard = api.client.post(f"/api/projects/{pid}/batches", json={"provider": "library"})
        checks.expect(
            guard.status_code == 422 and guard.json().get("code") == "library_empty",
            f"library before uploads → {guard.status_code}",
        )

        files = [
            ("files", (n, (DEMO_DIR / n).open("rb"), "video/mp4"))
            for n in ("clip_01.mp4", "clip_02.mp4", "clip_03.mp4")
        ]
        uploaded = api.call("POST", f"/api/projects/{pid}/clips/upload", files=files, expect=201)
        for _, (_, fh, _) in files:
            fh.close()
        log(f"uploaded clips: {[(c['id'][:8], c['duration'], c['width'], c['height']) for c in uploaded]}")

        settings_a = {
            "provider": "mock",
            "count": 3,
            "duration_sec": 15,
            "lyric_style": "auto",
            "start_strategy": "segment",
            "reuse": "balanced",
        }
        estimate = api.call("POST", f"/api/projects/{pid}/estimate", json=settings_a)
        log(
            f"estimate A: new {estimate['new_clips']} / unique {estimate['unique_clips']} / shots "
            f"{estimate['shots_total']}, total ${estimate['total_usd']}, variety {estimate['variety_score']}"
        )
        cheapest = sorted(
            (r for r in estimate["comparison"] if r["provider"] not in ("mock", "library")),
            key=lambda r: r["total_usd"],
        )[:3]
        log(f"same plan on APIs: {[(r['provider'], r['model'], r['total_usd']) for r in cheapest]}")
        checks.expect(estimate["within_budget"] and estimate["total_usd"] == 0, "mock estimate should be free")

        batch_a, timings["batch_a"] = run_batch(api, pid, "A", settings_a, args.batch_timeout, checks)
        styles = {v["plan"]["lyric_style"] for v in batch_a["videos"]}
        checks.expect(len(styles) == 3, f"batch A lyric styles should differ: {styles}")
        settings_b = {
            "provider": "library",
            "count": 3,
            "duration_sec": 15,
            "lyric_style": "karaoke",
            "start_strategy": "varied",
        }
        batch_b, timings["batch_b"] = run_batch(api, pid, "B", settings_b, args.batch_timeout, checks)
        upload_ids = {c["id"] for c in uploaded}
        for video in batch_b["videos"]:
            checks.expect(
                {s["clip_id"] for s in video["plan"]["shots"]} <= upload_ids,
                f"batch B video {video['index']} uses non-uploaded clips",
            )

        reports += collect(api, batch_a, "A", out, checks, artist, title)
        reports += collect(api, batch_b, "B", out, checks, artist, title)
        download_zip(
            api,
            batch_a["id"],
            out / "batch_A.zip",
            [r.file for r in reports if r.label == "A" and r.file],
            checks,
            signed_link=True,
        )
        download_zip(
            api, batch_b["id"], out / "batch_B.zip", [r.file for r in reports if r.label == "B" and r.file], checks
        )

        if not args.skip_edit:
            target = batch_a["videos"][0]
            detail = api.call("GET", f"/api/videos/{target['id']}")
            plan = detail["plan"]
            visible = [
                ln
                for ln in detail["lyric_lines"]
                if ln["start"] is not None and plan["start"] <= ln["start"] < plan["start"] + plan["duration"] - 2
            ]
            line = visible[0]
            new_style = "karaoke" if plan["lyric_style"] != "karaoke" else "minimal"
            swap_clip = uploaded[1]["id"]
            edit = {
                "lyric_style": new_style,
                "lyric_offsets": {
                    line["id"]: {"start": round(line["start"] + 0.5, 3), "end": round(line["end"] + 0.5, 3)}
                },
                "shots": [{"index": 0, "clip_id": swap_clip}],
            }
            log(f"edit A#1: style {plan['lyric_style']} → {new_style}, line {line['id']} +0.5 s")
            log(f"edit A#1: shot 0 → clip {swap_clip[:8]}")
            started = time.monotonic()
            api.call("PUT", f"/api/videos/{target['id']}/edit", json=edit)
            edited = api.wait(
                f"/api/videos/{target['id']}",
                lambda b: True if b["status"] in ("done", "failed") else f"{b['status']} {b['progress']:.2f}",
                timeout=args.batch_timeout,
                what="re-render",
            )
            timings["edit_rerender"] = time.monotonic() - started
            checks.expect(edited["status"] == "done", f"edited video {edited['status']}: {edited['error']}")
            checks.expect(edited["render_version"] == 2, f"render_version {edited['render_version']}")
            checks.expect(edited["plan"]["lyric_style"] == new_style, "edited style not applied")
            checks.expect(edited["plan"]["shots"][0]["clip_id"] == swap_clip, "clip swap not applied")
            moved = next(ln for ln in edited["lyric_lines"] if ln["id"] == line["id"])
            checks.expect(abs(moved["start"] - (line["start"] + 0.5)) < 0.01, "lyric offset not applied")
            report = VideoReport(
                "A-edit",
                0,
                target["id"],
                new_style,
                edited["plan"]["color_grade"],
                float(edited["plan"]["start"]),
                len(edited["plan"]["shots"]),
            )
            report.quality = bool((edited.get("quality") or {}).get("passed"))
            path = download(api, target["id"], out / "A" / "01_v2.mp4", checks)
            validate_file(report, path, DEMO_DIR / "demo_song.mp3", checks)
            reports.append(report)
            final = api.call("GET", f"/api/batches/{batch_a['id']}")
            checks.expect(final["status"] == "completed", f"batch A after edit: {final['status']}")

        dashboard = api.call("GET", "/api/dashboard")
        log(f"dashboard: videos {dashboard['videos_done']}/{dashboard['videos_total']}, usage {dashboard['usage']}")
    except E2EFailure as exc:
        checks.failures.append(str(exc))
        log(f"ABORTED: {exc}")
    except httpx.HTTPError as exc:
        checks.failures.append(f"HTTP error: {exc!r}")
        log(f"ABORTED: cannot reach the API at {args.api}: {exc!r}")

    print()
    if reports:
        print_table(reports)
    timings["total"] = time.monotonic() - t0
    print("timings: " + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items()))
    print(f"files: {out}")
    if checks.failures:
        print(f"\nE2E FAILED ({len(checks.failures)} problem(s)):")
        for failure in checks.failures:
            print(f"  - {failure}")
        return 1
    print(f"\nE2E PASSED: {sum(r.ok for r in reports)}/{len(reports)} videos verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
