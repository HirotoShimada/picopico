# AI Music Video Factory — Architecture & Implementation Contract

This document is the **single source of truth** for module boundaries, data
shapes and HTTP API. Every module MUST follow the names and shapes here so the
pieces fit together. If you must deviate, update this document in the same
change and say why.

## 1. Runtime & environment (verified 2026-10-11 in the dev container)

| Item | Version / note |
|---|---|
| Python | 3.13, venv at `backend/.venv` (managed with `uv`) — run with `backend/.venv/bin/python` |
| Key Python libs | fastapi 0.143, sqlalchemy 2.1, psycopg 3.3, celery 5.6, redis 8.1, **librosa 1.0.0** (new major: verify API, e.g. `beat_track` returns tempo as ndarray), numpy 2.5, soundfile, anthropic **1.13** (SDK 1.x), faster-whisper 1.2 (model download from huggingface is BLOCKED here), httpx 0.28, respx, moto[s3,server], pytest 9 |
| FFmpeg | 6.1.1 with libx264, libass, libfreetype, fontconfig, librubberband. `subtitles`/`ass` filter available |
| Fonts | `Noto Sans CJK JP` (Thin..Black), `Noto Serif CJK JP`, IPAGothic (fontconfig names) |
| Node | 22, Next.js **16.4** (App Router, React 19.3, Tailwind v4, Turbopack). Read `frontend/node_modules/next/dist/docs/` before using Next APIs — this version differs from training data |
| DB | PostgreSQL 16 on 127.0.0.1:5432, user/pass `mvf`/`mvf`, DBs `mvf` and `mvf_test` |
| Queue | Redis 127.0.0.1:6379 (Celery broker). `TASK_MODE=inline` runs tasks in-process threads (no Redis) |
| Browser | Chromium at `/opt/pw-browsers/chromium` (Playwright 1.64 installed in frontend devDeps; pass `executablePath`) |
| Network | huggingface.co, api.klingai.com, api.dev.runwayml.com are DENIED by the sandbox proxy; generativelanguage.googleapis.com reachable; npm/pypi reachable. Real video-generation calls can't be verified here → mock/HTTP-mocked tests only |

Start services if they are down:
```bash
pg_ctlcluster 16 main start
redis-server --daemonize yes --port 6379
```

Demo assets (synthesized, rights-free): `backend/.venv/bin/python backend/scripts/make_demo_assets.py`
→ `backend/var/demo/{demo_song.wav,demo_song.mp3,demo_lyrics_ja.txt,demo_lyrics_ja.lrc,clip_01..03.mp4}`.
The song is 60 s, ~120 BPM: intro 0–8 (quiet), verse 8–24, chorus 24–40 (loud), bridge 40–48 (quiet), chorus 48–60.
Lyric phrases start at 8,12,16,20,24,28,32,36,48,52 s (each ~3.6 s).

## 2. Repository layout

```
music-video-factory/
  docs/ARCHITECTURE.md            ← this file
  docs/VIDEO_PROVIDERS.md         ← research: official APIs, pricing, limits, commercial terms
  docs/RIGHTS_AND_SECURITY.md     ← rights checklist rationale, security model
  backend/
    requirements.txt / requirements.lock.txt
    .env.example
    app/
      main.py            FastAPI app factory `create_app()`, module-level `app`
      config.py          `Settings` (pydantic-settings), `get_settings()`
      db.py              engine, `SessionLocal`, `Base`, `get_db()`, `init_db()`
      models.py          SQLAlchemy ORM models (section 4)
      schemas.py         Pydantic API models (section 5)
      security.py        auth dependency `current_user`, media URL signing
      storage.py         `Storage` interface, `LocalStorage`, `S3Storage`, `get_storage()`
      routers/           one APIRouter per file, all mounted under `/api`
        system.py projects.py assets.py clips.py analysis.py lyrics.py
        generation.py videos.py dashboard.py media.py
      services/
        media.py            ffprobe helpers + upload validation
        audio_analysis.py   librosa analysis (section 6.1)
        lyrics_parse.py     TXT/LRC/SRT parsing + export (6.2)
        lyrics_timing.py    heuristic auto-timing (6.2)
        lyrics_align.py     optional Whisper alignment (6.2)
        presets.py          world presets, camera/scene vocab, color grades (6.3)
        prompting.py        prompt safety + JA→EN translation (6.3)
        storyboard.py       Claude-or-template storyboard (6.3)
        variation.py        batch planner: clip pool + per-video plans (6.4)
        cost.py             pricing table + estimates (6.5)
        subtitles.py        ASS generation for 5 lyric styles (6.6)
        render.py           FFmpeg compositor (6.6)
        quality.py          ffprobe-based output validation (6.6)
        providers/          video generation adapters (6.7)
          base.py mock.py library.py kling.py runway.py veo.py registry.py
      pipeline.py        orchestration over DB (section 7)
      worker.py          Celery app + task wrappers; inline executor
    scripts/make_demo_assets.py, scripts/e2e_demo.py
    tests/               pytest; `conftest.py` provides fixtures
  frontend/              Next.js app (section 8)
```

## 3. Configuration (`app/config.py`, env vars, prefix-free)

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://mvf:mvf@127.0.0.1:5432/mvf` | SQLAlchemy URL. `sqlite:///...` must also work (tests may use it) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery broker |
| `TASK_MODE` | `inline` | `inline` (thread pool in API process) or `celery` |
| `STORAGE_BACKEND` | `local` | `local` or `s3` |
| `STORAGE_DIR` | `backend/var/storage` | local storage root |
| `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | – | S3-compatible storage |
| `WORK_DIR` | `backend/var/work` | scratch dir for renders |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000` | used to build absolute signed media URLs |
| `CORS_ORIGINS` | `http://localhost:3000` | comma-separated |
| `AUTH_MODE` | `dev` | `dev` = single local user, no token; `token` = `Authorization: Bearer <token>` required |
| `API_TOKENS` | – | `token` mode: comma list `name:token` creating users at startup |
| `SECRET_KEY` | random per process if unset (warn) | HMAC key for signed media URLs |
| `MEDIA_URL_TTL` | `3600` | seconds |
| `MAX_AUDIO_MB` / `MAX_VIDEO_MB` | `100` / `500` | upload limits |
| `ANTHROPIC_API_KEY` | – | enables Claude storyboard/translation |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | |
| `KLING_ACCESS_KEY`, `KLING_SECRET_KEY`, `KLING_API_BASE` | – | Kling official API |
| `RUNWAY_API_KEY`, `RUNWAY_API_BASE` | – | Runway official API |
| `GOOGLE_API_KEY` (Gemini API) | – | Veo via Gemini API |
| `DEFAULT_BUDGET_USD` | `50` | per-batch default budget cap |
| `WHISPER_MODEL` | `small` | faster-whisper model size/path |
| `FONTS_DIR` | – | optional extra fonts dir for libass |

API keys are read server-side only and never returned by any endpoint (only booleans `configured`).

## 4. Data model (`app/models.py`, SQLAlchemy 2.0 typed ORM)

Use portable types only: `String`, `Text`, `Integer`, `Float`, `Boolean`, `DateTime(timezone=True)`, `JSON` (works on Postgres and SQLite). IDs are `String(32)` = `uuid4().hex`. All tables have `created_at`; mutable ones `updated_at`.

- **User**: `id`, `name`, `token_hash` (sha256 hex, nullable), `created_at`.
- **Project**: `id`, `owner_id`→User, `title` (楽曲名), `artist_name`, `mood` (雰囲気, free text), `purpose` (動画生成目的), `notes`,
  `audio_asset_id`→Asset (nullable), `analysis` JSON (AudioAnalysis §6.1, nullable), `analysis_status` (`none|queued|running|done|failed`), `analysis_error`,
  `lyrics_text` Text, `lyrics_format` (`txt|lrc|srt`), `lyric_lines` JSON (list of LyricLine §6.2, default `[]`),
  `segment_start` Float, `segment_end` Float (user-selected/confirmed section, nullable),
  `world_preset` (default `cinematic`), `custom_world` Text,
  `rights` JSON (dict key→bool, §9), `rights_confirmed_at`,
  `budget_limit_usd` Float nullable, `created_at`, `updated_at`.
- **Asset**: `id`, `project_id`, `owner_id`, `kind` (`audio|lyrics|material|clip|render|thumbnail|zip`), `filename` (original, sanitized), `storage_key`, `content_type`, `size_bytes`, `duration` Float, `width`, `height`, `meta` JSON, `created_at`.
- **Clip** (video material pool, uploaded or generated): `id`, `project_id`, `source` (`upload|mock|kling|runway|veo`), `provider`, `model`, `scene_id` (storyboard scene id, nullable), `prompt` Text, `negative_prompt` Text, `params` JSON, `cache_key` (String(64), indexed), `status` (`pending|generating|ready|failed|cancelled`), `asset_id`→Asset nullable, `thumbnail_asset_id`, `duration`, `width`, `height`, `cost_usd` Float default 0, `external_job_id`, `error`, `attempts` Int, `created_at`, `updated_at`.
- **Batch** (one generation run): `id`, `project_id`, `settings` JSON (GenerationSettings §5), `estimate` JSON (Estimate §6.5), `storyboard` JSON (§6.3), `plan` JSON (BatchPlan summary §6.4), `status` (`queued|planning|generating_clips|rendering|completed|partial|failed|cancelled|budget_exceeded`), `error`, `cost_usd` Float, `created_at`, `started_at`, `finished_at`.
- **Video** (one output MP4): `id`, `project_id`, `batch_id`, `index` Int, `title`, `status` (`queued|waiting_clips|rendering|done|failed|cancelled`), `progress` Float 0..1, `plan` JSON (VideoPlan §6.4), `asset_id`, `thumbnail_asset_id`, `quality` JSON (QualityReport §6.6), `favorite` Bool, `error`, `attempts`, `render_version` Int (increments on re-render), `created_at`, `updated_at`, `rendered_at`.
- **CostRecord**: `id`, `project_id`, `batch_id` nullable, `clip_id` nullable, `kind` (`video_generation|llm`), `provider`, `model`, `units` Float, `unit` (`second|token|clip`), `unit_price_usd`, `amount_usd`, `cached` Bool (true when a cache hit avoided spend; amount 0), `note`, `created_at`.

`init_db()` calls `Base.metadata.create_all()` (MVP; no Alembic yet).

## 5. HTTP API (all JSON under `/api`, except multipart uploads and media)

Errors: FastAPI default `{"detail": "..."}` with proper status codes (400 validation of files, 404 not found / not owned, 409 state conflict, 413 too large, 422 schema, 402-style budget refusal → use 422 with detail code). Every project-scoped route checks `project.owner_id == current_user.id` (404 otherwise).

Timestamps ISO-8601. Media URLs in responses are **signed absolute URLs** (`{PUBLIC_API_BASE_URL}/media/{key}?exp=..&sig=..`) — never raw storage keys.

### 5.1 System & dashboard
- `GET /api/health` → `{status:"ok", db:bool, redis:bool, ffmpeg:bool, task_mode, storage_backend}`
- `GET /api/system/capabilities` → `{providers:[ProviderInfo], claude_configured:bool, whisper:{available:bool, reason?:string}, presets:[WorldPresetInfo], lyric_styles:[{id,name_ja,description}], color_grades:[{id,name}], fonts:[{id,family,label}], durations:[15,30,60], counts:[1,3,5,10,30], limits:{max_audio_mb,max_video_mb,max_count:50,min_duration:5,max_duration:90}}`
  - `ProviderInfo = {id, name, configured:bool, available:bool, reason?:string, kind:"mock"|"library"|"api", commercial_use_note, models:[{id,name,clip_durations:[number], max_clip_duration, resolution, supports_vertical:bool, price_per_second_usd:number, price_note, verified:bool}]}`
- `GET /api/dashboard` → `{projects:int, videos_done:int, videos_total:int, active_jobs:[{batch_id, project_id, project_title, status, done, total, failed}], recent_videos:[VideoOut], usage:{total_usd, month_usd, by_provider:[{provider, amount_usd, clips}], clips_generated:int, clips_reused:int, saved_usd:number}, pending_estimate_usd:number}`

### 5.2 Projects
- `GET /api/projects` → `[ProjectSummary]` (`id,title,artist_name,created_at,updated_at,has_audio,analysis_status,lyric_line_count,video_count,cover_url?`)
- `POST /api/projects` `{title, artist_name, mood?, purpose?, notes?, world_preset?, custom_world?, budget_limit_usd?}` → `ProjectOut`
- `GET /api/projects/{id}` → `ProjectOut` = all Project fields (minus owner) + `audio: AssetOut|null` + `counts:{clips,videos,batches}`
- `PATCH /api/projects/{id}` partial update of editable fields → `ProjectOut`
- `DELETE /api/projects/{id}` → 204 (deletes stored files too)
- `PUT /api/projects/{id}/rights` `{items:{key:bool}}` → `ProjectOut` (sets `rights_confirmed_at` when all required items true)
- `AssetOut = {id, kind, filename, content_type, size_bytes, duration, width, height, url, created_at}`

### 5.3 Audio & analysis
- `POST /api/projects/{id}/audio` multipart `file` → validates (extension in mp3,wav,m4a,aac,flac,ogg; ffprobe must find an audio stream; size ≤ MAX_AUDIO_MB; duration 5 s–15 min) → stores Asset(kind=audio), sets `analysis_status=queued`, enqueues analysis → `ProjectOut` (201)
- `GET /api/projects/{id}/analysis` → `{status, error?, analysis: AudioAnalysis|null, segment:{start,end}|null}`
- `POST /api/projects/{id}/analysis/rerun` → 202 `{status}`
- `PUT /api/projects/{id}/segment` `{start, end}` (validated within duration, end-start ≥ 3) → `{segment}`

### 5.4 Lyrics
- `PUT /api/projects/{id}/lyrics` `{text, format?: "auto"|"txt"|"lrc"|"srt"}` → `{format, lines:[LyricLine], warnings:[string]}` (text stored verbatim; never rewritten)
- `POST /api/projects/{id}/lyrics/upload` multipart `file` (.txt/.lrc/.srt, ≤1 MB, UTF-8 or UTF-8-BOM or Shift_JIS) → same response
- `GET /api/projects/{id}/lyrics` → `{text, format, lines}`
- `PUT /api/projects/{id}/lyrics/lines` `{lines:[LyricLine]}` (manual timing edits; text of a line may be edited only by the user) → `{lines}`
- `POST /api/projects/{id}/lyrics/auto-time` `{method:"heuristic"|"whisper", start?, end?, overwrite_timed?:bool}` → `{lines, method_used, warnings}`; whisper unavailable → 503 `{detail}`
- `GET /api/projects/{id}/lyrics/export?format=lrc|srt` → text/plain attachment

### 5.5 Clips (video materials)
- `GET /api/projects/{id}/clips` → `[ClipOut]` (`id, source, provider, model, scene_id, prompt, status, duration, width, height, cost_usd, error, url?, thumbnail_url?, created_at, used_in_videos:int`)
- `POST /api/projects/{id}/clips/upload` multipart `files` (1..20; mp4,mov,webm,m4v; video stream required; ≤ MAX_VIDEO_MB; 1 s–10 min) → `[ClipOut]` (source=`upload`, status=`ready`, thumbnail generated)
- `DELETE /api/clips/{clip_id}` → 204 (409 if used by a video currently rendering)

### 5.6 Storyboard, estimate, batches
- `POST /api/projects/{id}/storyboard` `{preset, custom_world?, scenes?:int}` → `Storyboard` (§6.3) (does not persist; preview)
- `POST /api/projects/{id}/estimate` `GenerationSettings` → `Estimate` (§6.5)
- `POST /api/projects/{id}/batches` `GenerationSettings` → `BatchOut` (201). Rejects 422 when: no audio/analysis, rights not confirmed, provider not configured, estimate > budget, `library` provider with no ready uploaded clips.
- `GET /api/projects/{id}/batches` → `[BatchOut]`
- `GET /api/batches/{id}` → `BatchOut` with `videos:[VideoOut]`, `clips:[ClipOut]`
- `POST /api/batches/{id}/cancel` → `BatchOut`
- `GET /api/batches/{id}/zip` → `application/zip` stream of all `done` videos (also `POST /api/videos/zip {video_ids}` for a selection)

`GenerationSettings`:
```json
{
  "preset": "cinematic|emotional|urban_night|nostalgic|nature|dark|dreamy|documentary|custom",
  "custom_world": "string|null",
  "provider": "mock|library|kling|runway|veo",
  "model": "string|null",            // provider default when null
  "duration_sec": 15,                // 5..90
  "count": 3,                        // 1..50
  "lyric_style": "simple|cinematic|minimal|karaoke|fade",
  "text_style": TextStyle,           // optional overrides, see 6.6
  "color_grade": "auto|<grade id>",  // auto = varied per video from preset
  "start_strategy": "segment|chorus|varied|custom",
  "start_sec": null,                 // used when custom
  "reuse": "max_variety|balanced|economy",
  "max_new_clips": null,             // hard cap on newly generated clips
  "budget_limit_usd": null,          // overrides project/default budget
  "seed": null
}
```
`BatchOut = {id, project_id, status, settings, estimate, storyboard?, error, cost_usd, created_at, started_at, finished_at, counts:{total, done, rendering, queued, failed, cancelled}, clip_counts:{total, ready, generating, failed, reused}}`

### 5.7 Videos
- `GET /api/projects/{id}/videos?favorite=true|false&batch_id=` → `[VideoOut]`
- `GET /api/videos/{id}` → `VideoOut` (+ `lyric_lines` effective for this video, + `available_clips:[ClipOut]` for swapping)
- `PATCH /api/videos/{id}` `{favorite?, title?}` → `VideoOut`
- `POST /api/videos/{id}/retry` → re-render same plan (failed/cancelled) → `VideoOut`
- `POST /api/videos/{id}/regenerate` `{new_clips?:bool}` → new plan variant (new seed), re-render → `VideoOut`
- `PUT /api/videos/{id}/edit` `VideoEdit` → saves overrides, increments `render_version`, re-renders → `VideoOut`
  - `VideoEdit = {start?, lyric_style?, text_style?, color_grade?, lyric_offsets?:{line_id:{start,end}}, shots?:[{index, clip_id, src_in?}]}`
- `GET /api/videos/{id}/download` → 302 to signed URL with `download=1` (Content-Disposition attachment, filename `{artist}_{title}_{index:02d}.mp4` ASCII-safe + RFC5987 utf-8 name)
- `VideoOut = {id, project_id, batch_id, index, title, status, progress, error, favorite, url?, thumbnail_url?, duration?, quality?, plan, render_version, created_at, rendered_at}`

### 5.8 Media
- `GET /media/{key:path}?exp=&sig=[&download=1&filename=]` (NOT under /api; no auth header needed — the HMAC signature is the capability). Local storage only; supports HTTP Range (video seeking). S3 backend returns presigned URLs instead.

## 6. Service contracts (pure functions, no DB access)

All services raise typed exceptions from `app/services/errors.py`:
`ServiceError(Exception)` base; `MediaValidationError`, `AnalysisError`, `LyricsParseError`, `WhisperUnavailable`, `RenderError(stderr_tail)`, `ProviderError(retryable: bool)`, `ProviderNotConfigured`, `BudgetExceeded`.

### 6.1 `audio_analysis.analyze_audio(path: str) -> dict`
Decode with librosa (via soundfile/audioread; for formats librosa can't read, transcode to WAV with ffmpeg first). Analysis at sr=22050 mono is fine. Return **AudioAnalysis**:
```json
{
  "version": 1,
  "duration": 60.0, "sample_rate": 44100, "channels": 2, "codec": "pcm_s16le", "bit_rate": 1411200,
  "bpm": 120.0, "bpm_confidence": 0.0-1.0,
  "beats": [seconds...], "downbeats": [seconds...],
  "loudness": {"hop": 0.5, "rms_db": [..]},                  // one value per 0.5 s
  "waveform": {"points": 1200, "peaks": [0..1 ...]},         // fixed 1200 peak points over the whole track
  "sections": [{"start":0,"end":8,"label":"intro|verse|chorus|bridge|outro|break","energy":0..1,"is_chorus_candidate":false}],
  "chorus_candidates": [{"start":24,"end":40,"score":0..1}],
  "highlights": [{"start","end","energy"}],                  // loudest regions
  "quiet_parts": [{"start","end"}],
  "suggested_segments": [{"start","end","duration":15|30|60,"score":0..1,"reason":"string(ja)"}],  // best per duration that fits; start snapped to a downbeat
  "vocal_activity": {"hop": 0.1, "values": [0..1]},          // heuristic vocal-likeness for lyric timing
  "mood": {"energy":0..1, "tempo_class":"slow|mid|fast", "brightness":0..1, "mode_guess":"major|minor", "tags":["energetic",...]}
}
```
Sections come from novelty/self-similarity segmentation (e.g. `librosa.segment.agglomerative` on beat-synced chroma+MFCC) plus energy; chorus candidates = repeated + high-energy sections. Results are heuristics: the UI lets the user override the segment.
On the demo song, expected: bpm within 115–125; a chorus candidate overlapping 24–40 or 48–60; quiet parts include ~0–8 and ~40–48.

### 6.2 Lyrics
`LyricLine = {"id": str, "text": str, "start": float|null, "end": float|null, "words": [{"text","start","end"}]|null, "source": "manual|lrc|srt|txt|heuristic|whisper", "confidence": float|null}`
Times are **absolute seconds in the song**. Text is preserved exactly (strip only trailing newline/CR and leading/trailing whitespace; keep internal spaces); empty lines are dropped from `lines` but blank-line structure is kept in `lyrics_text`.
- `lyrics_parse.detect_format(text) -> "txt"|"lrc"|"srt"`
- `lyrics_parse.parse_lyrics(text, fmt="auto") -> tuple[list[LyricLine], list[str] warnings]` — LRC: `[mm:ss.xx]`, multiple tags per line, metadata tags ignored, `[offset:]` applied, enhanced `<mm:ss.xx>` word tags → words; end = next line start (last: start+4 s). SRT: index/timecode/text blocks, multi-line text joined with a space (warn). TXT: start/end null.
- `lyrics_parse.decode_bytes(data: bytes) -> str` (utf-8-sig, then cp932/shift_jis)
- `lyrics_parse.to_lrc(lines) -> str`, `to_srt(lines) -> str`
- `lyrics_timing.auto_time(lines, analysis, start=None, end=None, overwrite_timed=False) -> tuple[list[LyricLine], list[str]]` — heuristic: find vocal-active regions from `analysis.vocal_activity` (+ energy) within [start,end] (default whole song), distribute untimed lines proportionally to text length (characters, spaces ignored), snap line starts to the nearest beat (≤0.25 s), min line duration 1.0 s, no overlaps, `source="heuristic"`, `confidence≈0.3`. Also fill `words` with per-character (JA) / per-word (Latin) times by even split (karaoke style needs them).
- `lyrics_align.whisper_available() -> tuple[bool, str|None]`; `lyrics_align.align_lines(audio_path, lines, language="ja", model_size=settings.WHISPER_MODEL, start=None, end=None) -> tuple[list[LyricLine], list[str]]` — faster-whisper with word timestamps; align recognized chars to user lines with sequence matching on normalized text (NFKC, kana-folded, punctuation/space removed); **never replace user text**; lines without a confident match keep heuristic timing and get a warning. Raises `WhisperUnavailable` when the model can't be loaded.

### 6.3 Creative: presets, prompting, storyboard
- `presets.WORLD_PRESETS: dict[str, WorldPreset]` with ids `cinematic, emotional, urban_night, nostalgic, nature, dark, dreamy, documentary, custom`. `WorldPreset = {id, name_ja, name_en, description_ja, look_en (style/lighting/lens/film stock words), palette:[hex×3], scene_types:[ids], camera_moves:[ids], color_grades:[grade ids], negative_en}`.
- `presets.SCENE_TYPES`, `presets.CAMERA_ANGLES`, `presets.CAMERA_MOVES` — dict id → `{name_ja, prompt_en}`.
- `presets.COLOR_GRADES` — dict id → `{name, ffmpeg_filter}` (e.g. `teal_orange`, `warm_film`, `cool_night`, `bleach_bypass`, `faded_pastel`, `mono_noir`, `natural`, `vivid`). Filters must be valid FFmpeg 6.1 filter strings (`eq`, `colorbalance`, `curves`, `colortemperature`, `hue`, `vignette`, `noise`…), safe to append to a filtergraph.
- `presets.LYRIC_STYLES` — dict id → `{name_ja, description}` for `simple, cinematic, minimal, karaoke, fade`.
- `prompting.sanitize_prompt(text, *, forbidden_names: list[str]) -> tuple[str, list[str] warnings]` — removes/flags forbidden names (artist name, "celebrity", "lookalike", real-person phrasing), always appends the likeness-safety clause `"fictional characters only, no real or identifiable people, no celebrities"`; never adds text overlays (append `"no text, no captions, no watermark"`).
- `prompting.translate_to_english(text) -> tuple[str, str source]` — Claude if configured, else a built-in JA→EN keyword glossary (best-effort, `source="glossary"`), passthrough if already ASCII.
- `storyboard.generate_storyboard(*, title, artist_name, mood, purpose, lyrics: list[LyricLine], analysis: dict|None, preset_id, custom_world, n_scenes, seed=None, use_claude=True) -> dict` **Storyboard**:
```json
{
  "source": "claude|template", "model": "claude-opus-5-5|null", "usage": {"input_tokens":0,"output_tokens":0}|null, "cost_usd": 0.0,
  "world_bible": {"summary_ja": "...", "look_en": "...", "protagonist_en": "fictional ...", "location_en": "...", "palette": ["#.."], "consistency_tokens_en": "shared descriptors appended to every prompt"},
  "scenes": [{"id":"s01","section":"chorus","lyric_ref":"line id|null","scene_type":"...","camera_angle":"...","camera_move":"...","description_ja":"...","prompt_en":"full English prompt incl. world bible + 9:16 vertical + photoreal cinematic terms","negative_prompt_en":"...","duration_hint":5}],
  "warnings": []
}
```
  Claude call: Python SDK 1.x, `client.beta.messages.create(model=settings.ANTHROPIC_MODEL, max_tokens=16000, betas=["server-side-fallback-2026-07-01"], fallbacks="default", output_config={"format":{"type":"json_schema","schema":...}, "effort":"low"}, system=..., messages=[...])`; parse the first text block as JSON; handle `stop_reason == "refusal"` / API errors (typed exceptions: `anthropic.RateLimitError`, `APIStatusError`, `APIConnectionError`…) by falling back to the template generator with a warning. Template generator is deterministic for a given seed and produces varied scenes from preset vocab. Every `prompt_en` passes through `sanitize_prompt` with the artist name forbidden. Record LLM cost from `usage` (Opus 5.5: $4/MTok in, $20/MTok out).

### 6.4 `variation.plan_batch(...)`
```python
plan_batch(*, analysis: dict, lyric_lines: list[dict], settings: dict, storyboard: dict,
           segment: tuple[float,float]|None, provider_clip_duration: float,
           existing_clips: list[dict],   # ready clips: {id, scene_id, cache_key, duration, source}
           seed: int) -> dict  # BatchPlan
```
**BatchPlan**:
```json
{
  "clip_specs": [{"key":"c01","scene_id":"s01","prompt":"...","negative_prompt":"...","duration":5,"seed":123,"cache_key":"sha256","existing_clip_id":null|"id"}],
  "videos": [VideoPlan],
  "stats": {"videos":3,"shots_total":24,"unique_clips":8,"new_clips":8,"reused_clips":0,"variety_score":0..1,"variety_warning":"string(ja)|null"}
}
```
**VideoPlan** (also stored per Video):
```json
{
  "index": 0, "title": "Cinematic #1", "seed": 1, "start": 24.0, "duration": 15.0,
  "lyric_style": "simple", "text_style": {TextStyle}, "color_grade": "teal_orange",
  "shots": [{"clip_key":"c03","clip_id":null,"src_in":0.4,"duration":2.0,"hflip":false,"zoom_from":1.0,"zoom_to":1.08,"pan":"left|right|up|down|center","speed":1.0}],
  "transition": "cut|crossfade"
}
```
Rules: shot boundaries land on beats (shot length = 1 or 2 bars from analysis BPM, clamped 1.5–6 s; vary per video); shots sum exactly to `duration`; `src_in + duration*speed ≤ clip duration`; start time per `start_strategy` (`segment` = user segment start, `chorus` = best chorus candidate, `varied` = rotate among suggested segments/chorus/verse starts snapped to downbeats, `custom` = start_sec), never beyond song end − duration (if song shorter than duration → duration = song length, warn).
Pool size K by `reuse`: `max_variety` → one clip per shot (capped 60), `balanced` → ≈ shots_total/3, `economy` → ≈ shots_total/6, at least min(shots_per_video, #scenes), at most `max_new_clips` new ones. Reuse ready clips whose `cache_key` matches (and, for `library`, all uploaded clips are the pool). Videos differ by start, shot order, in-points, flips, zoom/pan, color grade and (if `auto`) lyric style variant. `variety_score` = unique (clip, src_in bucket, flip) combos / shots_total; warn in Japanese when < 0.35 (e.g. 「素材の再利用率が高いため、動画間の映像バリエーションが限定的です」).
`cache_key = sha256(provider|model|prompt|negative|duration|aspect|seed)`.

### 6.5 `cost.py`
`PRICING: dict[provider_id, dict[model_id, ModelPricing]]` with `price_per_second_usd`, `clip_durations`, `default_duration`, `source_url`, `as_of`, `verified` (filled from docs/VIDEO_PROVIDERS.md research; mock/library = 0).
`estimate(settings: dict, plan_stats: dict, provider: str, model: str, *, cached_clips: int, llm_estimate_usd: float, budget_limit_usd: float|None) -> dict` **Estimate**:
```json
{"provider":"mock","model":"mock-v1","clip_duration":5,"videos":3,"shots_total":24,"unique_clips":8,"new_clips":8,"cached_clips":0,
 "price_per_second_usd":0.0,"video_generation_usd":0.0,"llm_usd":0.01,"total_usd":0.01,
 "naive_total_usd":0.0,"savings_usd":0.0,              // naive = every shot generated fresh
 "budget_limit_usd":50,"within_budget":true,"variety_score":0.8,"variety_warning":null,
 "comparison":[{"provider","model","total_usd","configured"}],   // same plan priced on every provider/model
 "notes":["ja strings"]}
```

### 6.6 Rendering (`subtitles.py`, `render.py`, `quality.py`)
**TextStyle** (all optional overrides; defaults depend on lyric_style):
```json
{"font_family":"Noto Sans CJK JP","font_weight":"bold|regular|light","font_size":64,"primary_color":"#FFFFFF","highlight_color":"#4DA3FF",
 "outline_color":"#000000","outline_width":3,"shadow":1,"position":"bottom|center|top","bg_opacity":0.0,"letter_spacing":0,"max_chars_per_line":14}
```
- `subtitles.build_ass(lines, *, style_id, text_style, segment_start, duration, width=1080, height=1920, beats=None) -> str` — times relative to the segment; lines clipped to [0,duration]; horizontal text only (no rotation tags); Japanese-aware line wrapping (prefer breaking at spaces/punctuation, never inside a word for Latin, max 2 lines); **Instagram Stories safe area**: no text above y=270 px or below y=1920−380 px (top 14%, bottom ~20%); `position` maps to top/center/bottom inside that area.
  Styles: `simple` (white bold sans, thin outline, soft shadow), `cinematic` (serif `Noto Serif CJK JP`, smaller, semi-transparent black box via BorderStyle=3, low position), `minimal` (light weight, small, letter-spacing, no outline, subtle shadow), `karaoke` (`\kf` per word/char from `words`, else even split; highlight_color sweep), `fade` (`\fad(in,out)` with in/out aligned to nearby beats when given, ~250–400 ms).
- `render.render_video(spec: RenderSpec, progress_cb=None) -> RenderResult`
```python
@dataclass
class RenderShot: clip_path: str; src_in: float; duration: float; hflip: bool=False; zoom_from: float=1.0; zoom_to: float=1.06; pan: str="center"; speed: float=1.0
@dataclass
class RenderSpec:
    shots: list[RenderShot]; audio_path: str; audio_start: float; duration: float
    lyric_lines: list[dict]; lyric_style: str; text_style: dict; color_grade: str
    output_path: str; work_dir: str; beats: list[float] | None = None
    width: int = 1080; height: int = 1920; fps: int = 30
    crf: int = 18; preset: str = "medium"; audio_bitrate: str = "256k"
    audio_fade_in: float = 0.15; audio_fade_out: float = 0.8; transition: str = "cut"
@dataclass
class RenderResult: output_path: str; thumbnail_path: str; duration: float; size_bytes: int; log_tail: str
```
  Each shot: trim → speed → scale-to-cover → animated crop (Ken Burns zoom/pan, cheap `crop` expressions; avoid `zoompan`) → optional hflip → fps 30, setsar 1; concat (cut) or `xfade` (crossfade 0.3 s, durations compensated so total = duration); color grade; subtle film grain/vignette per grade; burn ASS with `subtitles=...:fontsdir=` (libass; escape paths). Audio: single encode from the **original file** (`-ss audio_start -t duration`), `afade` in/out, AAC-LC, keep source sample rate if 44.1/48 kHz else 48 kHz, stereo. Output: H.264 High, yuv420p, 1080×1920, 30 fps CFR, `-movflags +faststart`, GOP 60. Thumbnail: JPEG 540×960 from ~1 s. `progress_cb(fraction, stage)` best-effort (parse `-progress pipe:1`).
- `quality.validate_output(path, *, expected_duration, width=1080, height=1920, fps=30) -> dict` **QualityReport** `{"passed": bool, "checks":[{"name","passed","value","expected"}], "loudness_lufs": float|null, "black_ratio": float}` — checks: container mp4, video h264 yuv420p, size, fps 30, duration ±0.1 s, audio aac present, audio duration matches, faststart (moov before mdat), black frames ratio < 0.5 (`blackdetect`), integrated loudness (`ebur128`, informational only).

### 6.7 Video providers (`services/providers/`)
```python
@dataclass
class ProviderModel: id: str; name: str; clip_durations: list[float]; max_clip_duration: float; resolution: str; supports_vertical: bool; price_per_second_usd: float; price_note: str; verified: bool
@dataclass
class ClipRequest: prompt: str; negative_prompt: str; duration: float; model: str; aspect_ratio: str = "9:16"; seed: int | None = None; image_path: str | None = None; scene: dict | None = None  # preset/palette hints for mock
@dataclass
class ClipResult: path: str; duration: float; width: int; height: int; model: str; cost_usd: float; external_job_id: str | None; raw: dict
class VideoProvider(ABC):
    id: str; name: str; kind: str   # "mock" | "library" | "api"
    commercial_use_note: str
    def models(self) -> list[ProviderModel]: ...
    def default_model(self) -> str: ...
    def is_configured(self) -> tuple[bool, str | None]: ...
    def clip_duration_for(self, model: str, wanted: float) -> float: ...   # nearest supported duration ≥ wanted
    def generate(self, req: ClipRequest, out_path: str, *, timeout: float = 900, on_progress=None, cancel_check=None) -> ClipResult: ...
```
`registry.get_provider(id) -> VideoProvider`, `registry.list_providers() -> list[VideoProvider]`, `registry.provider_info() -> list[ProviderInfo dict]`.
- `mock`: synthesizes a 9:16 1080×1920 (or 720×1280 upscaled later) H.264 clip locally with FFmpeg lavfi in ~1–3 s: preset palette gradients + motion + grain + vignette, deterministic per (prompt, seed); small corner label `MOCK` so it is never mistaken for real output. Cost 0. Always configured.
- `library`: no generation; the planner maps scenes onto uploaded clips. `generate` raises `ProviderError` (not used). Always configured.
- `kling`, `runway`, `veo`: official REST APIs per docs/VIDEO_PROVIDERS.md (submit → poll with backoff → download). Configured only with keys. Retryable vs non-retryable errors distinguished (429/5xx/timeouts retryable). Unit-tested with `respx` mocks only (marked `verified: false` until a real key test passes).

## 7. Pipeline & jobs (`pipeline.py`, `worker.py`)

Task functions (idempotent, take IDs, open their own DB session):
- `analyze_project_audio(project_id)` → runs `analyze_audio`, stores analysis, sets default segment (best 15 s suggestion) if none, `analysis_status=done|failed`.
- `run_batch(batch_id)` → status `planning`: storyboard (Claude or template; record LLM CostRecord) → `plan_batch` → create Clip rows (reuse by cache_key: CostRecord `cached=true`, amount 0) and Video rows (status `waiting_clips`, plan with `clip_key`s) → status `generating_clips`: generate missing clips (bounded concurrency, budget check before each paid clip → `budget_exceeded`, cancel check, retries with backoff for retryable errors up to 3 attempts) → resolve `clip_id`s into video plans → status `rendering`: enqueue `render_video` per video → batch status recomputed when each video finishes (`completed` all done; `partial` some failed; `failed` none done).
- `render_video(video_id)` → download inputs via storage to work dir, `render_video` → `validate_output` → upload MP4 + thumbnail → `status=done` (quality stored even if a check fails; failed hard checks → `status=failed` with error).
- `TASK_MODE=inline`: `worker.enqueue(name, *args)` submits to a process-wide `ThreadPoolExecutor(max_workers=2)`; `celery`: `.delay()`. Same task bodies either way. Celery app name `mvf`, tasks `mvf.analyze`, `mvf.run_batch`, `mvf.render_video`. Run worker: `celery -A app.worker.celery_app worker -l info -c 2`.
- Cancellation: batch `cancelled` → pending/waiting videos → `cancelled`; running tasks check between steps.

## 8. Frontend (Next.js 16, `frontend/`)

- Talks to the backend directly from the browser: `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`). Optional `NEXT_PUBLIC_API_TOKEN` is NOT used; in `token` auth mode the user pastes a token on `/settings` (stored in localStorage) and the client sends `Authorization: Bearer`.
- Typed client in `src/lib/api.ts` mirroring section 5 exactly; types in `src/lib/types.ts`.
- UI kit: shadcn/ui-style components hand-written in `src/components/ui/` (shadcn registry is unreachable here) on `radix-ui`, `class-variance-authority`, `clsx`, `tailwind-merge`, `lucide-react`; data fetching with `swr` (polling while jobs run).
- Design: dark only. Background `#0a0a0a`, surfaces `#111113`/`#18181b`, borders `#27272a`, text `#e4e4e7`/muted `#a1a1aa`, **blue `#3b82f6` only for primary actions/progress**, red for errors, no decorative animation. Desktop-first; large 9:16 previews. Japanese UI copy.
- Routes: `/` dashboard · `/projects/new` · `/projects/[id]` (tabs: 楽曲解析 / 歌詞 / 素材 / 権利確認) · `/projects/[id]/generate` · `/batches/[id]` progress · `/projects/[id]/videos` gallery · `/videos/[id]/edit` editor · `/settings` providers & usage.

## 9. Rights & safety

Rights checklist keys (all required before batch creation):
`audio_rights` 音源の権利を保有、または利用許諾を得ている · `lyrics_rights` 歌詞の権利を保有、または利用許諾を得ている · `no_real_person_likeness` 実在の人物（アーティスト本人を含む）の肖像を生成しない · `model_terms_checked` 使用する動画生成モデルの利用規約・商用利用条件を確認した · `platform_terms` 投稿先（Instagram 等）の規約を確認した.
Uploaded media is private (signed URLs only), file types are validated by content (ffprobe) not only by extension, filenames are sanitized, paths never come from user input.
