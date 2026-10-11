"""Song analysis for the video planner and lyric timing (ARCHITECTURE.md §6.1).

``analyze_audio(path)`` decodes a song, analyses it at 22.05 kHz mono and
returns an **AudioAnalysis** dict made only of JSON-native types (``float``,
``int``, ``str``, ``bool``, ``list``, ``dict``, ``None``). All times are in
seconds from the start of the song.

Every result is a heuristic meant to give the UI a sensible default that the
user can override:

- tempo / beats: librosa onset envelope + dynamic-programming beat tracker.
  The BPM is refined from the mean inter-beat interval (the tracker's own
  estimate is quantised to the STFT hop). The beat grid is extended at the
  same period into quiet intros/outros the tracker trims away.
- downbeats: 4/4 assumed. The bar phase is the beat phase with the strongest
  accumulated low-frequency onsets/energy (kick, bass) plus harmonic change
  (chords usually change on the "one").
- sections: Foote novelty on a self-similarity matrix of beat-synchronous
  chroma + MFCC + loudness, plus a loudness-change novelty; boundaries are
  snapped to downbeats and sections are at least ~6 s long. Labels come from
  energy, repetition (recurrence to other parts of the song) and position.
- vocal activity: level of the *harmonic* (HPSS) component in the
  300-3500 Hz band relative to the song, weighted by its harmonic share. This
  is not source separation: sustained loud mid-range instruments also score.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import librosa
import numpy as np
import soundfile as sf
from scipy import ndimage
from scipy.spatial.distance import cdist

from app.services.errors import AnalysisError

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = 1

SR = 22050
N_FFT = 2048
HOP = 512
FRAME_SEC = HOP / SR

LOUDNESS_HOP = 0.5
VOCAL_HOP = 0.1
WAVEFORM_POINTS = 1200
SEGMENT_DURATIONS = (15, 30, 60)

FALLBACK_BPM = 120.0
MIN_DURATION = 1.0
MIN_SECTION_SEC = 6.0
MIN_BEATS_FOR_GRID = 4
MIN_UNITS_FOR_SEGMENTATION = 16
BOUNDARY_NOVELTY = 0.2  # ~ a 3 dB loudness step, or a clear change of harmony/timbre
CHORUS_WITHIN_DB = 6.0  # chorus energy score falls from 1 (loudest section) to 0 at 6 dB below
QUIET_SECTION_DB = 6.0  # sections this far below the loudest one count as quiet

DB_AMIN = 1e-5  # -100 dBFS floor
SILENCE_DB = -60.0  # frames quieter than this (dBFS) count as silence
ENERGY_RANGE_DB = 30.0  # energy 0..1 maps [ref - 30 dB, ref] where ref = loud level of the song
QUIET_BELOW_DB = 9.0  # quiet part: this much below the song's loud level
HIGHLIGHT_WITHIN_DB = 3.0  # highlight: within this much of the song's loud level

VOCAL_BAND_HZ = (300.0, 3500.0)
LOW_BAND_HZ = (30.0, 150.0)

FFMPEG_TIMEOUT = 300
FFPROBE_TIMEOUT = 60

# Krumhansl-Kessler key profiles (C major / C minor).
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


# --- public API ----------------------------------------------------------------------


def analyze_audio(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Analyse the song at ``path`` and return an AudioAnalysis dict (§6.1).

    List ordering: ``chorus_candidates`` best score first, ``highlights``
    loudest first, ``quiet_parts`` and ``sections`` chronological,
    ``suggested_segments`` by duration (15, 30, 60; only lengths that fit).

    Raises :class:`AnalysisError` when the file is missing, has no audio
    stream, or cannot be decoded.
    """
    started = time.perf_counter()
    src = Path(path)
    if not src.is_file():
        raise AnalysisError("音声ファイルが見つかりません。")

    meta = _probe_audio(src)
    y = _decode(src)
    if meta is None:
        meta = _soundfile_meta(src)

    # Whole milliseconds, so the reported duration and every per-hop array agree
    # (len(rms_db) == ceil(duration / 0.5), len(values) == ceil(duration / 0.1)).
    duration_ms = len(y) * 1000 // SR
    y = y[: duration_ms * SR // 1000]
    duration = duration_ms / 1000.0
    if duration < MIN_DURATION:
        raise AnalysisError("音声が短すぎるため解析できません（1秒以上必要です）。")

    feats = _compute_features(y, duration)
    beats = _analyze_beats(feats)
    sections = _segment(feats, beats)
    vocal = _vocal_activity(feats)
    loud_blocks = _block_loudness_db(y)
    quiet_parts = _quiet_parts(loud_blocks, feats, beats, duration)
    highlights = _highlights(loud_blocks, feats, beats, duration)
    chorus = [
        {"start": s["start"], "end": s["end"], "score": s["_score"]} for s in sections if s["is_chorus_candidate"]
    ]
    # Near-equal scores (repeated choruses) -> the earlier occurrence first.
    chorus.sort(key=lambda c: (-round(c["score"], 2), c["start"]))
    segments = _suggest_segments(duration, beats, sections, chorus, loud_blocks, feats, vocal)
    mood = _mood(feats, beats, sections)

    result: dict[str, Any] = {
        "version": ANALYSIS_VERSION,
        "duration": _r(duration),
        "sample_rate": meta.get("sample_rate"),
        "channels": meta.get("channels"),
        "codec": meta.get("codec"),
        "bit_rate": meta.get("bit_rate"),
        "bpm": _r(beats.bpm, 2),
        "bpm_confidence": _r(beats.confidence),
        "beats": [_r(t) for t in beats.times],
        "downbeats": [_r(t) for t in beats.downbeats],
        "loudness": {"hop": LOUDNESS_HOP, "rms_db": [_r(v, 2) for v in loud_blocks]},
        "waveform": {"points": WAVEFORM_POINTS, "peaks": _waveform(y)},
        "sections": [
            {
                "start": s["start"],
                "end": s["end"],
                "label": s["label"],
                "energy": s["energy"],
                "is_chorus_candidate": s["is_chorus_candidate"],
            }
            for s in sections
        ],
        "chorus_candidates": chorus,
        "highlights": highlights,
        "quiet_parts": quiet_parts,
        "suggested_segments": segments,
        "vocal_activity": {"hop": VOCAL_HOP, "values": [_r(v) for v in vocal]},
        "mood": mood,
    }
    result = _plain(result)
    logger.info(
        "audio analysis: %.1f s of audio in %.2f s (bpm %.1f, %d sections)",
        duration,
        time.perf_counter() - started,
        beats.bpm,
        len(sections),
    )
    return result


# --- decoding & metadata -----------------------------------------------------------------


def _probe_audio(path: Path) -> dict[str, Any] | None:
    """Container metadata of the first audio stream via ffprobe.

    Returns ``None`` when ffprobe is unavailable or cannot read the file (the
    decoder then gets its own chance). Raises :class:`AnalysisError` when the
    file is readable but has no audio stream.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=FFPROBE_TIMEOUT, check=False)
        data = json.loads(proc.stdout or b"{}") if proc.returncode == 0 else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        data = None
    if not data:
        return None
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise AnalysisError("ファイルに音声トラックが含まれていません。")
    fmt = data.get("format") or {}
    return {
        "sample_rate": _int_or_none(audio.get("sample_rate")),
        "channels": _int_or_none(audio.get("channels")),
        "codec": audio.get("codec_name"),
        # Stream bit rate (e.g. 1411200 for 44.1 kHz/16-bit stereo PCM); the
        # container's overall bit rate when the stream does not declare one.
        "bit_rate": _int_or_none(audio.get("bit_rate")) or _int_or_none(fmt.get("bit_rate")),
    }


def _soundfile_meta(path: Path) -> dict[str, Any]:
    try:
        info = sf.info(str(path))
    except Exception:  # metadata is best-effort once decoding succeeded
        return {"sample_rate": None, "channels": None, "codec": None, "bit_rate": None}
    return {
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "codec": str(info.subtype).lower() or None,
        "bit_rate": None,
    }


def _decode(path: Path) -> np.ndarray:
    """Mono float32 samples at ``SR``: libsndfile first, FFmpeg for anything else (m4a/aac, video...)."""
    try:
        y, _ = librosa.load(path, sr=SR, mono=True)
    except Exception as exc:  # any libsndfile failure: unsupported container/codec, odd headers
        logger.info("soundfile could not decode %s (%s); transcoding with ffmpeg", path.name, exc)
        y = _decode_with_ffmpeg(path)
    y = np.asarray(y, dtype=np.float32)
    if y.ndim != 1 or y.size == 0:
        raise AnalysisError("音声データを読み取れませんでした（無音または空のファイルです）。")
    if not np.all(np.isfinite(y)):
        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    return y


def _decode_with_ffmpeg(path: Path) -> np.ndarray:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AnalysisError(
            "この形式の音声を読み込むには FFmpeg が必要です。サーバーに FFmpeg をインストールしてください。"
        )
    with tempfile.TemporaryDirectory(prefix="mvf-audio-") as tmp:
        wav = Path(tmp) / "decoded.wav"
        cmd = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR),
            "-c:a", "pcm_f32le", str(wav),
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AnalysisError("音声のデコードがタイムアウトしました。") from exc
        if proc.returncode != 0 or not wav.is_file():
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if proc.stderr else []
            logger.warning("ffmpeg could not decode %s: %s", path.name, " ".join(tail))
            raise AnalysisError("音声ファイルを読み取れませんでした（形式が不正か、ファイルが破損しています）。")
        try:
            y, _ = sf.read(str(wav), dtype="float32", always_2d=False)
        except Exception as exc:
            raise AnalysisError("音声ファイルを読み取れませんでした（デコード結果が不正です）。") from exc
    return np.asarray(y, dtype=np.float32)


# --- shared features -------------------------------------------------------------------------


@dataclass
class _Features:
    duration: float
    n_frames: int
    frame_times: np.ndarray  # (n,) centre time of each STFT frame
    frame_db: np.ndarray  # (n,) RMS dBFS
    S: np.ndarray  # (1 + N_FFT/2, n) magnitude spectrogram
    freqs: np.ndarray
    oenv: np.ndarray  # (n,) onset strength
    chroma: np.ndarray  # (12, n)
    mfcc: np.ndarray  # (13, n)
    silent: bool  # whole track below SILENCE_DB
    loud_ref_db: float  # "loud" frame level of the song (95th percentile of active frames)
    active: np.ndarray  # (n,) bool, frames above the silence threshold
    span: slice  # first..last active frame (contiguous, for time-structure features)

    def energy_of_db(self, db: np.ndarray | float) -> np.ndarray:
        """Map dBFS to 0..1 relative to the song's loud level (silence -> 0)."""
        if self.silent:
            return np.zeros_like(np.asarray(db, dtype=np.float64))
        floor = self.loud_ref_db - ENERGY_RANGE_DB
        out = np.clip((np.asarray(db, dtype=np.float64) - floor) / ENERGY_RANGE_DB, 0.0, 1.0)
        return np.where(np.asarray(db) < SILENCE_DB, 0.0, out)

    def frames_in(self, start: float, end: float) -> np.ndarray:
        return (self.frame_times >= start) & (self.frame_times < end)


def _compute_features(y: np.ndarray, duration: float) -> _Features:
    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP)).astype(np.float32)
    n = S.shape[1]
    rms = librosa.feature.rms(y=y, frame_length=N_FFT, hop_length=HOP)[0][:n]
    if rms.shape[0] < n:
        rms = np.pad(rms, (0, n - rms.shape[0]))
    frame_db = 20.0 * np.log10(np.maximum(rms, DB_AMIN))
    power = S**2
    mel_db = librosa.power_to_db(librosa.feature.melspectrogram(S=power, sr=SR, n_mels=128))
    oenv = librosa.onset.onset_strength(S=mel_db, sr=SR, hop_length=HOP)[:n]
    # tuning=0: tuning estimation costs ~3 s per minute of audio and barely
    # changes 12-bin chroma used for structure and mode.
    chroma = librosa.feature.chroma_stft(S=power, sr=SR, tuning=0.0)
    mfcc = librosa.feature.mfcc(S=mel_db, n_mfcc=13)

    active = frame_db > SILENCE_DB
    silent = not bool(np.any(active)) or float(np.percentile(frame_db, 99)) < SILENCE_DB
    loud_ref = float(np.percentile(frame_db[active], 95)) if not silent else SILENCE_DB
    active_idx = np.flatnonzero(active)
    span = slice(int(active_idx[0]), int(active_idx[-1]) + 1) if active_idx.size else slice(0, n)
    return _Features(
        duration=duration,
        n_frames=n,
        frame_times=librosa.frames_to_time(np.arange(n), sr=SR, hop_length=HOP),
        frame_db=frame_db,
        S=S,
        freqs=librosa.fft_frequencies(sr=SR, n_fft=N_FFT),
        oenv=oenv,
        chroma=chroma,
        mfcc=mfcc,
        silent=silent,
        loud_ref_db=loud_ref,
        active=active,
        span=span,
    )


def _block_loudness_db(y: np.ndarray) -> np.ndarray:
    """RMS dBFS per ``LOUDNESS_HOP`` block (the last block may be shorter)."""
    block = round(SR * LOUDNESS_HOP)
    starts = np.arange(0, len(y), block)
    sq = y.astype(np.float64) ** 2
    sums = np.add.reduceat(sq, starts)
    counts = np.diff(np.append(starts, len(y)))
    rms = np.sqrt(sums / np.maximum(counts, 1))
    return 20.0 * np.log10(np.maximum(rms, DB_AMIN))


def _waveform(y: np.ndarray) -> list[float]:
    edges = np.linspace(0, len(y), WAVEFORM_POINTS + 1).astype(np.int64)
    edges = np.minimum(edges[:-1], len(y) - 1)
    peaks = np.maximum.reduceat(np.abs(y), edges)
    top = float(peaks.max()) if peaks.size else 0.0
    if top <= 0.0:
        return [0.0] * WAVEFORM_POINTS
    return [_r(p / top, 4) for p in peaks]


# --- beats & downbeats ----------------------------------------------------------------------


@dataclass
class _Beats:
    bpm: float
    confidence: float
    times: np.ndarray  # beat grid (seconds), extended over quiet intro/outro
    frames: np.ndarray  # same grid as STFT frame indices
    downbeats: np.ndarray
    period: float  # seconds per beat
    phase: int  # index of the first downbeat in ``times``

    @property
    def bar(self) -> float:
        return 4.0 * self.period


def _analyze_beats(f: _Features) -> _Beats:
    empty = np.zeros(0)
    fallback = _Beats(FALLBACK_BPM, 0.0, empty, empty.astype(np.int64), empty, 60.0 / FALLBACK_BPM, 0)
    if f.silent:
        return fallback

    env = f.oenv[f.span]
    autocorr = _onset_autocorrelation(env)
    tempo, tracked = librosa.beat.beat_track(onset_envelope=f.oenv, sr=SR, hop_length=HOP, units="frames")
    tracker_bpm = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.0
    tracked = np.unique(np.asarray(tracked, dtype=np.int64))
    if tracked.size >= MIN_BEATS_FOR_GRID:
        better = _better_tempo(autocorr, _steady_period(tracked))
        if better is not None:
            _, retracked = librosa.beat.beat_track(
                onset_envelope=f.oenv, sr=SR, hop_length=HOP, bpm=better, units="frames"
            )
            retracked = np.unique(np.asarray(retracked, dtype=np.int64))
            if retracked.size >= MIN_BEATS_FOR_GRID:
                tracked = retracked
    if tracked.size < MIN_BEATS_FOR_GRID:
        bpm = tracker_bpm if 30.0 <= tracker_bpm <= 300.0 else FALLBACK_BPM
        return _Beats(bpm, 0.0, empty, empty.astype(np.int64), empty, 60.0 / bpm, 0)

    period = _steady_period(tracked)
    tracked_times = librosa.frames_to_time(tracked, sr=SR, hop_length=HOP)
    confidence = _bpm_confidence(f, env, autocorr, tracked, period)
    times = _extend_grid(tracked_times, period, f)
    frames = np.clip(np.round(times / FRAME_SEC).astype(np.int64), 0, f.n_frames - 1)
    phase = _downbeat_phase(f, frames)
    return _Beats(60.0 / period, confidence, times, frames, times[phase::4], period, phase)


def _steady_period(tracked: np.ndarray) -> float:
    """Beat period in seconds: mean of the inter-beat intervals near the median.

    Averaging many intervals undoes the one-frame (23 ms) quantisation of
    tracked beats, e.g. 120 BPM instead of the tracker's 117.45.
    """
    ibis = np.diff(tracked.astype(np.float64)) * FRAME_SEC
    median = float(np.median(ibis))
    steady = ibis[np.abs(ibis - median) <= 0.15 * median]
    return float(np.mean(steady)) if steady.size else median


def _onset_autocorrelation(env: np.ndarray) -> np.ndarray:
    """Unbiased autocorrelation of the (lightly smoothed) onset envelope, 1 at lag 0."""
    x = ndimage.gaussian_filter1d(env.astype(np.float64), 1.0)
    x = x - x.mean()
    n = x.size
    if n < 4:
        return np.zeros(max(n, 1))
    ac = librosa.autocorrelate(x) / (n - np.arange(n))
    return ac / ac[0] if ac[0] > 0 else np.zeros(n)


def _comb(autocorr: np.ndarray, period: float) -> float:
    """Mean autocorrelation at 1..4 beat lags (±1 frame), i.e. how periodic onsets are at ``period`` s."""
    lag = period / FRAME_SEC
    usable = autocorr.size // 2
    peaks = []
    for k in range(1, 5):
        centre = k * lag
        if centre + 1 >= usable:
            break
        lo, hi = max(1, math.floor(centre) - 1), math.ceil(centre) + 2
        peaks.append(float(autocorr[lo:hi].max()))
    return float(np.mean(peaks)) if peaks else 0.0


def _tempo_prior(bpm: float) -> float:
    """Log-normal preference for ~120 BPM (one octave std), as librosa's tempo estimator."""
    return math.exp(-0.5 * math.log2(bpm / 120.0) ** 2)


def _better_tempo(autocorr: np.ndarray, period: float) -> float | None:
    """BPM of a clearly more periodic tempo related to the tracked one (x2, x3/2, x4/3 ...).

    The tracker's tempo estimate is unreliable on short files (its 8 s
    autocorrelation window is longer than the audio), e.g. 160 instead of 120.
    """
    current_bpm = 60.0 / period
    current = _comb(autocorr, period) * _tempo_prior(current_bpm)
    best_bpm, best = None, current
    for ratio in (4 / 3, 3 / 4, 3 / 2, 2 / 3, 2.0, 0.5):
        bpm = current_bpm / ratio
        if not 50.0 <= bpm <= 220.0:
            continue
        score = _comb(autocorr, period * ratio) * _tempo_prior(bpm)
        if score > best:
            best_bpm, best = bpm, score
    if best_bpm is not None and best >= 0.3 and best > 1.2 * max(current, 0.0):
        return best_bpm
    return None


def _bpm_confidence(f: _Features, env: np.ndarray, autocorr: np.ndarray, tracked: np.ndarray, period: float) -> float:
    """0..1 from beat regularity, onset periodicity at the beat period and beat salience."""
    # Regularity: inter-beat jitter beyond the one-frame quantisation of the tracker.
    ibis = np.diff(tracked.astype(np.float64)) * FRAME_SEC
    jitter = max(0.0, float(np.std(ibis)) - 0.5 * FRAME_SEC)
    regularity = float(np.clip(1.0 - jitter / (0.1 * period), 0.0, 1.0))

    periodicity = float(np.clip(_comb(autocorr, period) / 0.6, 0.0, 1.0))

    # Salience: onset strength at the beats vs. on average.
    mean_env = float(np.mean(env)) if env.size else 0.0
    salience = 0.0
    if mean_env > 0:
        at_beats = float(np.mean(f.oenv[np.clip(tracked, 0, f.n_frames - 1)]))
        salience = float(np.clip((at_beats / mean_env - 1.0) / 1.5, 0.0, 1.0))

    # Coverage: share of the non-silent span of the song that the tracker followed.
    active_span = (f.span.stop - f.span.start) * FRAME_SEC
    tracked_span = float(tracked[-1] - tracked[0]) * FRAME_SEC
    coverage = float(np.clip(tracked_span / active_span, 0.0, 1.0)) if active_span > 0 else 0.0
    count_factor = min(1.0, len(tracked) / 16.0)

    score = math.sqrt(regularity) * (0.55 * periodicity + 0.45 * salience) * math.sqrt(coverage) * count_factor
    return float(np.clip(score, 0.0, 1.0))


def _extend_grid(tracked_times: np.ndarray, period: float, f: _Features) -> np.ndarray:
    """Continue the beat grid at ``period`` over the non-silent intro/outro the tracker trimmed."""
    lo = max(0.0, float(f.frame_times[f.span.start]) - 0.25 * period)
    hi = min(f.duration - 0.05, float(f.frame_times[f.span.stop - 1]))
    first, last = float(tracked_times[0]), float(tracked_times[-1])
    n_before = math.floor((first - lo) / period + 1e-6)
    n_after = math.floor((hi - last) / period + 1e-6)
    before = first - period * np.arange(max(n_before, 0), 0, -1)
    after = last + period * np.arange(1, max(n_after, 0) + 1)
    grid = np.concatenate([before, tracked_times, after])
    return grid[(grid >= 0.0) & (grid < f.duration)]


def _downbeat_phase(f: _Features, frames: np.ndarray) -> int:
    """Which of the 4 beat phases starts a bar (4/4 assumed)."""
    if frames.size < 8:
        return 0
    band = (f.freqs >= LOW_BAND_HZ[0]) & (f.freqs <= LOW_BAND_HZ[1])
    low_db = 10.0 * np.log10(np.sum(f.S[band].astype(np.float64) ** 2, axis=0) + 1e-10)
    low_flux = np.maximum(0.0, np.diff(low_db, prepend=low_db[0]))
    n = f.n_frames

    def window_max(x: np.ndarray, before: int, after: int) -> np.ndarray:
        return np.array([x[max(0, i - before) : min(n, i + after + 1)].max() for i in frames])

    def window_mean(x: np.ndarray, before: int, after: int) -> np.ndarray:
        return np.array([x[max(0, i - before) : min(n, i + after + 1)].mean() for i in frames])

    low_onset = window_max(low_flux, 2, 3)
    low_level = window_mean(low_db, 0, 4)
    low_level = low_level - low_level.min()
    # Harmonic change: chroma of the beat that starts here vs. the previous beat.
    bounds = np.append(frames, n)
    beat_chroma = np.stack([f.chroma[:, a : max(a + 1, b)].mean(axis=1) for a, b in itertools.pairwise(bounds)])
    norms = np.linalg.norm(beat_chroma, axis=1) + 1e-9
    cos = np.sum(beat_chroma[1:] * beat_chroma[:-1], axis=1) / (norms[1:] * norms[:-1])
    chroma_change = np.concatenate([[0.0], 1.0 - cos])

    def phase_contrast(values: np.ndarray) -> np.ndarray:
        means = np.array([values[p::4].mean() if values[p::4].size else 0.0 for p in range(4)])
        scale = float(np.mean(np.abs(values))) + 1e-9
        return (means - means.mean()) / scale

    score = 1.0 * phase_contrast(low_onset) + 1.0 * phase_contrast(chroma_change) + 0.5 * phase_contrast(low_level)
    return int(np.argmax(score))


# --- sections ----------------------------------------------------------------------------------


def _segment(f: _Features, beats: _Beats) -> list[dict[str, Any]]:
    """Structural sections covering [0, duration] (internal ``_score`` = chorus score)."""
    duration = f.duration
    if f.silent:
        return [_section(0.0, duration, "break", 0.0, False, 0.0)]

    unit_frames = beats.frames if beats.frames.size >= MIN_UNITS_FOR_SEGMENTATION else _uniform_frames(f, 0.5)
    bounds = np.unique(np.concatenate([[0], unit_frames, [f.n_frames]])).astype(np.int64)
    unit_starts = librosa.frames_to_time(bounds[:-1], sr=SR, hop_length=HOP)
    unit_starts[0] = 0.0

    boundaries: list[float] = []
    feats: np.ndarray | None = None
    if len(unit_starts) >= MIN_UNITS_FOR_SEGMENTATION and duration >= 2 * MIN_SECTION_SEC:
        feats = _unit_features(f, bounds)
        unit_len = float(np.median(np.diff(unit_starts))) or 0.5
        half = int(np.clip(round(4.0 / unit_len), 4, 12))
        novelty = 0.6 * _ssm_novelty(feats, half) + 0.4 * _energy_novelty(f, bounds, half)
        boundaries = _pick_boundaries(novelty, unit_starts, duration, beats)

    edges = [0.0, *boundaries, duration]
    spans = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    repetition = _repetition(feats, unit_starts, spans) if feats is not None else [0.0] * len(spans)
    return _label_sections(f, spans, repetition)


def _uniform_frames(f: _Features, step: float) -> np.ndarray:
    return np.arange(0, f.n_frames, max(1, round(step / FRAME_SEC)), dtype=np.int64)[1:]


def _sync_mean(x: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    sums = np.add.reduceat(x, bounds[:-1], axis=-1)
    return sums / np.maximum(np.diff(bounds), 1)


def _zscore_rows(x: np.ndarray) -> np.ndarray:
    return (x - x.mean(axis=1, keepdims=True)) / (x.std(axis=1, keepdims=True) + 1e-9)


def _unit_features(f: _Features, bounds: np.ndarray) -> np.ndarray:
    """Per-unit (beat) feature vectors: chroma, timbre (MFCC 1-12), loudness; each block weighted ~equally."""
    chroma = _sync_mean(f.chroma, bounds)
    chroma = chroma / (np.linalg.norm(chroma, axis=0, keepdims=True) + 1e-9)
    timbre = _zscore_rows(_sync_mean(f.mfcc[1:], bounds)) / math.sqrt(f.mfcc.shape[0] - 1)
    level = _zscore_rows(_sync_mean(f.frame_db[np.newaxis, :], bounds))
    return np.vstack([chroma, timbre, level]).astype(np.float64)


def _ssm_novelty(feats: np.ndarray, half: int) -> np.ndarray:
    """Foote checkerboard novelty: high where the past and future blocks differ."""
    n = feats.shape[1]
    dist = cdist(feats.T, feats.T)
    sigma = float(np.median(dist[dist > 0])) if np.any(dist > 0) else 1.0
    ssm = np.exp(-(dist**2) / (2.0 * sigma**2))
    offsets = np.arange(-half, half) + 0.5
    taper = np.exp(-0.5 * (offsets / (0.5 * half)) ** 2)
    sign = np.where(offsets < 0, -1.0, 1.0)
    kernel = np.outer(sign, sign) * np.outer(taper, taper)
    novelty = np.zeros(n)
    for c in range(1, n):
        lo, hi = max(0, c - half), min(n, c + half)
        k = kernel[lo - (c - half) : hi - (c - half), lo - (c - half) : hi - (c - half)]
        block = ssm[lo:hi, lo:hi]
        pos, neg = k > 0, k < 0
        if not np.any(neg):
            continue
        within = float(np.sum(k[pos] * block[pos]) / np.sum(k[pos]))
        across = float(np.sum(-k[neg] * block[neg]) / np.sum(-k[neg]))
        novelty[c] = max(0.0, within - across)
    return np.clip(novelty / 0.5, 0.0, 1.0)


def _energy_novelty(f: _Features, bounds: np.ndarray, half: int) -> np.ndarray:
    """Loudness step between the ``half`` units before and after each unit start (6 dB -> 1)."""
    power = _sync_mean(10.0 ** (f.frame_db / 10.0), bounds)
    n = power.size
    out = np.zeros(n)
    for c in range(1, n):
        before = power[max(0, c - half) : c].mean()
        after = power[c : min(n, c + half)].mean()
        out[c] = abs(10.0 * np.log10((after + 1e-10) / (before + 1e-10)))
    return np.clip(out / 6.0, 0.0, 1.0)


def _pick_boundaries(novelty: np.ndarray, unit_starts: np.ndarray, duration: float, beats: _Beats) -> list[float]:
    """Strongest novelty peaks, snapped to downbeats, at least ``MIN_SECTION_SEC`` apart."""
    # Both novelty terms are on absolute scales (similarity contrast, dB step),
    # so a fixed threshold works; spacing and label merging limit over-segmentation.
    threshold = BOUNDARY_NOVELTY
    peaks = [
        i
        for i in range(1, len(novelty) - 1)
        if novelty[i] >= threshold and novelty[i] >= novelty[i - 1] and novelty[i] >= novelty[i + 1]
    ]
    candidates = sorted(((float(novelty[i]), float(unit_starts[i])) for i in peaks), reverse=True)
    snapped = [(strength, _snap(t, beats.downbeats, 0.6 * beats.bar)) for strength, t in candidates]
    chosen: list[float] = []
    for _, t in snapped:
        if t < MIN_SECTION_SEC or duration - t < MIN_SECTION_SEC:
            continue
        if all(abs(t - c) >= MIN_SECTION_SEC for c in chosen):
            chosen.append(t)
    return sorted(chosen)


def _repetition(feats: np.ndarray, unit_starts: np.ndarray, spans: list[tuple[float, float]]) -> list[float]:
    """How strongly each section recurs in a *non-adjacent* section (0..1).

    Pairwise share of recurrence affinity between two sections; a verse that
    leads into a chorus is similar to it as well, so immediate neighbours do
    not count. Symmetric: two occurrences of a chorus get the same value.
    """
    stacked = librosa.feature.stack_memory(feats, n_steps=4, mode="edge")
    try:
        rec = librosa.segment.recurrence_matrix(stacked, width=4, mode="affinity", sym=True)
    except Exception:  # too few units for a k-NN recurrence graph
        return [0.0] * len(spans)
    rec = np.asarray(rec.toarray() if hasattr(rec, "toarray") else rec, dtype=np.float64)
    masks = [(unit_starts >= start - 1e-6) & (unit_starts < end - 1e-6) for start, end in spans]
    totals = [float(rec[mask].sum()) for mask in masks]
    out = [0.0] * len(spans)
    for i, mask_i in enumerate(masks):
        for j in range(i + 2, len(masks)):
            denom = totals[i] + totals[j]
            if denom <= 0:
                continue
            shared = 2.0 * float(rec[np.ix_(mask_i, masks[j])].sum()) / denom
            out[i] = max(out[i], shared)
            out[j] = max(out[j], shared)
    return out


def _section(start: float, end: float, label: str, energy: float, chorus: bool, score: float) -> dict[str, Any]:
    return {
        "start": _r(start),
        "end": _r(end),
        "label": label,
        "energy": _r(energy),
        "is_chorus_candidate": chorus,
        "_score": _r(score),
    }


def _label_sections(f: _Features, spans: list[tuple[float, float]], repetition: list[float]) -> list[dict[str, Any]]:
    """Energy (0..1), chorus score and label per span.

    chorus  = within a few dB of the loudest section, best if it recurs elsewhere;
    intro   = first section when quieter than the song's middle;
    outro   = last section unless it is a chorus;
    bridge  = quiet section between two choruses; break = other quiet middle sections;
    verse   = everything else.
    """
    n = len(spans)
    energies, levels = [], []
    for start, end in spans:
        mask = f.frames_in(start, end)
        db = f.frame_db[mask] if np.any(mask) else np.array([float(np.mean(f.frame_db))])
        energies.append(float(np.mean(f.energy_of_db(db))))
        levels.append(float(10.0 * np.log10(np.mean(10.0 ** (db / 10.0)) + 1e-12)))
    if n == 1:
        return [_section(spans[0][0], spans[0][1], "verse", energies[0], False, 0.0)]

    level = np.array(levels)
    below_loudest = float(level.max()) - level
    loudness_score = np.clip(1.0 - below_loudest / CHORUS_WITHIN_DB, 0.0, 1.0)
    recurrence = np.clip(np.array(repetition) / 0.5, 0.0, 1.0)
    length = np.array([min(1.0, (end - start) / 16.0) for start, end in spans])
    score = np.clip(0.6 * loudness_score + 0.3 * recurrence + 0.1 * length, 0.0, 1.0)
    dynamic = float(below_loudest.max()) >= 1.5  # otherwise loudness cannot tell parts apart

    chorus = (loudness_score >= 0.6) & (score >= 0.6) if dynamic else recurrence >= 0.8
    if not np.any(chorus):
        best = int(np.argmax(score))
        chorus[best] = score[best] >= 0.5
    quiet = below_loudest >= QUIET_SECTION_DB
    median_level = float(np.median(level))
    chorus_idx = np.flatnonzero(chorus)

    labels: list[str] = []
    for i in range(n):
        if chorus[i]:
            label = "chorus"
        elif i == 0 and (quiet[i] or level[i] < median_level):
            label = "intro"
        elif i == n - 1:
            label = "outro"
        elif quiet[i]:
            between = bool(np.any(chorus_idx < i) and np.any(chorus_idx > i))
            label = "bridge" if between else "break"
        else:
            label = "verse"
        labels.append(label)

    sections = [
        _section(start, end, labels[i], energies[i], bool(chorus[i]), float(score[i]))
        for i, (start, end) in enumerate(spans)
    ]
    return _merge_alike(sections, f)


def _merge_alike(sections: list[dict[str, Any]], f: _Features) -> list[dict[str, Any]]:
    """Merge neighbours that got the same label and similar energy (one part split by the novelty)."""
    merged: list[dict[str, Any]] = [sections[0]]
    for sec in sections[1:]:
        prev = merged[-1]
        if sec["label"] == prev["label"] and abs(sec["energy"] - prev["energy"]) < 0.12:
            mask = f.frames_in(prev["start"], sec["end"])
            energy = float(np.mean(f.energy_of_db(f.frame_db[mask]))) if np.any(mask) else prev["energy"]
            merged[-1] = _section(
                prev["start"],
                sec["end"],
                prev["label"],
                energy,
                prev["is_chorus_candidate"],
                max(prev["_score"], sec["_score"]),
            )
        else:
            merged.append(sec)
    return merged


# --- loudness regions ------------------------------------------------------------------------


def _regions(mask: np.ndarray, hop: float, *, min_len: float, max_gap: float) -> list[tuple[float, float]]:
    """Contiguous True runs of a block mask as (start, end) seconds, small gaps bridged."""
    runs: list[list[float]] = []
    i, n = 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        start, end = i * hop, j * hop
        if runs and start - runs[-1][1] <= max_gap:
            runs[-1][1] = end
        else:
            runs.append([start, end])
        i = j
    return [(s, e) for s, e in runs if e - s >= min_len]


def _snap(t: float, grid: np.ndarray, tolerance: float) -> float:
    if grid.size == 0:
        return t
    idx = int(np.argmin(np.abs(grid - t)))
    return float(grid[idx]) if abs(float(grid[idx]) - t) <= tolerance else t


def _snap_region(start: float, end: float, beats: _Beats, duration: float) -> tuple[float, float]:
    tol = 0.75
    start = 0.0 if start <= tol else _snap(start, beats.downbeats, tol)
    end = duration if duration - end <= tol else _snap(end, beats.downbeats, tol)
    return start, min(end, duration)


def _smooth_db(blocks: np.ndarray) -> np.ndarray:
    return ndimage.median_filter(blocks, size=3, mode="nearest") if blocks.size >= 3 else blocks


def _quiet_parts(blocks: np.ndarray, f: _Features, beats: _Beats, duration: float) -> list[dict[str, float]]:
    if f.silent:
        return [{"start": 0.0, "end": _r(duration)}]
    smooth = _smooth_db(blocks)
    active = blocks[blocks > SILENCE_DB]
    loud = float(np.percentile(active, 90)) if active.size else SILENCE_DB
    mask = smooth < loud - QUIET_BELOW_DB
    out = []
    for start, end in _regions(mask, LOUDNESS_HOP, min_len=2.0, max_gap=1.0):
        s, e = _snap_region(start, min(end, duration), beats, duration)
        if e - s >= 1.0:
            out.append({"start": _r(s), "end": _r(e)})
    return out


def _highlights(blocks: np.ndarray, f: _Features, beats: _Beats, duration: float) -> list[dict[str, float]]:
    if f.silent:
        return []
    smooth = _smooth_db(blocks)
    active = blocks[blocks > SILENCE_DB]
    loud = float(np.percentile(active, 90)) if active.size else SILENCE_DB
    mask = smooth >= loud - HIGHLIGHT_WITHIN_DB
    out = []
    for start, end in _regions(mask, LOUDNESS_HOP, min_len=3.0, max_gap=1.5):
        s, e = _snap_region(start, min(end, duration), beats, duration)
        a, b = int(s / LOUDNESS_HOP), max(int(s / LOUDNESS_HOP) + 1, math.ceil(e / LOUDNESS_HOP))
        energy = float(np.mean(f.energy_of_db(blocks[a:b])))
        out.append({"start": _r(s), "end": _r(e), "energy": _r(energy)})
    out.sort(key=lambda h: (-h["energy"], h["start"]))
    return out[:5]


# --- suggested segments --------------------------------------------------------------------


def _suggest_segments(
    duration: float,
    beats: _Beats,
    sections: list[dict[str, Any]],
    chorus: list[dict[str, Any]],
    blocks: np.ndarray,
    f: _Features,
    vocal: np.ndarray,
) -> list[dict[str, Any]]:
    """Best window per length in ``SEGMENT_DURATIONS`` that fits, starting on a downbeat."""
    grid = beats.downbeats if beats.downbeats.size else beats.times
    starts = np.unique(np.concatenate([[0.0], grid]))
    block_energy = f.energy_of_db(blocks)
    chorus_total = sum(c["end"] - c["start"] for c in chorus)
    section_starts = np.array([s["start"] for s in sections])
    bar = beats.bar if beats.times.size else 2.0

    def mean_over(values: np.ndarray, hop: float, start: float, end: float) -> float:
        a = int(start / hop)
        b = max(a + 1, math.ceil(end / hop))
        chunk = values[a:b]
        return float(np.mean(chunk)) if chunk.size else 0.0

    out: list[dict[str, Any]] = []
    for length in SEGMENT_DURATIONS:
        if length > duration + 0.05:
            continue
        best: tuple[float, float, dict[str, Any]] | None = None
        for start in starts:
            start = float(start)
            if start + length > duration + 0.05:
                break
            end = min(start + length, duration)
            overlap = sum(max(0.0, min(end, c["end"]) - max(start, c["start"])) for c in chorus)
            coverage = overlap / min(length, chorus_total) if chorus_total > 0 else 0.0
            entry, entry_info = _chorus_entry(start, chorus, bar, length)
            energy = mean_over(block_energy, LOUDNESS_HOP, start, end)
            vocal_mean = mean_over(vocal, VOCAL_HOP, start, end)
            at_section = bool(np.any(np.abs(section_starts - start) <= 0.25))
            score = 0.35 * coverage + 0.25 * entry + 0.2 * energy + 0.1 * vocal_mean + 0.1 * float(at_section)
            facts = {
                "inside": overlap / max(end - start, 1e-9),
                "whole_choruses": sum(1 for c in chorus if c["start"] >= start - 0.25 and c["end"] <= end + 0.25),
                "entry": entry_info,
                "energy": energy,
                "on_downbeat": bool(np.any(np.abs(beats.downbeats - start) < 1e-6)),
            }
            if best is None or score > best[0] + 0.01:  # near ties keep the earlier start
                best = (score, start, facts)
        if best is None:
            continue
        score, start, facts = best
        end = min(start + length, duration)
        out.append(
            {
                "start": _r(start),
                "end": _r(end),
                "duration": length,
                "score": _r(min(1.0, max(0.0, score))),
                "reason": _segment_reason(length, start, end, duration, facts),
            }
        )
    return out


def _chorus_entry(
    start: float, chorus: list[dict[str, Any]], bar: float, length: float
) -> tuple[float, dict[str, float] | None]:
    """How well a window start leads into a chorus: 1 at its first beat, less with a longer lead-in."""
    best, info = 0.0, None
    for c in chorus:
        lead = c["start"] - start
        if abs(lead) <= 0.25:
            value = 1.0
        elif 0 < lead <= 1.05 * bar:
            value = 0.9
        elif 0 < lead <= 2.1 * bar:
            value = 0.75
        elif 0 < lead <= min(length / 3.0, 4.2 * bar):
            value = 0.45
        elif -1.05 * bar <= lead < 0:
            value = 0.5
        else:
            continue
        value *= math.sqrt(max(c["score"], 0.0))
        if value > best:
            best, info = value, {"chorus_start": c["start"], "lead": lead}
    return best, info


def _fmt_time(t: float) -> str:
    total = round(max(0.0, t))
    return f"{total // 60}:{total % 60:02d}"


def _segment_reason(length: int, start: float, end: float, duration: float, facts: dict[str, Any]) -> str:
    parts: list[str] = []
    entry = facts["entry"]
    if start <= 0.25 and end >= duration - 0.25:
        parts.append(f"曲全体をカバーする{length}秒")
    elif entry is not None:
        cs, lead = entry["chorus_start"], entry["lead"]
        if abs(lead) <= 0.25:
            parts.append(f"サビ（{_fmt_time(cs)}〜）の頭から始まる{length}秒")
        elif lead > 0:
            parts.append(f"サビ（{_fmt_time(cs)}〜）の約{lead:.0f}秒前から入り、サビへ向かう{length}秒")
        else:
            parts.append(f"サビ（{_fmt_time(cs)}〜）の途中から始まる{length}秒")
    elif start <= 0.25:
        parts.append(f"曲の冒頭から始まる{length}秒")
    else:
        parts.append(f"{_fmt_time(start)}から始まる{length}秒")

    inside, whole = facts["inside"], facts["whole_choruses"]
    if inside >= 0.9:
        parts.append("ほぼ全体がサビです")
    elif whole >= 2:
        parts.append(f"サビを{whole}回まるごと含みます")
    elif whole == 1:
        parts.append("サビをまるごと含みます")
    elif inside >= 0.3:
        parts.append(f"約{round(inside * 100)}%がサビです")
    energy = facts["energy"]
    if start <= 0.25 and end >= duration - 0.25:
        pass  # the whole song: its average energy says nothing about the choice
    elif energy >= 0.7:
        parts.append("音量・エネルギーが高い区間です")
    elif energy < 0.4:
        parts.append("比較的落ち着いた区間です")
    if facts["on_downbeat"]:
        parts.append("小節の頭（ダウンビート）で開始します")
    return "。".join(parts) + "。"


# --- vocal activity -----------------------------------------------------------------------------


def _vocal_activity(f: _Features) -> np.ndarray:
    """Vocal-likeness per ``VOCAL_HOP`` block, 0..1 (see module docstring)."""
    n_out = max(1, math.ceil(f.duration / VOCAL_HOP - 1e-9))
    if f.silent:
        return np.zeros(n_out)
    band = (f.freqs >= VOCAL_BAND_HZ[0]) & (f.freqs <= VOCAL_BAND_HZ[1])
    harmonic, percussive = librosa.decompose.hpss(f.S[band], kernel_size=31)
    h_pow = np.sum(harmonic.astype(np.float64) ** 2, axis=0)
    p_pow = np.sum(percussive.astype(np.float64) ** 2, axis=0)
    h_db = 10.0 * np.log10(h_pow + 1e-10)
    active = f.active & (h_db > -100.0)
    ref = float(np.percentile(h_db[active], 95)) if np.any(active) else float(h_db.max())
    level = np.clip((h_db - (ref - ENERGY_RANGE_DB)) / ENERGY_RANGE_DB, 0.0, 1.0)
    harmonic_share = h_pow / (h_pow + p_pow + 1e-12)
    raw = level**1.5 * (0.7 + 0.3 * harmonic_share)
    raw[~f.active] = 0.0

    idx = np.minimum((f.frame_times / VOCAL_HOP).astype(np.int64), n_out - 1)
    sums = np.bincount(idx, weights=raw, minlength=n_out)
    counts = np.bincount(idx, minlength=n_out)
    values = np.divide(sums, counts, out=np.zeros(n_out), where=counts > 0)
    values = ndimage.median_filter(values, size=3, mode="nearest") if n_out >= 3 else values
    top = float(np.percentile(values, 98))
    if top <= 1e-6:
        return np.zeros(n_out)
    return np.clip(values / top, 0.0, 1.0)


# --- mood -----------------------------------------------------------------------------------------


def _mood(f: _Features, beats: _Beats, sections: list[dict[str, Any]]) -> dict[str, Any]:
    tempo_class = "slow" if beats.bpm < 90 else "fast" if beats.bpm >= 130 else "mid"
    if f.silent:
        return {"energy": 0.0, "tempo_class": tempo_class, "brightness": 0.0, "mode_guess": "major", "tags": ["silent"]}

    active = f.active
    level_db = float(10.0 * np.log10(np.mean(10.0 ** (f.frame_db[active] / 10.0))))
    loudness = float(np.clip((level_db + 30.0) / 20.0, 0.0, 1.0))  # -30 dBFS -> 0, -10 dBFS -> 1
    onsets = librosa.onset.onset_detect(onset_envelope=f.oenv, sr=SR, hop_length=HOP)
    span_sec = max((f.span.stop - f.span.start) * FRAME_SEC, 1.0)
    density = float(np.clip(len(onsets) / span_sec / 4.0, 0.0, 1.0))  # 4 onsets/s -> 1
    pace = float(np.clip((beats.bpm - 60.0) / 120.0, 0.0, 1.0)) if beats.confidence > 0 else 0.5
    energy = float(np.clip(0.45 * loudness + 0.35 * density + 0.2 * pace, 0.0, 1.0))

    centroid = librosa.feature.spectral_centroid(S=f.S, freq=f.freqs)[0]
    c = float(np.median(centroid[active]))
    brightness = float(np.clip(math.log(max(c, 1.0) / 600.0) / math.log(4000.0 / 600.0), 0.0, 1.0))

    weights = 10.0 ** (f.frame_db[active] / 20.0)
    profile = (f.chroma[:, active] * weights).sum(axis=1)
    mode = _mode_guess(profile)

    tags: list[str] = []
    if energy >= 0.65:
        tags.append("energetic")
    elif energy <= 0.35:
        tags.append("calm")
    if tempo_class == "fast":
        tags.append("upbeat")
    elif tempo_class == "slow":
        tags.append("slow")
    if brightness >= 0.6:
        tags.append("bright")
    elif brightness <= 0.3:
        tags.append("dark")
    tags.append("melancholic" if mode == "minor" else "uplifting")
    energies = [s["energy"] for s in sections]
    if len(energies) > 1 and max(energies) - min(energies) >= 0.35:
        tags.append("dynamic")
    if beats.confidence >= 0.5 and 95 <= beats.bpm <= 135 and energy >= 0.45:
        tags.append("danceable")
    return {
        "energy": _r(energy),
        "tempo_class": tempo_class,
        "brightness": _r(brightness),
        "mode_guess": mode,
        "tags": tags,
    }


def _mode_guess(chroma_profile: np.ndarray) -> str:
    """Major/minor by the best Krumhansl-Kessler key-profile correlation over the 12 keys."""
    if float(np.std(chroma_profile)) < 1e-9:
        return "major"
    best_major = max(np.corrcoef(np.roll(_MAJOR_PROFILE, k), chroma_profile)[0, 1] for k in range(12))
    best_minor = max(np.corrcoef(np.roll(_MINOR_PROFILE, k), chroma_profile)[0, 1] for k in range(12))
    return "minor" if best_minor > best_major else "major"


# --- helpers ---------------------------------------------------------------------------------------


def _r(value: float | np.floating[Any], ndigits: int = 3) -> float:
    v = float(value)
    return round(v, ndigits) if math.isfinite(v) else 0.0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _plain(obj: Any) -> Any:
    """Recursively convert numpy scalars/arrays to JSON-native types and drop ``_private`` keys."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple, np.ndarray)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return value if math.isfinite(value) else 0.0
    return obj
