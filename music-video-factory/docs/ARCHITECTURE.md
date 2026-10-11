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
| `KLING_API_KEY`, `KLING_ACCESS_KEY`, `KLING_SECRET_KEY`, `KLING_API_BASE` | –, –, –, `https://api-singapore.klingai.com` | Kling official API. `KLING_API_KEY` (Bearer) is the current auth; the AK/SK pair is legacy-API only (VIDEO_PROVIDERS.md §2.4) |
| `RUNWAY_API_KEY`, `RUNWAY_API_BASE` | –, `https://api.dev.runwayml.com` | Runway official API |
| `GOOGLE_API_KEY` (Gemini API) | – | Veo via Gemini API |
| `DEFAULT_BUDGET_USD` | `50` | per-batch default budget cap |
| `WHISPER_MODEL` | `small` | faster-whisper model size/path |
| `FONTS_DIR` | – | optional extra fonts dir for libass |

API keys are read server-side only and never returned by any endpoint (only booleans `configured`).

## 4. Data model (`app/models.py`, SQLAlchemy 2.0 typed ORM)

Use portable types only: `String`, `Text`, `Integer`, `Float`, `Boolean`, `DateTime(timezone=True)`, `JSON` (works on Postgres and SQLite). IDs are `String(32)` = `uuid4().hex`. All tables have `created_at`; mutable ones `updated_at`.

- **User**: `id`, `name`, `token_hash` (sha256 hex, nullable), `created_at`.
- **Project**: `id`, `owner_id`→User, `title` (楽曲名), `artist_name`, `artist_aliases` JSON list (other spellings, e.g. romanised; nullable, added later), `mood` (雰囲気, free text), `purpose` (動画生成目的), `notes`,
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

`init_db()` calls `Base.metadata.create_all()` (MVP; no Alembic yet) and then `add_missing_columns()`: nullable model columns that an existing table lacks are added with `ALTER TABLE … ADD COLUMN` (e.g. `projects.artist_aliases` on a database created before it existed); other missing columns are only logged.

## 5. HTTP API (all JSON under `/api`, except multipart uploads and media)

Errors: FastAPI default `{"detail": "..."}` with proper status codes (400 validation of files, 404 not found / not owned, 409 state conflict, 413 too large, 422 schema, 402-style budget refusal → use 422 with detail code). Every project-scoped route checks `project.owner_id == current_user.id` (404 otherwise).

Timestamps ISO-8601. Media URLs in responses are **signed absolute URLs** (`{PUBLIC_API_BASE_URL}/media/{key}?exp=..&sig=..`) — never raw storage keys.

### 5.1 System & dashboard
- `GET /api/health` → `{status:"ok"|"degraded", db:bool, redis:bool, ffmpeg:bool, task_mode, storage_backend}` (no auth; never raises; `degraded` when db or ffmpeg — or redis in `celery` mode — is down)
- `GET /api/system/capabilities` → `{providers:[ProviderInfo], claude_configured:bool, whisper:{available:bool, reason?:string}, presets:[WorldPresetInfo], lyric_styles:[{id,name_ja,description}], color_grades:[{id,name}], fonts:[{id,family,label}], durations:[15,30,60], counts:[1,3,5,10,30], limits:{max_audio_mb,max_video_mb,max_count:50,min_duration:5,max_duration:90}}`
  - `ProviderInfo = {id, name, configured:bool, available:bool, reason?:string, kind:"mock"|"library"|"api", commercial_use_note, models:[{id,name,clip_durations:[number], max_clip_duration, resolution, supports_vertical:bool, price_per_second_usd:number, price_note, verified:bool}]}`
- `GET /api/dashboard` → `{projects:int, videos_done:int, videos_total:int, active_jobs:[{batch_id, project_id, project_title, status, done, total, failed}], recent_videos:[VideoOut], usage:{total_usd, month_usd, by_provider:[{provider, amount_usd, clips}], clips_generated:int, clips_reused:int, saved_usd:number}, pending_estimate_usd:number}`

### 5.2 Projects
- `GET /api/projects` → `[ProjectSummary]` (`id,title,artist_name,created_at,updated_at,has_audio,analysis_status,lyric_line_count,video_count,cover_url?`)
- `POST /api/projects` `{title, artist_name, artist_aliases?: [string ≤100] (≤10, cleaned + de-duplicated), mood?, purpose?, notes?, world_preset?, custom_world?, budget_limit_usd?}` → `ProjectOut` (includes `artist_aliases`, `[]` by default; `PATCH` rejects `null`)
- `GET /api/projects/{id}` → `ProjectOut` = all Project fields (minus owner) + `audio: AssetOut|null` + `counts:{clips,videos,batches}`
- `PATCH /api/projects/{id}` partial update of editable fields → `ProjectOut`
- `DELETE /api/projects/{id}` → 204 (deletes stored files too)
- `PUT /api/projects/{id}/rights` `{items:{key:bool}}` → `ProjectOut` (sets `rights_confirmed_at` when all required items true)
- `AssetOut = {id, kind, filename, content_type, size_bytes, duration, width, height, url, created_at}`

### 5.3 Audio & analysis
- `POST /api/projects/{id}/audio` multipart `file` → validates (extension in mp3,wav,m4a,aac,flac,ogg; ffprobe must find an audio stream in an allowed container — Matroska/WebM are not accepted for songs; size ≤ MAX_AUDIO_MB; duration 5 s–15 min measured on the **decoded** audio (`media.decoded_audio_duration`, 8 kHz mono, stops past the limit), so a header that under-reports the length cannot pass) → stores Asset(kind=audio), sets `analysis_status=queued`, enqueues analysis → `ProjectOut` (201)
- `GET /api/projects/{id}/analysis` → `{status, error?, analysis: AudioAnalysis|null, segment:{start,end}|null}`
- `POST /api/projects/{id}/analysis/rerun` → 202 `{status}`
- `PUT /api/projects/{id}/segment` `{start, end}` (validated within duration, end-start ≥ 3) → `{segment}`
- `GET /api/projects/{id}/world-estimate` (extension, 感情・世界観推定) → `{emotions:[id], themes:[id], mood_en, recommended_presets:[preset id], sources:{audio,lyrics,user:[id]}, rationale_ja, analysis_used:bool}` = `storyboard.estimate_world(analysis if done else None, lyric_lines, mood, purpose)`; deterministic, no API call. Emotion ids `melancholic nostalgic hopeful energetic romantic calm dark dreamy`, themes `night nature`.

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
  "lyric_style": "auto|simple|cinematic|minimal|karaoke|fade",   // auto = the preset's styles rotated per video
  "text_style": TextStyle,           // optional overrides, see 6.6
  "color_grade": "auto|<grade id>",  // auto = varied per video from preset
  "start_strategy": "segment|chorus|varied|custom",
  "start_sec": null,                 // used when custom
  "reuse": "max_variety|balanced|economy",
  "max_new_clips": null,             // hard cap on newly generated clips
  "budget_limit_usd": null,          // overrides project/default budget
  "seed": null,
  "new_storyboard": false            // extension: ask Claude for a new storyboard instead of reusing the stored one
}
```
`BatchOut = {id, project_id, status, settings, estimate, storyboard?, error, cost_usd, created_at, started_at, finished_at, counts:{total, done, rendering, queued, failed, cancelled}, clip_counts:{total, ready, generating, failed, reused}, reopened_from?, previous_error?}` — `reopened_from` / `previous_error` (extension): while a finished batch is re-opened for a re-render, the status and terminal reason it had before. `cost_usd` includes the reservations of clip attempts in flight (§7).
- `PATCH /api/batches/{id}` `{budget_limit_usd ≥ 0}` (extension) → `BatchOut` with children: sets `estimate.budget_limit_usd` (and `within_budget`), which every later clip reservation and retry admission uses — e.g. to resume a `budget_exceeded` batch by retrying its stopped videos.

### 5.7 Videos
- `GET /api/projects/{id}/videos?favorite=true|false&batch_id=` → `[VideoOut]`
- `GET /api/videos/{id}` → `VideoOut` (+ `lyric_lines` effective for this video, + `available_clips:[ClipOut]` for swapping)
- `PATCH /api/videos/{id}` `{favorite?, title?}` → `VideoOut`
- `POST /api/videos/{id}/retry` → re-render same plan (failed/cancelled) → `VideoOut`
- `POST /api/videos/{id}/regenerate` `{new_clips?:bool}` → new plan variant (new seed), re-render → `VideoOut`
- `PUT /api/videos/{id}/edit` `VideoEdit` → saves overrides, increments `render_version`, re-renders → `VideoOut`
  - `VideoEdit = {start?, lyric_style?, text_style?, color_grade?, lyric_offsets?:{line_id:{start,end}}, shots?:[{index, clip_id, src_in?}]}`
  - Field semantics (the `/videos/[id]/edit` page relies on them; absent/`null` = keep the current value):
    - `start`: absolute song seconds of the video start (same as `VideoPlan.start`); the duration is unchanged.
    - `text_style`: replaces the video's text overrides (`VideoPlan.text_style`); `{}` clears them back to the lyric-style defaults.
    - `lyric_offsets`: per-line timing for this video only, in **absolute song seconds** (same unit as `LyricLine.start/end`, hence `start ≥ 0`, `end > start`). Merged into the video's saved overrides by line id; the editor sends only the lines it changed. `GET /api/videos/{id}` returns `lyric_lines` with the overrides applied.
    - `shots`: replaces `clip_id` of the listed shot indexes (merged; other shots keep theirs). Without `src_in` the server keeps the shot's in-point when the new clip is long enough, else clamps it.
    - The response is the updated `VideoOut` (status `queued`/`rendering`, incremented `render_version`, `plan` already reflecting the edit).
- `GET /api/videos/{id}/download` → 302 to signed URL with `download=1` (Content-Disposition attachment, filename `{artist}_{title}_{index:02d}.mp4` ASCII-safe + RFC5987 utf-8 name)
- `VideoOut = {id, project_id, batch_id, index, title, status, progress, error, favorite, url?, thumbnail_url?, duration?, quality?, plan, render_version, created_at, rendered_at}`

### 5.9 As implemented (integration step)

All coded errors are `{"detail": <ja>, "code": <code>}`.

- **System**: `GET /api/system/capabilities` requires the normal auth (`CurrentUser`; only `/api/health` is public). `providers` = `registry.provider_info()` (adds `default_model`), `presets/lyric_styles/color_grades` from `presets.*_infos()` (`WorldPresetInfo` also declares `color_grades` and `lyric_styles` — the ids `auto` rotates through), `fonts` = the two Noto CJK JP families, filtered by `fc-list` when available. `whisper` = `lyrics_align.whisper_available()`.
- **Analysis (§5.3)**: rerun → 409 `audio_missing` without a song; returns the current status unchanged while `running`. `PUT /segment` → 409 `audio_missing`, 422 `segment_out_of_range` when `end` > song length (+0.05 s tolerance; a hair over is clamped). The analysis task stores the result only if `project.audio_asset_id` is still the analysed asset, and sets the default segment to the `suggested_segments` entry closest to 15 s (else `0..min(15, duration)`).
- **Lyrics (§5.4)**: upload accepts `.txt/.lrc/.srt` (`.txt` → auto-detect), ≤ 1 MB (413), decoded with `decode_bytes_with_info` (its warnings are returned). `PUT /lyrics/lines`: word timings follow an edited line (rescaled from the words' span) and are dropped when the line text no longer matches them. `POST /auto-time`: 422 `lyrics_missing` without lines; `heuristic` uses the analysis only when `analysis_status == done`; `whisper` → 503 `whisper_unavailable` (detail = the availability reason), otherwise `auto_time` first, then `align_lines` (sync route → FastAPI threadpool). Export file name `{artist}_{title}.{lrc|srt}` (`Content-Disposition` attachment, UTF-8 `filename*`).
- **Generation (§5.6)**:
  - `GenerationSettings.lyric_style: "auto"` (extension, offered in the UI as 自動（動画ごとに変える）): the planner rotates the preset's recommended styles (`WorldPresetInfo.lyric_styles`), so the videos of one batch get different styles; each video's `plan.lyric_style` is a concrete id. The generate form keeps `auto` from the previous batch.
  - `color_grade` must be `auto` or a `presets.COLOR_GRADES` id (422 `color_grade_unknown`, same as the editor) — for estimate, batch creation and regenerate.
  - When the request omits `preset` / `custom_world`, the project's `world_preset` / `custom_world` are used.
  - `POST /estimate` plans with the storyboard the batch would use: the project's stored Claude storyboard for the same world (`stored_storyboard`, see §7) when one exists, else the deterministic template (no paid Claude call) plus the expected Claude cost when a key is set (6k input tokens + 1.5k + 700 per scene output; `new_storyboard: true` always prices a new call). It uses the request's `seed` (else 0) — the generate page sends one random seed for both the estimate and the batch, so the confirmed figure describes the run. Clips other batches of the project are still generating count as cached. The plan warnings are prepended to `notes`.
  - The budget is resolved as settings → project → `DEFAULT_BUDGET_USD`.
  - `POST /batches` guard codes (all 422): `audio_missing`, `analysis_missing`, `rights_unconfirmed`, `provider_not_configured` (from `check_model`), `library_empty`, `budget_exceeded`, plus `provider_unknown_model`, `color_grade_unknown`.
  - Batch creation fixes a random `seed` when none is given (stored in `settings`, together with the resolved `model`), so the stored estimate describes exactly the plan the batch runs.
  - If the queue is down: 503 `queue_unavailable` and the batch is stored as `failed`.
  - `POST /batches/{id}/cancel`: idempotent on a cancelled batch; 409 `batch_finished` on other terminal states. Semantics in §7 (rendering videos are cancelled too; cancelling the re-render of a re-opened finished batch restores its earlier status and the previous renders).
  - `GET /batches/{id}/zip` and `POST /videos/zip`: streamed, uncompressed (`ZIP_STORED`, data descriptors, UTF-8 names = download names, de-duplicated with `_2`…). 409 `no_videos` when nothing is done.
  - `POST /storyboard` uses Claude when configured and books an LLM `CostRecord` (batch_id null) when it costs anything. It does not check the rights checklist or the budget (preview only); with a key it sends the same data as a batch storyboard (RIGHTS_AND_SECURITY §5).
- **Videos (§5.7)**:
  - `GET /videos/{id}`: `lyric_lines` = all project lines with this video's overrides applied. `available_clips` = the project's ready clips.
  - List order: newest batch first, then `index`.
  - `retry`: only `failed`/`cancelled` (409 `video_not_retryable`). It keeps the plan and `render_version`. Clips of the plan that this batch failed or cancelled (provider outage, budget stop, cancel) are reset to `pending` and generated again by the render task, after the batch budget admission check (422 `budget_exceeded`; raise the budget with `PATCH /api/batches/{id}` first).
  - `edit` / `regenerate` / `retry` require the rights checklist (422 `rights_unconfirmed`); the tasks re-check it, so withdrawing consent also stops queued work.
  - `edit` / `regenerate`: 409 `video_busy` while queued/waiting/rendering. Both increment `render_version`. Re-rendering a video re-opens a finished batch (`rendering`, with `plan.reopened_from` / `plan.previous_error`); its status is then recomputed. The finished render stays the fallback (`plan.previous = {plan, render_version, quality}`): when the re-render is cancelled or fails, the video returns to `done` with the previous plan and MP4, and `error` says why (…前回の動画を表示しています。).
  - `VideoEdit` semantics:
    - `lyric_offsets` = absolute song seconds per line id, merged into `plan.lyric_offsets` (422 `lyric_line_unknown`).
    - `text_style` replaces the plan's overrides (`{}` clears them).
    - `color_grade` must be a `presets.COLOR_GRADES` id (422 `color_grade_unknown`).
    - `start` must lie within `0..song − duration` (422 `start_out_of_range`).
    - `shots[]` takes a ready clip of the same project (422 `shot_unknown`/`clip_unknown`/`clip_not_ready`). `src_in` is clamped into the clip, and `clip_key` becomes null.
  - `regenerate` keeps the video's own start (`start_strategy=custom`), lyric style, per-line lyric timing (`lyric_offsets`) and — once edited — text overrides and colour grade (also a fixed batch grade); only shots, in-points and the seed change. `{new_clips:false}` plans with `max_new_clips=0`, so it uses cached/ready footage only and spends nothing. `{new_clips:true}` sets `fresh_clips`, creates `pending` Clip rows (generated by the render task) and is refused with 422 `budget_exceeded` when spend + **every pending clip of the batch** + the new clips would exceed the batch budget.
  - `download` → 302 to the signed URL with `download=1&filename={artist}_{title}_{NN}.mp4`. **NN = `index + 1`** (1-based, 2 digits; deviation from the literal `{index:02d}`, so the first video is `_01`). 409 `video_not_ready`.
  - Plan keys added by the pipeline: `lyric_offsets`, `edited`, `regenerated`, `substitutions` (count), shot `substituted_from`, `previous` (fallback render while re-rendering); BatchPlan `seed`, `reopened_from`, `previous_error`. The UI shows substitutions in the batch table, gallery, preview and editor.

### 5.8 Media
- `GET /media/{key:path}?exp=&sig=[&download=1&filename=]` (NOT under /api; no auth header needed — the HMAC signature is the capability). Local storage only; supports HTTP Range (video seeking). S3 backend returns presigned URLs instead.
- `exp` is rounded up to a window of `ttl/10` (≤ 5 min), so repeated responses carry identical URLs and the browser cache works while the UI polls (a URL stays valid for `ttl` … `ttl + window` seconds).

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
As implemented: decoding stops at `MAX_DECODE_SEC = media.MAX_AUDIO_SEC + 1` (librosa `duration=`, FFmpeg `-t`), so even a file that bypassed upload validation cannot decode for hours. The `mood` tags feed `storyboard.estimate_world` (§6.3). Known bias: beats come out ≈ 30 ms late on the demo song (tracker latency, not compensated).
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
- As implemented (compatible additions; ids are `l001`, `l002`, … in time order for LRC/SRT, text order for TXT; parsed lines have `confidence: null`):
  - `decode_bytes` order: UTF-8-BOM → UTF-16-BOM → UTF-8 → CP932 (⊇ Shift_JIS) or EUC-JP, whichever reads as more plausible Japanese → UTF-8 with U+FFFD; data containing NUL raises `LyricsParseError`. `decode_bytes_with_info(data) -> DecodedText(text, encoding, warnings)` returns the warning for the replacement fallback (use it in the upload route).
  - LRC: lines whose starts are within `SAME_TIME_SEC = 0.05` s (bilingual / duet layouts) form a group: they get the warning 「同じ時刻の行が N 組あります（対訳・デュエットなど）。動画では上下に並べて表示します。」 and share their end (the next distinct start) instead of ending each other. `[mm:ss]`, `[mm:ss.x|xx|xxx]`, `[mm:ss:xx]`; only known metadata keys (`ti ar al au by length offset re ve la …`) are dropped, so TXT headings like `[Chorus: …]` are not; a positive `[offset:+ms]` makes lyrics earlier; empty timed lines end the previous line; the last line ends at start+4 s (or its last word end, capped by `[length:]`); lines without a time tag are kept untimed (warning); a warning lists lines held > 15 s. SRT: `<i>/<b>/<font>`/`{\…}` formatting tags are removed (warning); text after a blank line inside a cue stays with that cue (warning); cues are sorted by time; optional hours/millis and `WEBVTT` headers accepted. TXT: bracket-only heading lines (`[サビ]`, `【…】`) are kept but warned about.
  - `to_lrc(lines, *, include_words=False, metadata=None)`: writes an empty `[mm:ss.xx]` end tag when a line ends before the next one starts (and after the last line) so ends survive a round trip; untimed lines are written without a tag; `include_words` writes enhanced `<mm:ss.xx>` word tags. `to_srt` skips untimed lines (a missing end becomes the next start, else start+4 s).
  - `auto_time`: vocal activity (fallback: `loudness.rms_db`) is smoothed and hysteresis-thresholded relative to the whole song; runs are cut at pauses ≥ 0.2 s into phrase units (gaps < 0.6 s count as breaths inside a region); a small DP assigns each run of untimed lines (between kept lines, inside `[start, end]`) to units — one line may span several units, several lines may share one (split at the weakest activity), spurious units may be skipped — comparing voiced duration with the expected duration from text length (spaces/punctuation ignored, kanji ≈ 1.5 morae, Latin words by syllables). Gaps < 0.5 s between consecutive lines are closed. Lines spread evenly for lack of vocal activity get `confidence 0.15` and a warning. With a window, `overwrite_timed` re-times only lines whose start lies inside it. Helpers: `text_units(text)`, `line_weight(text)`, `fill_words(text, start, end)` (word texts are exact substrings of the line, in order). Demo song: mean start error 0.036 s.
  - `align_lines(..., transcriber=None)`: `transcriber(audio_path, *, language, start, end, initial_prompt) -> [(word, start, end)]` in song seconds replaces faster-whisper (tests/other engines). Matching is a semi-global Needleman–Wunsch alignment (not greedy `difflib`, which mis-maps repeated choruses); substituted characters take the time of the recognized character they align with; missing ones are inter/extrapolated at the line's pace; a line needs ≥ 40 % exactly matched characters (`confidence` = that ratio). Unmatched lines keep their timing unless it collides with aligned neighbours or is missing, in which case they are re-spread in the gap (`source="heuristic"`, `confidence 0.1`). With a window only untimed lines and lines starting inside it are aligned. Call `auto_time` first so unmatched lines have heuristic timing. `whisper_available(model_size=None)` uses `local_files_only` (≈0.4 s, no network; failures cached 5 min; `reset_whisper_cache()`); `WHISPER_MODEL` may be a size/repo id in the local HF cache or a CTranslate2 model directory. Audio is decoded with FFmpeg (16 kHz mono) because `faster_whisper.decode_audio` is incompatible with the installed PyAV 19.

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

  As implemented (additions; the shapes above are unchanged):
  - **presets**: `WorldPreset` also has `lighting_en, lens_en, film_en, atmosphere_en` (`look_en` = their join), `palette_en, palette_ja, camera_angles, lyric_styles` (recommended, best first), `protagonists` / `locations` (`[{en, ja}]`, fictional adults only). `SCENE_TYPES[id]` adds `desc_ja`, `has_subject`, `energy` (`low|mid|high`); `prompt_en`/`desc_ja` are templates with `{subject}`/`{place}`. `CAMERA_MOVES[id].energy`. `COLOR_GRADES[id]` adds `name_en`, `description_ja`; grades are **colour-only point filters** (`eq`, `colorbalance`, `colortemperature`, `curves`, `hue`, `vibrance`) because `render.py` adds vignette/grain per grade and bakes point-filter runs into a LUT; 12 ids = the 8 above + `golden_hour, neon_night, forest_moody, lavender_haze`. Note: FFmpeg 6.1 `colorbalance=…:pl=1` collapses bright saturated colours to grey — not used (regression-tested). `LYRIC_STYLES[id].default_position`. Helpers: `get_preset(id)` (unknown → cinematic), `preset_infos()`, `color_grade_infos()`, `lyric_style_infos()` (ready-made lists for `GET /api/system/capabilities`), `SECTION_LABELS_JA`.
  - **prompting**: `sanitize_prompt(text, *, forbidden_names, max_chars=1000)` — result ends with `"<body>. fictional characters only, no real or identifiable people, no celebrities, no text, no captions, no watermark."`, is idempotent and never longer than `max_chars` (Runway's `promptText` limit is 1000; the body is shortened at a sentence/clause boundary with a warning). Names match case/width/spacing-insensitively (`米津 玄師` = `米津玄師`, `Taylor-Swift`), Latin names on word boundaries; JA terms include 本人 (not 日本人), 芸能人, 有名人, そっくり, 激似, 実在の人物, モノマネ…. `translate_to_english(text, *, use_claude=True, client=None)`; sources `passthrough|claude|glossary`. Helpers: `strip_forbidden_names`, `glossary_translate`, `glossary_keywords`, `make_claude_client()`, `first_text_block(response)`, `prompt_body_budget(max_chars)`.
  - **likeness guard** (prompting, review fix): `strip_person_references(text) -> PersonReferences` removes Japanese resemblance markers with the name before them (`似の`, `に似た`, `そっくり`, `風の顔`, `みたいな顔/人`…), `<Name> lookalike/vibes/-esque/style`, `looks (exactly) like / resembling / resemblance to / likeness of / spitting image of <person>` (only when the object is not known vocabulary, so "looks like a dream" stays), `in the style of / inspired by / vibes of / like <Capitalised Name>`, and remaining probable names; `detect_person_names(text)` = two or more adjacent Capitalised words with an unknown word (vocabulary: glossary, preset text, common scene words, place names; Title Case text needs two unknown words); `screen_free_text`, `person_reference_warnings`, `mood_keywords` (glossary words + an English mood-adjective allowlist — raw ASCII mood text is never copied), `artist_name_variants` (kana artist names are also forbidden in romaji: あいみょん → aimyon). `sanitize_prompt` applies all of this to every prompt. Limits: a single-word name outside a look-alike phrase and kanji artist names in romaji need `artist_aliases` (or Claude); an unlisted two-word place name may be removed (with a warning).
  - **storyboard**: extra keywords `client=None` (inject an Anthropic client; tests use the real SDK over `httpx2.MockTransport`) and `artist_aliases=None` (the project's `artist_aliases`; the pipeline and the preview route pass them). `forbidden_likeness.names` = artist name + aliases + romaji variants + people named in custom world / mood / purpose; on the template path custom world and mood are screened (Japanese warnings) before the glossary translation and `mood_en` uses only `mood_keywords`, else the world estimate. Claude must return `detected_real_people` (required in `STORYBOARD_SCHEMA`); those names are added to the forbidden list and removed from its output (consistency tokens stay verbatim). `estimate_world(analysis, lyrics, mood, purpose)` (感情・世界観推定: audio mood tags/energy/mode + a JA/EN emotion lexicon over the lyrics + the user's mood/purpose, which weighs most; rule table emotion → presets; live/tour wording boosts documentary) → `{emotions, themes, mood_en, recommended_presets, sources, rationale_ja}`; every storyboard carries it as `world_estimate`, the Claude payload adds `song.audio_mood` and `song.world_estimate`, and the template path uses the estimated emotions as mood when the user gave none (`summary_ja` 「ムードは推定で…」). The pipeline adds `world_key` (§7). `n_scenes=None|0` → 8, clamped to 1..30. Extra keys: storyboard `preset`, `seed`; scenes `start`/`end` (song seconds of the slot, null when unknown); template `world_bible.protagonist_ja/location_ja`. Prompts are ≤ 950 chars (room for the planner's "Alternate take …" suffix) and always contain `consistency_tokens_en` verbatim. When a Claude call was made but failed (refusal / unparsable JSON), the result is `source: "template"` **with** that call's `model`, `usage` and `cost_usd` (tokens are billed) — the pipeline should book an LLM CostRecord whenever `cost_usd > 0`. The artist name reaches Claude only in `forbidden_likeness.names` (stripped from title/mood/purpose/custom world/lyrics). anthropic 1.13 accepts `betas`, `fallbacks` and `output_config` as native kwargs of `client.beta.messages.create` (no `extra_body`). Cost: `claude_cost_usd(model, usage)` (cache writes 1.25× input, cache reads $0.20/MTok on Opus 5.5).

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

As implemented (additions):
- `make_cache_key(provider, model, prompt, negative, duration, aspect, seed)` formats `duration` as `%.3f` and `seed=None` as an empty string; `aspect` is `"9:16"`. The **model must be the resolved model id**: pass `settings["model"]` filled in (the planner otherwise asks `providers.registry` for the default, falling back to `"default"`).
- Clip seeds derive from (prompt, negative, take) — not from the batch seed — so a later batch with the same storyboard hits the cache. `settings["fresh_clips"]: true` (optional extension) mixes the batch seed in to force new footage (e.g. `regenerate {new_clips:true}`).
- Pool: K = `pool_target_size(reuse, shots_total, shots_per_video, n_scenes)` (all policies capped at 60). Takes are allocated to scenes in proportion to how much of the planned videos falls into the scene's section (plus its lyric line being on screen), with a floor so other scenes still appear; take ≥ 1 adds an "Alternate take …" hint. Cache hits inside K are free; further cache hits beyond K are added for variety at no cost (so `unique_clips` can exceed K). Every new (paid) clip is used by at least one shot; unused specs are dropped and keys renumbered `c01…`.
- `clip_specs[]` extra keys: `variant` (take index), `section`, `hints: {preset, palette, scene_type, camera_move, section}` (for the mock provider's `ClipRequest.scene`). Shots extra keys: `scene_id`, `section`. For cache hits the shot already carries `clip_id` (= `existing_clip_id`).
- BatchPlan extra top-level `warnings: [ja]` (song shorter than duration, start clamped, no chorus found, `max_new_clips` limiting the pool…); `stats` extra `pool_target`, `clip_duration`, `shots_per_video: [int]`. Library mode: `clip_specs = []`, `stats.reused_clips = unique uploads used`, `new_clips = 0`.
- Shot lengths: "short"/"long" = 1/2 bars; when a bar is < 1.5 s the whole-bar counts that fit are used (e.g. 2/4 bars), when only one whole-bar length fits (slow songs) the other is a half-bar multiple. Whole-bar shots cut on downbeats whenever one is in range, else on the nearest beat; without beats the BPM grid is used. Shots never repeat the same clip back-to-back (pool ≥ 2); clips needing slow motion below 0.6× are avoided; generated clips keep a 0.1 s margin at both ends.
- `lyric_style: "auto"` (part of GenerationSettings; the UI's 自動) rotates the preset's `lyric_styles`. Errors: `ServiceError` 422 with codes `analysis_missing`, `library_empty`, `storyboard_empty`, `no_clips` (`max_new_clips=0` and no cache hit).
- `start_strategy: "varied"` (review fix): candidates are chorus, verse and highlight starts plus `suggested_segments` whose duration is within ±25 % of the video length (bridge sections are not candidates). Windows with < 50 % sung coverage (the minimum of lyric-on-screen coverage and `vocal_activity` coverage — LRC holds over breaks overstate lyrics) or with more than 1.5 s of intro/bridge/break/outro in their first 3 s are dropped unless fewer than 2 remain; the rest are used best first by coverage + 0.5 × mean energy, and the user's own segment stays first. Demo song, 30 × 15 s: starts {24.03, 26.03, 18.04, 12.03, 8.03}.
- Variety warning: the fixed advice to change the reuse setting is shown even when it cannot help (library mode, `max_new_clips` cap) — known issue.

### 6.5 `cost.py`
`PRICING: dict[provider_id, dict[model_id, ModelPricing]]` with `price_per_second_usd`, `clip_durations`, `default_duration`, `source_url`, `as_of`, `verified` (filled from docs/VIDEO_PROVIDERS.md research; mock/library = 0).
`estimate(settings: dict, plan_stats: dict, provider: str, model: str, *, cached_clips: int, llm_estimate_usd: float, budget_limit_usd: float|None) -> dict` **Estimate**:
```json
{"provider":"mock","model":"mock-v1","clip_duration":5,"videos":3,"shots_total":24,"unique_clips":8,"new_clips":8,"cached_clips":0,
 "price_per_second_usd":0.0,"video_generation_usd":0.0,"llm_usd":0.01,"total_usd":0.01,
 "naive_total_usd":0.01,"savings_usd":0.0,             // naive = every shot generated fresh (+ llm, like total_usd)
 "budget_limit_usd":50,"within_budget":true,"variety_score":0.8,"variety_warning":null,
 "comparison":[{"provider","model","total_usd","configured"}],   // same plan priced on every provider/model
 "notes":["ja strings"]}
```
As implemented:
- `PRICING` is built from the providers' `ProviderModel`s (the single source of truth for prices): `ModelPricing{provider, model, name, price_per_second_usd, clip_durations, default_duration, max_clip_duration, source_url, as_of, verified, text_to_video, price_note}`. Helpers: `get_pricing(provider, model=None)` (default model when empty; unknown ids → `ServiceError` 422 `provider_unknown`/`provider_unknown_model`), `clip_cost_usd(provider, model, seconds)`, `TARGET_CLIP_SEC = 5.0`: the clip length the pipeline should request, i.e. `provider_clip_duration = provider.clip_duration_for(model, TARGET_CLIP_SEC)`, which equals `ProviderModel.default_duration`.
- Math: `video_generation_usd = new_clips × clip_duration × $/s`. `clip_duration` is `plan_stats.clip_duration`, else the model default; `library` → 0. Then `total_usd = video_generation_usd + llm_usd`, `naive_total_usd = shots_total × clip_duration × $/s + llm_usd` (one fresh clip per shot, no cache), and `savings_usd = naive_total_usd − total_usd`. Money is rounded to 4 decimals.
- Budget: `budget_limit_usd=None` falls back to `settings["budget_limit_usd"]`; if that is also `None` there is no cap and `within_budget` is true. The caller resolves settings → project → `DEFAULT_BUDGET_USD`.
- `comparison`: the selected model (its cache-aware total) plus every model that can generate from text (`text_to_video`), in registry order. Image-only `gen4_turbo` and `library` appear only when selected. Other models are priced as generating all `unique_clips` at their own `default_duration`, because cached clips are model-specific. `configured` = `provider.check_model(model)[0]`. Rows also carry `name, selected, clip_duration, price_per_second_usd, video_generation_usd, verified`. Known issue: once the project has cached clips, `unique_clips` includes free cache-only extras and ignores `max_new_clips`, so other rows can be over- or under-stated (no wrong spend: selecting a row re-estimates it exactly).
- Additive Estimate fields: `configured` (the selected model is usable now), `as_of`, `pricing_verified`, `pricing_source_url`.
- `notes` (ja): cost breakdown, cache reuse, savings vs. naive (amount and %), LLM share, variety warning, budget overrun, why the selected provider/model cannot start, and for API providers the "price as of … (unverified)" disclaimer.

### 6.6 Rendering (`subtitles.py`, `render.py`, `quality.py`)
**TextStyle** (all optional overrides; defaults depend on lyric_style):
```json
{"font_family":"Noto Sans CJK JP","font_weight":"bold|regular|light","font_size":64,"primary_color":"#FFFFFF","highlight_color":"#4DA3FF",
 "outline_color":"#000000","outline_width":3,"shadow":1,"position":"bottom|center|top","bg_opacity":0.0,"letter_spacing":0,"max_chars_per_line":14}
```
Units (as implemented): `font_size` is the ASS `Fontsize` on the 1080×1920 canvas (Noto CJK glyphs are ≈0.69×font_size wide; scaled proportionally for other sizes); `letter_spacing`/`outline_width`/`shadow` are px on that canvas; `max_chars_per_line` counts full-width characters (half-width Latin ≈ 0.6) and is a soft limit — text never leaves the safe width (the line shrinks to ≥60 % instead); `font_weight` also accepts `thin|medium|semibold|black` (mapped to the fontconfig families `Noto Sans CJK JP Light/Medium/Black`, `Noto Serif CJK JP …`); colours `#RGB|#RRGGBB|#RRGGBBAA`. Invalid values fall back to the style default. In `karaoke`, `primary_color` is the not-yet-sung colour and `highlight_color` the sweep colour.
- `subtitles.build_ass(lines, *, style_id, text_style, segment_start, duration, width=1080, height=1920, beats=None) -> str` — times relative to the segment; lines clipped to [0,duration]; horizontal text only (no rotation tags); Japanese-aware line wrapping (prefer breaking at spaces/punctuation, never inside a word for Latin, max 2 lines); **Instagram Stories safe area**: no text above y=270 px or below y=1920−380 px (top 14%, bottom ~20%); `position` maps to top/center/bottom inside that area.
  Styles: `simple` (white bold sans, thin outline, soft shadow), `cinematic` (serif `Noto Serif CJK JP`, smaller, semi-transparent black box via BorderStyle=3, low position), `minimal` (light weight, small, letter-spacing, no outline, subtle shadow), `karaoke` (`\kf` per word/char from `words`, else even split; highlight_color sweep), `fade` (`\fad(in,out)` with in/out aligned to nearby beats when given, ~250–400 ms).
  Implementation notes: every visual line is its own `Dialogue` (`\an8\pos`), so line pitch and the block's extents (outline, shadow, box padding, fade rise) are computed exactly and kept inside the safe area; soft shadows are a blurred copy on layer 0; user text is escaped (`{`/`}` → `\{`/`\}`, a WORD JOINER after `\` so `\N`/`\h` stay literal, newlines → spaces). Default positions: `simple`/`cinematic`/`karaoke` bottom, `minimal`/`fade` center. Helpers: `safe_area(width, height)`, `wrap_text(...)`, `layout_text(...)`, `resolve_text_style(style_id, text_style)`, `escape_ass_text(text)`.
  Review fixes: lines starting within 0.05 s are one block (`_timed_lines` joins them with a newline, latest end, words of the first line only); `_layout_block` wraps each part separately with the smallest part's font size so the block stacks inside the safe area, and karaoke sweeps only the first part (the others stay static in the not-yet-sung colour). With `bg_opacity > 0` the box is a separate layer-0 event in style `LyricBox` (BorderStyle=3, always black at `bg_opacity`, invisible glyphs, padding covers outline + blur) under the layer-1 text event, whose `Lyric` style is BorderStyle=1 with the user's outline colour/width (no hard shadow in box mode). The editor/lyrics preview mirrors this (black box, outline kept, no shadow; same-start lines stacked).
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
    crf: int = 18; preset: str = "veryfast"; audio_bitrate: str = "256k"   # preset: see measurements below
    audio_fade_in: float = 0.15; audio_fade_out: float = 0.8; transition: str = "cut"
@dataclass
class RenderResult: output_path: str; thumbnail_path: str; duration: float; size_bytes: int; log_tail: str
                    timings: dict[str, float] = {}   # prepare/encode/finalize/total seconds (added)
```
  Each shot: trim → speed → scale-to-cover → animated crop (Ken Burns zoom/pan, cheap `crop` expressions; avoid `zoompan`) → optional hflip → fps 30, setsar 1; concat (cut) or `xfade` (crossfade 0.3 s, durations compensated so total = duration); color grade; subtle film grain/vignette per grade; burn ASS with `subtitles=...:fontsdir=` (libass; escape paths). Audio: single encode from the **original file** (`-ss audio_start -t duration`), `afade` in/out, AAC-LC, keep source sample rate if 44.1/48 kHz else 48 kHz, stereo. Output: H.264 High, yuv420p, 1080×1920, 30 fps CFR, `-movflags +faststart`, GOP 60. Thumbnail: JPEG 540×960 from ~1 s. `progress_cb(fraction, stage)` best-effort (parse `-progress pipe:1`).
  As implemented: thumbnail is written next to the output as `<output stem>_thumb.jpg`; the MP4 is written to a hidden `.<stem>.part.mp4` and renamed on success. Progress stages: `prepare` (0.0) → `encode` (0.03–0.95) → `thumbnail` → `done` (1.0); an exception raised by `progress_cb` kills FFmpeg and propagates (use it for cancellation). Shot chains work in planar RGB (`gbrp`; per-frame `scale=eval=frame` for the zoom + `crop` x/y expressions for the pan, so no double resampling; the order is `scale(eval=frame) → crop → format=gbrp` — `crop` clamps x/y to its *input link* size, so it must follow the per-frame scale directly or zoom-ins anchor to the top-left; regression-tested with dot tracking), the source matrix is honoured (`in_color_matrix`, untagged HD → BT.709) and the output is tagged BT.709. Clips shorter than needed are slowed down to ≥60 % speed, else looped (`-stream_loop`); rotation (autorotate) and non-square SAR are honoured. Colour grades: `presets.COLOR_GRADES[id].ffmpeg_filter` when importable and valid (checked once with a tiny FFmpeg run), else `render.FALLBACK_COLOR_GRADES` (same ids, cheap `colorchannelmixer`+`curves`). Runs of expensive point filters (`colorbalance`, `colortemperature`, …) are baked into a 36³ `.cube` (Hald CLUT sampled through the same chain) and applied with `lut3d` (≈4× faster, mean error <1/255); vignettes become a cached mask frame + `blend=multiply`; grain is a soft half-resolution luma noise layer (`blend=grainmerge`), only for film-look grades. Measured in the dev container (2 cores via `taskset`, 15 s 1080×1920, CRF 18): veryfast ≈21–31 s per video (31 s with grain), faster ≈28 s, medium ≈34 s; SSIM vs. lossless is identical across presets (0.969), hence the `veryfast` default. The render writes `filtergraph.txt`, `lyrics.ass`, `ffmpeg.log` (and `grade_*.cube`) into `work_dir`. `scripts/render_sample.py` renders one 15 s sample per lyric style into `backend/var/samples/`.
- `quality.validate_output(path, *, expected_duration, width=1080, height=1920, fps=30) -> dict` **QualityReport** `{"passed": bool, "checks":[{"name","passed","value","expected"}], "loudness_lufs": float|null, "black_ratio": float}` — checks: container mp4, video h264 yuv420p, size, fps 30, duration ±0.1 s, audio aac present, audio duration matches, faststart (moov before mdat), black frames ratio < 0.5 (`blackdetect`), integrated loudness (`ebur128`, informational only).
  Check names: `container, video_codec, pix_fmt, resolution, fps, duration, audio_codec, audio_duration, faststart, black_frames, loudness` (`readable` alone when ffprobe cannot read the file); `passed` = all checks passed (`loudness` always passes). Never raises for bad media.

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
    def generate(self, req: ClipRequest, out_path: str, *, timeout: float = 900, on_progress=None, cancel_check=None,
                 on_submitted=None) -> ClipResult: ...   # on_submitted: extension, see "As implemented"
```
`registry.get_provider(id) -> VideoProvider`, `registry.list_providers() -> list[VideoProvider]`, `registry.provider_info() -> list[ProviderInfo dict]`.
- `mock`: synthesizes a 9:16 1080×1920 (or 720×1280 upscaled later) H.264 clip locally with FFmpeg lavfi in ~1–3 s: preset palette gradients + motion + grain + vignette, deterministic per (prompt, seed); small corner label `MOCK` so it is never mistaken for real output. Cost 0. Always configured.
- `library`: no generation; the planner maps scenes onto uploaded clips. `generate` raises `ProviderError` (not used). Always configured.
- `kling`, `runway`, `veo`: official REST APIs per docs/VIDEO_PROVIDERS.md (submit → poll with backoff → download). Configured only with keys. Retryable vs non-retryable errors distinguished (429/5xx/timeouts retryable). Unit-tested with `respx` mocks only (marked `verified: false` until a real key test passes).

As implemented:

**Dataclass additions.** Each has a default, so constructing with only the contract fields still works.
- `ProviderModel.default_duration`: the clip length for a 5 s request.
- `ProviderModel.text_to_video` / `image_to_video`: `text_to_video=False` marks image-only models such as Runway `gen4_turbo`. They are never used for text-only requests.
- `ProviderModel.source_url`, `as_of`, and `params` (adapter request settings).
- `ProviderInfo` adds `default_model`. Its model entries add `default_duration`, `text_to_video`, `image_to_video`, `as_of` and `source_url`.

**`VideoProvider` extras.**
- `get_model(id|None)` returns the default model when the id is empty. An unknown id raises `ProviderError(code="provider_unknown_model")`.
- `is_available()`: for `mock`, whether FFmpeg is present.
- `check_model(model, *, has_image=False) -> (ok, reason_ja)` checks keys (including Kling's per-model keys) and image requirements. Batch creation should answer 422 when it returns false.
- `require_model(...)` is the raising form of `check_model`. Missing credentials, including Kling's per-model key via `model_credentials_problem(model)`, raise `ProviderNotConfigured`. An unsuitable request raises `ProviderError(code="provider_bad_input")`.
- `info()` builds the `ProviderInfo` dict.
- Constructor kwargs `sleep`, `clock`, `poll: PollConfig` and `transport` exist for tests.

**Crash safety (review fix).** Hosted APIs (`ApiVideoProvider`):
- `generate(..., on_submitted=None)`: `on_submitted(job_id, resume_token)` is called right after the job was created (exceptions are swallowed). The pipeline persists `Clip.external_job_id` and `params.open_job = resume_token` at once. `mock` / `library` accept and ignore the keyword.
- `resume(job_id, req, out_path, *, timeout=900, cancel_check=None, on_progress=None) -> ClipResult` finishes such a job (poll → download → probe) without submitting again; same errors as `generate`. `resume_token(job)` is the job id, except Kling (`new:<external_task_id>:<task id>` / `legacy:<task id>`, because the new API is polled by external id); `_job_from_token` is its inverse.
- `supports_remote_cancel: ClassVar[bool]`: `True` for Runway (DELETE), `False` for Veo and Kling — an abandoned job there may still run and be billed.
- Runway `FAILED` / `CANCELLED` tasks carry their reported `cost.credits × $0.01` as `ProviderTaskError.cost_usd` (often 0; `None` when not reported).
- Kling (new API): a task that the `external_task_ids` lookup does not list for `MAX_LOOKUP_MISSES = 6` polls in a row (≈ 1.5 min) fails with a non-retryable `ProviderTaskError` instead of waiting for the 900 s timeout; an unexpected lookup shape is a retryable `provider_bad_response` (bounded by the poll-error limit).

**`generate(...)` behaviour.**
- `on_progress(fraction, stage)` reports stages `submitted|queued|running|downloading|done`. It is best effort and its exceptions are swallowed.
- `cancel_check()` is checked before submit, between polls and during download.
- Errors:
  - Missing keys → `ProviderNotConfigured`.
  - Rejected before any job exists → `ProviderError(retryable)`. Retryable: 429, 5xx, 408, timeouts and connection errors. Not retryable: other 4xx.
  - Failures after submission → `ProviderTaskError(ProviderError)` with `external_job_id` and `failure_code`.
  - Cancel → `ProviderCancelled(ProviderTaskError)` with `code="provider_cancelled"`, not retryable.
  - Timeout → `ProviderTaskError(code="provider_timeout", retryable=True)`.
  - Runway tasks are DELETEd on cancel or timeout. Veo and Kling have no cancel API, so an abandoned job may still be billed.
- Up to 5 consecutive transient poll errors are tolerated.
- After a job **succeeded** it is billed and never submitted again: only download + probe are retried, up to `OUTPUT_FETCH_ATTEMPTS = 3` times on the poll backoff within the timeout, re-reading the job's output in between (`ApiVideoProvider._refresh_output`; the default re-polls — Runway hands out fresh pre-signed URLs). Every error raised after success is a `ProviderTaskError` with `retryable=False` and `cost_usd` = the reported cost or the table price (`ProviderTaskError.cost_usd: float | None`); a `ProviderCancelled` raised during the fetch also carries it. A succeeded job is never cancelled/DELETEd.
- Downloads:
  - Written atomically and capped at `MAX_VIDEO_MB`.
  - Credentials go only to the API origin, never to redirect targets.
  - The file is probed with ffprobe.
- `ClipResult` contents:
  - `width/height/duration` are the probed values.
  - `cost_usd` is Runway's `cost.credits × $0.01` when reported, otherwise price × requested (snapped) duration.
  - `raw` holds `requested_duration` and the submit/final status, never secrets.

**Mock.**
- `ClipRequest.scene` is the planner's `clip_specs[].hints` (`preset, palette, scene_type, …`).
- Output: 24 fps, 1080×1920 for 9:16 (16:9 and 1:1 also work). Any duration up to 20 s, rounded to whole frames.
- Encoding: H.264 Constrained Baseline (`ultrafast`), no audio, bit-exact for the same (prompt, negative, seed, scene).
- Speed: about 2 s per 5 s clip (1.6–2 MB) on the 4-core dev box.
- With `image_path`, the still image becomes the background.

**Models.** All API prices are `verified: false`, as of 2026-10-11.

| provider | model | notes |
|---|---|---|
| veo | `veo-3.1-lite-generate-preview` | default; 720p; 4/6/8 s; $0.05/s |
| veo | `…@1080p` | 8 s; $0.08/s |
| veo | `veo-3.1-fast-generate-preview` | $0.10/s |
| veo | `veo-3.1-generate-preview` | $0.40/s |
| runway | `gen4.5` | default; 2–10 s; $0.12/s |
| runway | `gen4_turbo` | image-only; $0.05/s |
| runway | `veo3.1_fast` | 4/6/8 s; silent; $0.10/s |
| kling | `kling-3.0` | new API with `KLING_API_KEY`; 3–15 s; $0.112/s |
| kling | `kling-v2-5-turbo` | legacy API (AK/SK HS256 JWT); 5/10 s; $0.042/s; the default when only AK/SK are set |

**Registry.**
- Order: mock, library, veo, runway, kling.
- `get_provider(unknown)` → `ProviderNotConfigured(code="provider_unknown")` (422).

## 7. Pipeline & jobs (`pipeline.py`, `worker.py`)

Task functions (idempotent, take IDs, open their own DB session):
- `analyze_project_audio(project_id)` → runs `analyze_audio`, stores analysis, sets default segment (best 15 s suggestion) if none, `analysis_status=done|failed`.
- `run_batch(batch_id)` → status `planning`: storyboard (Claude or template; record LLM CostRecord) → `plan_batch` → create Clip rows (reuse by cache_key: CostRecord `cached=true`, amount 0) and Video rows (status `waiting_clips`, plan with `clip_key`s) → status `generating_clips`: generate missing clips (bounded concurrency, budget check before each paid clip → `budget_exceeded`, cancel check, retries with backoff for retryable errors up to 3 attempts) → resolve `clip_id`s into video plans → status `rendering`: enqueue `render_video` per video → batch status recomputed when each video finishes (`completed` all done; `partial` some failed; `failed` none done).
- `render_video(video_id)` → download inputs via storage to work dir, `render_video` → `validate_output` → upload MP4 + thumbnail → `status=done` (quality stored even if a check fails; failed hard checks → `status=failed` with error).
- `TASK_MODE=inline`: `worker.enqueue(name, *args)` submits to process-wide thread pools (one per task kind, see below); `celery`: `apply_async`. Same task bodies either way. Celery app name `mvf`, tasks `mvf.analyze`, `mvf.run_batch`, `mvf.render_video`. Run worker: `celery -A app.worker.celery_app worker -l info -c 2`.
- Cancellation: batch `cancelled` → pending/waiting videos → `cancelled`; running tasks check between steps.

As implemented (`app/pipeline.py`, `app/worker.py`; review fixes included):
- **Shared helpers** (used by the routers): `preview_batch(db, project, settings) -> BatchPreview{settings, storyboard, plan, estimate, budget_limit_usd}`, `create_batch(db, project, settings) -> Batch` (guards above), `cancel_batch`, `reopen_batch`, `prepare_retry`, `update_batch_budget`, `apply_video_edit(db, video, VideoEdit)`, `regenerate_video_plan(db, video, new_clips=)`, `recompute_batch_status(db, batch_id)`, `require_rights(project)`, `resolve_generation_settings`, `existing_clip_refs(db, project_id, include_in_flight=)`, `stored_storyboard`, `effective_lyric_lines(lines, overrides)`, `retime_line`, `download_filename`, `generate_clip(clip_id, cancel_check=)`, `resolve_plan_clips(db, plan, project_id, preferred=, provider=, model=)`, `llm_cost_record`, `abandon_video`, `recover_interrupted_jobs(stale_after=None)`.
- **Rights**: `require_rights` (422 `rights_unconfirmed`) guards batch creation, regenerate, edit and retry; the tasks re-check it (`run_batch` fails the batch, `generate_clip` fails the clip without submitting, a render fails the video), so withdrawing consent stops queued work.
- **Planning** (`run_batch`, status `planning`):
  - The storyboard comes from Claude when `ANTHROPIC_API_KEY` is set, else the template. Library mode has no storyboard.
  - **Claude storyboard reuse**: storyboards carry `world_key` = sha256 of (preset, custom world, title, artist, mood, purpose, audio asset, lyric texts, and the artist aliases when set). `stored_storyboard()` returns the project's latest Claude storyboard with the same key; planning and `POST /estimate` reuse it (the estimate then has `llm_usd` 0, counts its cache hits and adds a note). `new_storyboard: true` asks for a fresh one. Without Claude the template is deterministic per song/preset anyway.
  - An LLM `CostRecord` is booked whenever `cost_usd > 0` (also when the batch was cancelled meanwhile), and that amount goes into `batch.cost_usd`.
  - `plan_batch` uses the settings seed and the project's ready clips **plus clips other running batches are still generating** (`include_in_flight`: a matching `cache_key` is a hit; the batch waits for that clip instead of paying for it twice). The clip cache is **per project**.
  - Clip rows are created **at planning time**: `pending`, `source = provider id`, `params = {batch_id, key, seed, duration, variant, section, hints, aspect_ratio}`. Shot `clip_id`s are resolved immediately and stored in `plan.clip_specs[].clip_id`. Cache hits get a `CostRecord(cached=true, amount 0, units = clip seconds, unit_price = model price)`. Videos are created as `waiting_clips`.
  - Rows, `batch.plan`, storyboard, settings and cost are committed in one transaction **only while the batch is still `planning` and this task owns it** (conditional UPDATE on `status` and the `started_at` token); a batch cancelled during a long Claude call gets no rows.
- **Budget** (no per-task in-memory guard): before **every** clip attempt its estimated price is reserved atomically in the database — `UPDATE batches SET cost_usd = cost_usd + :est WHERE id = :id AND cost_usd + :est <= :limit` (`_reserve_budget`); rowcount 0 = refused. So `batch.cost_usd` includes in-flight reservations and two renders/tasks can never both spend the same headroom. Each attempt settles its reservation to the real cost, to the billed cost of a failed attempt, or to 0. The admission checks of regenerate/retry (`_check_batch_budget`) count spend + every pending clip of the batch.
- **Generation** (`generating_clips`):
  - Before generating, the whole plan is priced; if spend + new clips would exceed `estimate.budget_limit_usd`, the batch goes to `budget_exceeded` and nothing is generated.
  - Clips are generated by `CLIP_CONCURRENCY = 2` threads. Each clip is **claimed** first (`pending → generating` with a conditional UPDATE): two tasks never generate the same row; a clip another task owns is waited for (`_wait_for_clips`, cancellable; a `generating` clip without heartbeat for `STALE_CLIP_SEC` = 600 s, or a `pending` one whose batch no longer runs, is not waited on). Generating clips heartbeat `updated_at` every `CLIP_HEARTBEAT_SEC` = 30 s through the cancel check.
  - A budget refusal cancels that clip (`params.budget_refused`) and the clips not started yet; after the in-flight ones finish the batch becomes `budget_exceeded`: pending clips and waiting videos are `cancelled`.
  - Retries: up to `MAX_CLIP_ATTEMPTS = 3` for retryable `ProviderError`s, with backoff `RETRY_BACKOFF_SEC = (5, 20)` (cancellable) — **unless the failed attempt is known or presumed billed**, which fails the clip instead of paying twice.
  - **Billed failures** (`_billed_usd`): a provider-reported `exc.cost_usd` is authoritative, even 0 (Runway credits of a FAILED task; the price of a succeeded job whose video could not be fetched); `exc.billed` → the estimate. A remote job that *ended with an error* (`provider_failed`, `provider_content_filtered`) is presumed not billed and may be retried. Presumed billed at the table price: unusable output of a finished job (`provider_bad_output`) and a job **abandoned** while it may still run (timeout, lost polling, cancel) on a provider without remote cancel (Veo, Kling); Runway's abandoned tasks are DELETEd. Every billed attempt gets a `CostRecord` (note 課金済み・生成失敗, plus （推定） when presumed) and its amount in `clip.cost_usd` / `batch.cost_usd`. A paid result that cannot be stored (storage error) is booked too.
  - **Job ids**: providers report the job right after submission (`on_submitted`); the clip keeps `external_job_id` and `params.open_job`. A clip with an open job and a provider with `resume()` resumes polling that job instead of submitting again (once).
  - `ProviderCancelled` → clip `cancelled`. `ProviderNotConfigured`, non-retryable errors and unexpected errors → `failed`, with a Japanese `error`. `ProviderTaskError` keeps `external_job_id` / `failure_code`.
  - A successful clip is stored as `Asset(kind=clip)` + `Asset(kind=thumbnail)` with a `CostRecord(video_generation, units = raw.requested_duration)`.
  - `cancel_check` = `CancelWatcher` (throttled DB read, every ≤ 1 s); for `run_batch` any batch status other than `generating_clips` counts as cancelled.
- **Start rendering**: the batch moves to `rendering` first (so two `run_batch` tasks cannot split the videos). Shots whose clip is not ready (failed, cancelled or deleted) are **substituted** by `resolve_plan_clips`: only clips of the same footage family (same provider and model; never mock clips in an API batch; uploads only in a library batch), the batch's own pool first, then the project's; ranked by avoiding the neighbouring shot's clip, same storyboard scene, least used. `src_in` is clamped; `plan.substitutions` counts them and shots keep `substituted_from`. A video with no acceptable clip at all → `failed` with a clear error. When a clip of the video's own batch was refused for budget, the video fails with the budget message instead of being substituted. Waiting videos → `queued`, then one `render_video` is enqueued per video (an enqueue failure fails that video and recomputes the batch).
- **Render**:
  - Pending clips of the plan are generated first (this is how regenerate/retry create clips); missing clips are substituted.
  - The video is **claimed** with a compare-and-swap (`queued`/`waiting_clips` → `rendering`, `attempts + 1`); a duplicate or stale task that loses the claim exits without side effects, and every later transition (progress, cancel, fail, done) is conditioned on the claimed `attempts`. Inputs are read via `storage.local_path` (local backend) or `fetch_to` into `WORK_DIR/renders/<id>-*`.
  - `render.render_video` gets the effective lyric lines, `analysis.beats` and the plan's style, grade and transition. Its `progress_cb` writes `progress` (0.02–0.95, ≤ 1/s) and raises when the batch or video was cancelled; FFmpeg is then killed.
  - Then `quality.validate_output`. A failed report → video `failed` with the failed checks listed in `error`, and the report kept in `video.quality`.
  - Otherwise the MP4 and thumbnail are uploaded as `Asset(kind=render)` (filename = download name) + `Asset(kind=thumbnail)`, and the video → `done` (`plan.previous` dropped). Assets of the previous render are deleted.
  - A cancelled or failed **re-render** of a finished video (`plan.previous` present and its MP4 still stored) returns to `done` with the previous plan, `render_version` and quality; `error` explains (`_end_video`).
  - The work directory is always removed, and the batch status is recomputed.
- **Cancel** (`cancel_batch` → `_stop_batch`): queued/waiting **and rendering** videos → `cancelled` (a running render notices at its next progress check and its `done` transition fails); every `pending` clip with `params.batch_id` = the batch → `cancelled` (also clips regenerate/retry created outside `plan.clip_specs`). Cancelling a batch re-opened for a re-render (`plan.reopened_from` = completed/partial/failed) restores the previous renders and recomputes its status; a re-opened `budget_exceeded` batch gets that status and reason back.
- **Concurrency safety**: status changes use conditional `UPDATE … WHERE status IN (…)`, so a cancel is never overwritten. `recompute_batch_status` locks the batch row (`SELECT … FOR UPDATE`). It acts only on `rendering|completed|partial|failed` batches; `cancelled` and `budget_exceeded` are final until a video is re-rendered. `partial` = some done, others failed **or cancelled** (the UI says 一部キャンセル when none failed).
- **Recovery**: `recover_interrupted_jobs(stale_after=None)`.
  - Inline mode (API start-up, `stale_after=None`): re-enqueues `queued`/`running` analyses, upstream batches (`run_batch` resumes: a stored plan skips planning) and queued/waiting/rendering videos of `rendering` batches (rendering ones are reset to `queued` first). Deletes task scratch directories (`WORK_DIR/{analysis,clips,renders,whisper}/*`) older than `STALE_WORK_SEC` (2 h). Inline mode is therefore single-process.
  - Celery (`worker_ready` signal, `stale_after = STALE_TASK_SEC` = 600 s): only work idle longer than that (upstream batches judged by their clips' heartbeats; videos by `updated_at`) is picked up, because other workers may be running. Redis would re-deliver an unacknowledged task only after the 6 h visibility timeout, so a crashed worker's work resumes when a worker (re)starts. Duplicate deliveries are harmless (clip and render claims).
  - Both settle terminal batches that still have active videos or pending clips, and reset `generating` clips whose task died to `pending`: the reservation is released; a job that can be resumed keeps `params.open_job`; an attempt that may have reached a provider that neither reports submissions nor resumes is booked as presumed billed (CostRecord 中断された生成ジョブ（課金済みとみなす）).
- **Inline pools** (`worker.INLINE_POOLS`): one `ThreadPoolExecutor` per task kind — `run_batch` 2, `render_video` 2, `analyze` 1 (others share a pool of 2) — so clip generation that waits minutes on provider APIs never starves analyses and renders.
- **Celery** (`worker.py`):
  - `celery_app = Celery("mvf", broker=REDIS_URL)` with `task_acks_late`, `task_reject_on_worker_lost`, `worker_prefetch_multiplier=1`, JSON only, `task_ignore_result`, queues `mvf` (default) and `mvf_batch` (`mvf.run_batch` is routed there), Redis `visibility_timeout` 6 h, a short publish retry policy and `imports=("app.pipeline",)`. A worker started without `-Q` consumes both queues; a dedicated `-Q mvf_batch` worker is optional.
  - Tasks `mvf.analyze` / `mvf.run_batch` / `mvf.render_video` call the registered bodies (`worker.CELERY_TASK_NAMES`).
  - `worker_process_init` disposes the inherited DB pool; `worker_ready` runs the recovery sweep above.
  - `DISPATCHERS["celery"]` publishes with `apply_async`. Names without a Celery task fall back to inline with a warning. A broker error raises `worker.QueueUnavailable` (`ServiceError`, 503 `queue_unavailable`). Routes turn it into a failed analysis, batch or video (re-renders fall back to the previous render) plus a 503, and the re-opened batch is recomputed; the audio upload still returns 201, with `analysis_status = failed`.

## 8. Frontend (Next.js 16, `frontend/`)

- Talks to the backend directly from the browser: `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`). Optional `NEXT_PUBLIC_API_TOKEN` is NOT used; in `token` auth mode the user pastes a token on `/settings` (stored in localStorage) and the client sends `Authorization: Bearer`.
- Typed client in `src/lib/api.ts` mirroring section 5 exactly; types in `src/lib/types.ts`.
- UI kit: shadcn/ui-style components hand-written in `src/components/ui/` (shadcn registry is unreachable here) on `radix-ui`, `class-variance-authority`, `clsx`, `tailwind-merge`, `lucide-react`; data fetching with `swr` (polling while jobs run).
- Design: dark only. Background `#0a0a0a`, surfaces `#111113`/`#18181b`, borders `#27272a`, text `#e4e4e7`/muted `#a1a1aa`, **blue `#3b82f6` only for primary actions/progress**, red for errors, no decorative animation. Desktop-first; large 9:16 previews. Japanese UI copy.
- Routes: `/` dashboard · `/projects/new` · `/projects/[id]` (tabs: 楽曲解析 / 歌詞 / 素材 / 権利確認) · `/projects/[id]/generate` · `/batches/[id]` progress · `/projects/[id]/videos` gallery · `/videos/[id]/edit` editor · `/settings` providers & usage.
- Foundation modules (implemented; pages build on these — details in `frontend/README.md`):
  - `src/lib/api.ts`: one function per §5 endpoint (e.g. `getProject`, `uploadAudio(id, file, {onProgress, signal})`, `estimateBatch`, `createBatch`, `getBatch`, `editVideo`, `downloadVideo`, `downloadBatchZip`), `apiPaths` (= SWR keys), `ApiError {status (0 = backend unreachable), code, detail, message (ja)}`, `isBackendUnreachable`, `errorMessage`, token helpers. `GET /api/batches/{id}` is typed `BatchDetailOut` (= `BatchOut` + `videos`, `clips`).
  - `src/lib/types.ts` (§5/§6 shapes), `status.ts` (ja labels + `isBatchActive` …), `format.ts`, `constants.ts` (`RIGHTS_ITEMS`, `WORLD_PRESETS_FALLBACK`, `DEFAULT_GENERATION_SETTINGS`, `DEFAULT_LIMITS`).
  - `src/hooks`: `useProject(s)`, `useAnalysis`, `useLyrics`, `useClips`, `useBatch`, `useProjectBatches`, `useVideos`, `useVideo`, `useDashboard`, `useCapabilities`/`usePresets`/`useLimits`, `useHealth`, `useApiToken`, `useNow`, `useHydrated` — each polls only while work is in flight.
  - Layout: `AppShell` (sidebar + top bar, wraps pages in `<Suspense>`), `PageHeader`, `ProjectHeader({projectId, active, batchId?, actions?})`, `ProjectNav({projectId, active: "assets"|"generate"|"jobs"|"videos", batchId?})`. The 生成ジョブ item links to `/batches/{batchId ?? latest batch of the project}` (disabled while the project has no batch).
  - UI kit in `src/components/ui/` incl. `StatusBadge({kind: "batch"|"video"|"clip"|"analysis", status})`, `ErrorState({error, onRetry})`, `EmptyState`, `StatCard`, `Thumbnail` (9:16), `FileDropzone`, `WorldPresetPicker`, `ConfirmDialog`, `toast()`, `RadioCards`, `NumberInput`, `ColorInput`.
- Generation, progress, gallery and editor pages (implemented):
  - `/projects/[id]/generate` (`src/components/generate/`): the form starts from the latest batch's settings (else the project's preset); live estimate = debounced (500 ms) `POST …/estimate`, requested only once the analysis is done; `POST …/storyboard` preview; readiness checks link to `/projects/[id]?tab=analysis|lyrics|materials|rights`; 生成開始 → `POST …/batches` (confirmation dialog when `total_usd > 0`) → `/batches/[id]`.
  - `/batches/[id]` (`src/components/batch/`): polls `GET /api/batches/{id}` every 2 s while the batch **or any of its videos/clips** is active (a retried video in a finished batch keeps polling); cancel, per-video retry, batch ZIP.
  - `/projects/[id]/videos` (`src/components/videos/`): filters in the URL (`?batch=<id>&fav=1`), grouped by job, preview dialog (`<video>` on the signed URL, quality report), favourite, regenerate, single download, selection ZIP (`POST /api/videos/zip`).
  - `/videos/[id]/edit` (`src/components/editor/`): diff-only draft → `PUT …/edit` (semantics in §5.7), then polls `GET /api/videos/{id}` until the new `render_version` is done. Song duration/sections for the start slider come from `GET /api/projects/{project_id}`.
- Review follow-ups (implemented): project form and info card edit `artist_aliases`; the analysis tab shows the world estimate (`WorldEstimateCard`, `useWorldEstimate`) and the generate page offers its recommended presets (pre-selecting the first one on a first run when the project is still on the default preset without a custom world); the generate form sends one random `seed` per form for estimate and batch, and a 「絵コンテを Claude で新しく作り直す」 switch (`new_storyboard`, never carried over) when Claude is configured; the batch page offers 「予算を変更」 (`PATCH /api/batches/{id}`) on `budget_exceeded`, explains a re-opened batch (`reopened_from`, `previous_error`), labels a `partial` batch without failures 一部キャンセル, and the cancel dialog describes the real semantics (§7); batch table, gallery, preview and editor show substitutions and the fallback `error` of a `done` video; re-render 422s (`rights_unconfirmed`, `budget_exceeded`) get a next-step hint; 「Powered by Runway」 (link to runwayml.com) on Runway cards, batches, clips and the settings row; the lyrics preview stacks same-start lines and draws box styles without shadow; the player tells a missing H.264 decoder apart from an expired URL; thumbnails refetch API data (≤ once per 30 s) when a signed URL expired; changing the API token clears the SWR cache; `--color-subtle-foreground` is `#8b8b94` (WCAG AA on the surfaces).
- Browser E2E: `frontend/playwright.config.ts` + `frontend/e2e/` (Playwright against the production build on :3000 and the inline-mode API on :8000; full journey with the mock provider and a library-provider run; screenshots in `frontend/e2e/screenshots/`). See `frontend/e2e/README.md`.

## 9. Rights & safety

Rights checklist keys (all required before batch creation):
`audio_rights` 音源の権利を保有、または利用許諾を得ている · `lyrics_rights` 歌詞の権利を保有、または利用許諾を得ている · `no_real_person_likeness` 実在の人物（アーティスト本人を含む）の肖像を生成しない · `model_terms_checked` 使用する動画生成モデルの利用規約・商用利用条件を確認した · `platform_terms` 投稿先（Instagram 等）の規約を確認した.
Uploaded media is private (signed URLs only), file types are validated by content (ffprobe) not only by extension, filenames are sanitized, paths never come from user input.
Details and the list of what is not covered yet: `docs/RIGHTS_AND_SECURITY.md`.

## 10. Foundation module APIs (implemented; other modules call these)

- **config**: `get_settings()` (cached; tests call `get_settings.cache_clear()`). Field names = env var names (`settings.WHISPER_MODEL`). Helpers: `settings.cors_origins`, `max_audio_bytes`, `max_video_bytes`, `is_sqlite`, `anthropic_configured`. Secret fields are excluded from `repr`. Relative dirs resolve against `backend/`; `STORAGE_DIR`/`WORK_DIR` are created on load.
- **db**: `Base`, `SessionLocal` (`autoflush=False, expire_on_commit=False`), `get_db`, `DbSession` (Annotated dependency), `init_db(bind=None)` (+ `add_missing_columns(bind)`, §4), `make_engine(url)`, `get_engine()`, `set_engine(engine)` (rebinds `SessionLocal`; prefer `get_engine()` over importing `engine`). SQLite enables `PRAGMA foreign_keys`.
- **models**: exactly §4, plus `new_id()`, `utcnow()` and `StrEnum`s `AnalysisStatus`, `LyricsFormat`, `AssetKind`, `ClipSource`, `ClipStatus`, `BatchStatus`, `VideoStatus`, `CostKind`, `CostUnit`; `ACTIVE_BATCH_STATUSES`, `ACTIVE_VIDEO_STATUSES`. JSON columns are `JSONB` on PostgreSQL (`JSON` elsewhere) wrapped in `MutableDict`/`MutableList`: top-level mutations are tracked, nested ones need reassignment or `flag_modified`. Set the song via `project.audio_asset = asset` (the projects↔assets FK cycle uses `post_update`). Uploaded clips: `Clip(source="upload", provider="upload", status="ready")` with `Asset(kind="clip")` + `Asset(kind="thumbnail")`; `Video.render_version` starts at 1.
- **storage**: `get_storage() -> Storage` (cached). `Storage.put_file(key, src_path, *, content_type=None, move=False) -> int`, `put_bytes(key, data, *, content_type=None) -> int`, `fetch_to(key, dest_path) -> Path`, `exists`, `delete` (missing ok), `delete_prefix(prefix) -> int`, `size`, `url(key, *, download=False, filename=None, ttl=None)`, `iter_bytes(key, chunk_size)`, `local_path(key) -> Path|None` (local backend only; use it to avoid copies). `make_key(project_id, kind, asset_id, ext)`, `project_prefix(project_id)`, `build_content_disposition(filename)`. Errors: `StorageKeyError`, `StorageNotFound` (→ 404 via app handler).
- **security**: `current_user` / `CurrentUser`, `get_owned_project|clip|batch|video(db, id, user)` (404 when not owned), `sign_media_url(key, *, download=False, filename=None, ttl=None)`, `verify_media_signature(...)`, `seed_users(db)` (token mode: users no longer listed in `API_TOKENS` lose their token hash = revoked; re-adding the same name restores the user and its projects), `hash_token`. Signature covers key, exp, download flag and filename.
- **serializers**: `to_asset_out(asset)`, `to_clip_out(clip, *, used_in_videos=0)`, `to_clip_outs(db, clips)`, `to_video_out(video)`, `to_video_outs(db, videos)` / `prefetch_assets(db, rows)` (lists load all assets in one query; a polled 30-video batch detail takes < 20 queries), `to_batch_out(db, batch, *, include_children=False)` (clips of a batch = ids found in `batch.plan.clip_specs[].clip_id|existing_clip_id` and in its videos' `plan.shots[].clip_id` — the pipeline should store those ids), `to_project_out(db, project)`, `to_project_summary(db, project)`, `to_project_summaries(db, projects)`, `rights_with_defaults`, `all_rights_confirmed`, `video_clip_ids(plan)`.
- **schemas**: request/response models for all of §5 (`GenerationSettings`, `TextStyle`, `VideoEdit`, `LyricLine`, `LyricsUpdate`, `LyricLinesUpdate`, `AutoTimeRequest`, `SegmentUpdate`, `StoryboardRequest`, `RightsUpdate`, `VideoUpdate`, `VideoRegenerateRequest`, `VideoZipRequest`, `ProjectOut`/`ProjectSummary`, `AssetOut`, `ClipOut`, `BatchOut`, `VideoOut`/`VideoDetailOut`, `AnalysisOut`, `LyricsParseOut`, `LyricsOut`, `AutoTimeOut`, `CapabilitiesOut`, `DashboardOut`, `HealthOut`, …) plus `RIGHTS_ITEMS`/`REQUIRED_RIGHTS_KEYS`.
- **services.errors**: each class has `status_code` (MediaValidation 400 — 413 for size, LyricsParse 400, WhisperUnavailable 503, ProviderNotConfigured 422, BudgetExceeded 422, ProviderError 502, others 500) and `code`. A `ServiceError` escaping a route becomes `{"detail": <ja message>, "code": <code>}` with that status. `RenderError(message, stderr_tail)`, `ProviderError(message, *, retryable)`, `BudgetExceeded(message, *, estimate_usd, limit_usd)`.
- **services.media**: `decoded_audio_duration(path, *, limit)` (decoded length, stops past `limit`), `MAX_AUDIO_SEC`, `probe(path)` → `{duration, has_audio, has_video (cover art excluded), width, height (display size after rotation), coded_width, coded_height, fps, rotation, codecs:{video,audio}, pix_fmt, sample_rate, channels, format_name, bit_rate, size}`; `validate_audio_upload(path, filename)`, `validate_video_upload(path, filename)`, `make_thumbnail(video, out_jpg, at=1.0, width=540)`, `sanitize_filename`, `check_extension`, `save_upload_stream` (async) / `save_upload_file` (sync), `copy_stream_limited`, `sha256_file`.
- **worker**: `register_task(name, fn)`, `get_task`, `enqueue(name, *args)` (inline: `Future`; celery: `AsyncResult`; unregistered name: warning + `None`; broker down: `QueueUnavailable` 503), `DISPATCHERS` (`inline`, `celery`), `celery_app`, `CELERY_TASK_NAMES`, `INLINE_POOLS`, `DEFAULT_QUEUE = "mvf"`, `BATCH_QUEUE = "mvf_batch"`, `shutdown()`. Task names: `analyze`, `run_batch`, `render_video`, registered by importing `app.pipeline` (done by `app.main` and by the Celery `imports` setting).
- **HTTP behaviour**: `POST /api/projects` → 201. Project delete and audio replacement → 409 while a batch is active; clip delete → 409 while generating or referenced by a queued/waiting/rendering video. Upload requests are rejected with 413 from `Content-Length` before the body is read (audio: `MAX_AUDIO_MB`+1 MiB, clips: 20×`MAX_VIDEO_MB`+1 MiB, lyrics upload: 2 MiB) and per file while copying. Clip uploads are all-or-nothing.
