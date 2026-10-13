# AI Music Video Factory

アーティストの楽曲と歌詞から、Instagram ストーリーズ向けの縦型（9:16・1080×1920）ミュージックビデオを
**まとめて何本も**作る Web アプリです。映像素材は AI 動画生成（Veo / Runway / Kling の公式 API、または OpenRouter 経由の
各社の動画モデル）、アップロード済みの素材、または動作確認用のモック映像から選べます。歌詞は楽曲に合わせてタイミングを取り、
映像に焼き込みます。

- 楽曲解析: BPM・拍・小節頭・セクション（イントロ／A メロ／サビ…）・サビ候補・おすすめ区間（15/30/60 秒）
- 感情・世界観推定: 音源のムード・歌詞・入力したムードと目的から感情とおすすめの世界観を推定（外部 API なし）。
  初回の生成画面ではおすすめの世界観を初期値にする（プロジェクトで世界観を明示した場合はそちらを優先）
- 歌詞: TXT / LRC / SRT 入力（Shift_JIS 等も自動判定）、ヒューリスティック自動タイミング、
  Whisper による整列（ローカルにモデルがある場合のみ）、手動修正、LRC / SRT 書き出し。
  同じ時刻の行（対訳・デュエットの LRC）は上下に並べて表示。`[サビ]`・`Verse 1:` のような見出し行は歌詞の行に含めない。
  歌詞を保存し直しても、変わっていない行の ID（動画ごとのタイミング調整の対応先）とタイミングは保たれる
- 権利確認: 5 項目のチェックリスト（自己申告）。音源を差し替える・歌詞の本文を変えると、該当する項目が未確認に戻る
- 生成: 世界観プリセット → 絵コンテ（Claude — Anthropic 直接または OpenRouter 経由 — またはテンプレート）→ 素材プール → 動画ごとに
  カット割り・開始位置・ズーム／パン・カラーグレード・歌詞スタイル（「自動」で動画ごとに変える）を変えたバリエーション。
  左右反転は素材内の文字が鏡文字になるため、生成設定の「左右反転を使う」をオンにしたときだけ使う
- 肖像の保護: アーティスト名・別表記（ローマ字など）・「〜似」「lookalike」などの表現と、自由入力に書かれた
  人名らしき語をプロンプトから除去し、全プロンプトに「架空の人物のみ」の指定を付ける
- コスト: 事前見積もり（プロバイダ／モデル比較つき）、予算上限（試行ごとに DB で予約し、上限を超えない）、
  生成済み・生成中の素材の再利用、課金済みの失敗も記録、停止したジョブの予算引き上げと再開
- 仕上げ: 5 種類の歌詞スタイル（simple / cinematic / minimal / karaoke / fade）、ストーリーズのセーフエリア、
  サブピクセル精度の Ken Burns（ズーム／パン）、FFmpeg による H.264 / AAC 出力（音圧の高い音源は true peak −1 dBTP に下げる）、
  品質チェック、個別・ZIP ダウンロード（署名付きリンクでブラウザが直接保存）、動画ごとの手動編集と再レンダリング
  （作り直しが失敗・キャンセルされたら前回の動画に戻る）

設計の詳細（モジュール境界・データ構造・API 仕様）は [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、
動画生成 API の調査は [`docs/VIDEO_PROVIDERS.md`](docs/VIDEO_PROVIDERS.md)、
権利確認とセキュリティは [`docs/RIGHTS_AND_SECURITY.md`](docs/RIGHTS_AND_SECURITY.md) を参照してください。

## 構成

```
 ブラウザ（Next.js 16 管理画面 :3000）
   │  JSON / multipart（AUTH_MODE=token のときは Bearer トークン）
   ▼
 FastAPI（backend/app/main.py :8000） ──────────────► PostgreSQL（プロジェクト・素材・バッチ・動画・費用）
   │  routers/: projects assets clips analysis lyrics generation videos dashboard system media
   │  pipeline.py: 見積もり・バッチ作成のガード、編集／再生成、状態遷移
   │
   │ worker.enqueue("analyze" | "run_batch" | "render_video", id)
   ├── TASK_MODE=inline : API プロセス内のスレッドプール（タスク種別ごと: run_batch 2・render_video 2・analyze 1）
   └── TASK_MODE=celery : Redis ──► Celery ワーカー（キュー mvf: mvf.analyze / mvf.render_video、
                                    キュー mvf_batch: mvf.run_batch）
                                          │（同じタスク本体: app/pipeline.py）
                                          ▼
   analyze      : services/audio_analysis.py（librosa）
   run_batch    : storyboard.py（Claude / テンプレート。llm.py: Anthropic 直接 または OpenRouter 経由）
                  → variation.py（素材プール・動画プラン）
                  → providers/（mock / library / veo / runway / kling / openrouter）で素材生成
                    （試行ごとの予算予約・リトライ・キャンセル・キャッシュ再利用・ジョブ ID の保存と再開）
   render_video : subtitles.py（ASS）→ render.py（FFmpeg）→ quality.py（ffprobe 検証）
   │
   ▼
 ストレージ（storage.py: ローカル or S3 互換）── 署名付き URL /media/...（Range 対応）でブラウザへ配信
```

## 必要なもの

| 項目 | バージョン・備考 |
|---|---|
| Python | 3.13（`uv` で `backend/.venv` を作成） |
| PostgreSQL | 16（`DATABASE_URL`。SQLite も可） |
| Redis | `TASK_MODE=celery` のときのみ必須 |
| FFmpeg | 6.1 以上（libx264・libass 付き） |
| フォント | Noto Sans CJK JP / Noto Serif CJK JP（歌詞の焼き込みに使用） |
| Node.js | 22（フロントエンド） |

## セットアップ

```bash
# サービス（開発コンテナの例）
pg_ctlcluster 16 main start
redis-server --daemonize yes --port 6379
# DB: ユーザー mvf / パスワード mvf、データベース mvf（テスト用に mvf_test）

# バックエンド
cd backend
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env          # SECRET_KEY などを設定
.venv/bin/python scripts/make_demo_assets.py   # デモ用の楽曲・歌詞・素材（合成、権利フリー）

# フロントエンド
cd ../frontend
npm install
cp .env.example .env.local    # NEXT_PUBLIC_API_BASE_URL（既定 http://localhost:8000）
```

テーブルは API の起動時に自動作成されます。マイグレーション（Alembic）はまだありません。既存のデータベースは
起動時に次の範囲だけ自動で更新します（`db.init_db`。どれも何度実行しても同じ結果になります）。

- 後から追加された **NULL 可の列**を `ALTER TABLE … ADD COLUMN` で追加する（`db.add_missing_columns`。例:
  `projects.artist_aliases`、外部キー付きの `cost_records.owner_id` とそのインデックス）
- `cost_records.project_id` を NULL 可・`ON DELETE SET NULL` に変える（`db.relax_foreign_keys`。PostgreSQL は
  `ALTER TABLE`、SQLite はテーブルを作り直す）。プロジェクトを削除しても費用記録が残るようにするため
- 追加した `cost_records.owner_id` をプロジェクトの所有者で埋める（`db.backfill_columns`）

それ以外のスキーマ変更は手動での対応が必要です。

## 環境変数（`backend/.env`）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://mvf:mvf@127.0.0.1:5432/mvf` | SQLAlchemy URL（`sqlite:///...` も可） |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery ブローカー |
| `TASK_MODE` | `inline` | `inline`（API プロセス内）または `celery` |
| `STORAGE_BACKEND` | `local` | `local` または `s3` |
| `STORAGE_DIR` / `WORK_DIR` | `backend/var/storage` / `backend/var/work` | 保存先 / レンダリング作業領域 |
| `S3_ENDPOINT_URL` `S3_BUCKET` `S3_ACCESS_KEY_ID` `S3_SECRET_ACCESS_KEY` `S3_REGION` | – | S3 互換ストレージ |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000` | 署名付きメディア URL の生成に使う公開 URL |
| `CORS_ORIGINS` | `http://localhost:3000` | カンマ区切り |
| `AUTH_MODE` / `API_TOKENS` | `dev` / – | `token` のとき `name:token,...` でユーザーを作成 |
| `SECRET_KEY` | 未設定ならプロセスごとにランダム | 署名付き URL の HMAC 鍵。**本番・Celery 構成では必ず固定値を設定** |
| `MEDIA_URL_TTL` | `3600` | 署名付き URL の有効秒数 |
| `MAX_AUDIO_MB` / `MAX_VIDEO_MB` | `100` / `500` | アップロード上限 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | – / `claude-opus-5-5` | 絵コンテ生成・翻訳に Claude を使う |
| `GOOGLE_API_KEY` | – | Veo（Gemini API） |
| `OPENROUTER_API_KEY` | – | OpenRouter。動画生成プロバイダー `openrouter` を有効にし、`ANTHROPIC_API_KEY` がないときは Claude（絵コンテ）も OpenRouter 経由で呼ぶ（下の「Claude と OpenRouter」）。**未検証** |
| `OPENROUTER_API_BASE` | `https://openrouter.ai/api/v1` | OpenRouter の API の URL（キーはこのオリジンにだけ送る） |
| `OPENROUTER_CLAUDE_MODEL` | `anthropic/claude-opus-5.5` | OpenRouter 経由で使う Claude のモデル ID（`ANTHROPIC_MODEL` とは表記が違う） |
| `OPENROUTER_VIDEO_DEFAULT_MODEL` | `google/veo-3.1-lite` | OpenRouter の既定の動画モデル（モデル一覧に料金付きであるとき） |
| `OPENROUTER_MODELS_CACHE_TTL` | `3600` | OpenRouter の動画モデル一覧（`GET /videos/models`）を使い回す秒数（60 以上。メモリと `WORK_DIR/cache/`） |
| `OPENROUTER_APP_URL` / `OPENROUTER_APP_TITLE` | – / `AI Music Video Factory` | OpenRouter に送るアプリの識別ヘッダー（`HTTP-Referer` / `X-OpenRouter-Title`）。空なら送らない |
| `RUNWAY_API_KEY` / `RUNWAY_API_BASE` | – / `https://api.dev.runwayml.com` | Runway |
| `KLING_API_KEY` / `KLING_ACCESS_KEY` / `KLING_SECRET_KEY` / `KLING_API_BASE` | – / – / – / `https://api-singapore.klingai.com` | Kling（AK/SK は旧 API 用） |
| `DEFAULT_BUDGET_USD` | `50` | バッチの既定予算上限 |
| `WHISPER_MODEL` | `small` | faster-whisper のモデル名またはローカルディレクトリ |
| `FONTS_DIR` | – | libass に追加で渡すフォントディレクトリ |

API キーはサーバー側でのみ読み込み、どの API レスポンスにも含めません（`configured` の真偽値のみ）。

## Claude と OpenRouter

絵コンテを作る Claude は、次の順で経路を選びます（`GET /api/system/capabilities` の `llm` と設定画面に表示）。

1. `ANTHROPIC_API_KEY` がある → Anthropic に直接送る
2. `ANTHROPIC_API_KEY` がなく `OPENROUTER_API_KEY` がある → OpenRouter 経由で Claude（`OPENROUTER_CLAUDE_MODEL`）に送る
3. どちらもない → テンプレートで作る（外部に送らない）

OpenRouter を使う手順（2026-10-13 に実キーで確認: 無料のモデル一覧 `GET /videos/models` の取得と、OpenRouter 経由の Claude による絵コンテ生成 1 回は成功。**動画生成は未検証** — アカウントで ZDR を強制していたため送信段階で 404 で拒否され、課金はありませんでした）:

1. OpenRouter でキーを作り、`backend/.env` に `OPENROUTER_API_KEY=...` を設定する。Celery 構成では API とワーカーの両方に渡す。
2. API とワーカーのホストから `https://openrouter.ai`（`OPENROUTER_API_BASE`）へ HTTPS で接続できるようにする
   （プロキシ・ファイアウォールの許可リスト）。
3. API とワーカーを再起動し、設定画面で OpenRouter が「設定済み」、モデル一覧が「OpenRouter から取得」になっていることを確認する。
   取得できないときは組み込みの 7 モデル（2026-10-13 に取得した実際のモデル一覧の写し）を表示します。
4. OpenRouter のアカウント設定（https://openrouter.ai/settings/privacy）で **プロンプトのログ記録はオフのまま** にし、**アカウント全体の ZDR 強制は有効にしない**。
   有効にすると動画生成は ZDR の対象外のため全提供元が除外され、`POST /videos` が 404（`ZDR violation (account settings)`）で拒否されます
   （2026-10-13 に実際に確認。アプリは「アカウントのプライバシー設定により除外」と表示し、再試行しません）。Claude（歌詞を含む絵コンテ）はアカウント設定に関係なく、アプリがリクエストごとに `zdr: true` と `data_collection: "deny"` を指定します。
5. 生成設定でプロバイダー「OpenRouter」とモデルを選ぶ。見積もりはモデル一覧の料金（取得できないときは組み込みの写し）による概算で、実際の
   費用は完了時に OpenRouter が返す金額（`usage.cost`）で記録します。料金を読み取れないモデルは選べません。

`OPENROUTER_API_KEY` だけを設定すると、動画生成に加えて絵コンテ（**歌詞の本文を含む**）も OpenRouter 経由で Claude に送られます。
歌詞を外部に送りたくない場合は、`ANTHROPIC_API_KEY` と `OPENROUTER_API_KEY` のどちらも設定しないでください（下の「外部 API に送るデータ」）。

## 起動

```bash
# API（inline モード: ワーカー不要）
cd backend && .venv/bin/uvicorn app.main:app --port 8000

# Celery モード（API とワーカーに同じ設定を渡す。SECRET_KEY は必ず同じ固定値にする）
cd backend && TASK_MODE=celery .venv/bin/uvicorn app.main:app --port 8000
cd backend && TASK_MODE=celery .venv/bin/celery -A app.worker.celery_app worker -l info -c 2
# （任意）素材生成専用のワーカー。-Q なしのワーカーは mvf と mvf_batch の両方を処理します
cd backend && TASK_MODE=celery .venv/bin/celery -A app.worker.celery_app worker -l info -Q mvf_batch -c 2 -n batch@%h

# フロントエンド
cd frontend && npm run dev    # http://localhost:3000
```

- inline モードは単一プロセス前提です（`uvicorn --workers` を増やさないでください）。API が途中で落ちた場合、
  再起動時に未完了の解析・バッチ・レンダリングを自動で再開します。素材生成中だった API ジョブは、保存済みの
  ジョブ ID でポーリングを再開し、新しく送信し直しません（二重課金の防止）。
- Celery モードで落ちたワーカーの処理は、Redis の再配信（visibility timeout 6 時間）を待たず、
  **ワーカーの起動時（`worker_ready`）の回収処理**が拾います。10 分（`STALE_TASK_SEC`）以上動きのないバッチ・
  レンダリング・生成中の素材が対象です。つまり、落ちたワーカーを再起動する（または別のワーカーを起動する）と再開します。
  後から重複して届いたタスクは、素材・レンダリングの「確保」（条件付き UPDATE）により何もしません。
  Redis に接続できないときは API が 503（`queue_unavailable`）を返し、バッチは `failed` になります。
- レンダリングは 1 本（15 秒）あたり CPU 2 コアで約 28〜37 秒です（2026-10-11 の計測。カット割りのみ・粒子なしで約 28 秒、
  クロスフェードや粒子つきのカラーグレードで約 37 秒、素材がすべて 1080×1920 のとき約 35 秒）。inline のレンダリング並列数は 2、
  Celery では `-c` で指定したワーカー数です。

## テスト

```bash
cd backend
.venv/bin/python -m pytest tests/ -q                       # 全テスト（PostgreSQL の mvf_test を使用）
TEST_DATABASE_URL=sqlite:////tmp/mvf.sqlite3 .venv/bin/python -m pytest tests/ -q   # SQLite で実行
.venv/bin/python -m pytest tests/test_pipeline.py tests/test_api_*.py -q            # 統合部分のみ
```

テストは有料 API を呼びません（API キーは空に上書きされ、外部 API は HTTP モックのみ）。OpenRouter（動画と Claude の経路）の
テストは `tests/test_providers_openrouter.py`・`tests/test_llm_openrouter.py`・`tests/test_pipeline_openrouter.py` です
（respx のモックのみ。実際の OpenRouter との通信は確認していません）。

## E2E デモ（受け入れテスト）

起動中の API に HTTP だけで接続し、プロジェクト作成から ZIP ダウンロードまでを通しで確認します。

```bash
cd backend && .venv/bin/python scripts/e2e_demo.py --api http://localhost:8000
```

手順は次のとおりです。

1. ヘルスチェック
2. プロジェクトを作成し、デモ曲をアップロードして解析の完了を待つ
3. 歌詞（デモ TXT）を登録する。Whisper が使えない環境では 503 になることを確認する
4. ヒューリスティック自動タイミングを実行する
5. 生成ガードを確認する（権利未確認・キー未設定・素材なし → 422）。そのあと権利を確認し、同じ歌詞を保存し直しても
   行 ID・タイミング・権利確認が変わらないことを確認する
6. デモ素材を 3 本アップロードし、見積もりを取る
7. バッチ A を実行する（mock、3 本 × 15 秒、歌詞スタイルは「auto」で動画ごとに変わる）
8. バッチ B を実行する（アップロード素材、3 本 × 15 秒）
9. 1 本を編集して再レンダリングする（歌詞スタイル変更・歌詞タイミング +0.5 秒・ショットの素材差し替え）
10. 全 MP4 と ZIP をダウンロードする（バッチ A の ZIP は `POST …/zip-link` の署名付きリンクを、API トークンなしの
    クライアントで取得する）
11. ffprobe で各ファイルを検証する（1080×1920 / H.264 / 30fps / AAC / 15.0 秒）
12. 音声を元曲の該当区間と相互相関で照合する

失敗が 1 つでもあると終了コード 1 になります。

2026-10-12 にこの開発コンテナで、OpenRouter の追加後に実行した結果を示します。inline モードと Celery モード
（`celery -A app.worker.celery_app worker -c 2`、API とワーカーに同じ `SECRET_KEY`）の両方で成功しました（inline: 合計 226.7 秒）。
どちらもキーなしのため、OpenRouter は「未設定」で、見積もりのプロバイダー比較に `openrouter`（`google/veo-3.1-lite`、組み込みの候補の料金）
の行が入ることだけを確認しています。以下は Celery モードの出力です（ワーカーが `mvf.analyze` 1 件・`mvf.run_batch` 2 件・
`mvf.render_video` 7 件を処理）。

```
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
| video    | style     | grade       | start | shots | MB   | resolution | vcodec | fps  | acodec | dur(s) | lag(ms) | corr  | quality | result |
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
| A#1      | cinematic | cool_night  | 24.03 | 5     | 6.1  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| A#2      | simple    | natural     | 24.03 | 5     | 7.5  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| A#3      | fade      | teal_orange | 24.03 | 5     | 6.9  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#1      | karaoke   | warm_film   | 24.03 | 5     | 13.4 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#2      | karaoke   | cool_night  | 22.04 | 6     | 12.3 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#3      | karaoke   | natural     | 8.03  | 6     | 12.3 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.999 | pass    | OK     |
| A-edit#1 | karaoke   | cool_night  | 24.03 | 5     | 9.9  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
timings: analysis 6.6s, batch_a 97.2s, batch_b 75.8s, edit_rerender 34.5s, total 220.4s
E2E PASSED: 7/7 videos verified
```

`lag(ms)` と `corr` は、出力動画の音声と元曲の該当区間との相互相関です（ズレ 0 ms・相関 0.998 なら元曲そのまま）。
ほかに、レンダリング単体のサンプルは `scripts/render_sample.py` で `backend/var/samples/` に出力できます。

## ブラウザ E2E（Playwright）

本番ビルドのフロントエンド（`:3000`）と API（`:8000`、inline モード）を実際のブラウザで操作し、ダッシュボードから
プロジェクト作成・解析・歌詞・素材・権利確認・生成・プレビュー・ダウンロード・編集と再レンダリングまでを通しで確認します。

```bash
cd frontend
npm run build
npx playwright test          # 起動済みのサーバーを使い、なければ playwright.config.ts が起動する
```

テストは 14 件（`full-flow.spec.ts` 2 件、`review-fixes.spec.ts` 6 件、`openrouter.spec.ts` 6 件）で、4 CPU で約 8.5 分です。
OpenRouter の「設定済み」の画面は、キーがないためバックエンドの実際の応答を書き換えて確認しています。
詳細（H.264 非対応の Chromium での扱い、スクリーンショットの一覧）は `frontend/e2e/README.md` を参照してください。

## 動画生成プロバイダの状況（2026-10-12 時点）

| プロバイダ | 状態 | 備考 |
|---|---|---|
| モック（`mock`） | **動作確認済み** | FFmpeg で合成するダミー映像。左上に「MOCK」と表示。無料 |
| アップロード素材（`library`） | **動作確認済み** | アップロードした動画だけでカット割り。生成なし・無料 |
| Veo（Gemini API） | 未検証（キーなし） | 実装・HTTP モックテスト済み。無効キーで公式エンドポイントの 400 応答のみ確認 |
| Runway | 未検証（キーなし・ホストへ接続不可） | 実装・HTTP モックテスト済み |
| Kling | 未検証（キーなし・ホストへ接続不可） | 実装・HTTP モックテスト済み |
| OpenRouter（`openrouter`） | **一部検証済み**（2026-10-13: モデル一覧の取得と Claude の絵コンテは実 API で成功。動画生成は未検証） | 実装・HTTP モックテスト済み。モデル・料金は `GET /videos/models` から取得（取得できなければ組み込みの 7 モデル。2026-10-13 の一覧の写し）。形状は公式 SDK（`@openrouter/sdk` 1.4.25）のソースによる。Claude の経路（絵コンテ）は実 API で 1 回成功（下の「検証済み」） |

API モデルの料金は公式料金表・モデルページの調査値（OpenRouter はモデル一覧の料金。いずれも `verified: false`）です。実キーでの検証が済むまでは概算として扱ってください。
Runway の利用規約に従い、UI には runwayml.com へのリンク付きで「Powered by Runway」を表示します（Runway の
プロバイダーカード、Runway 選択時の詳細、Runway を使ったジョブの設定欄・動画一覧・プレビュー、Runway の素材、
設定画面のプロバイダー一覧）。表示が必須かどうかの最終確認は `docs/VIDEO_PROVIDERS.md` §6 の未確定事項です。

## 検証済み / 未検証

この開発コンテナ（4 CPU、API キーなし、huggingface.co・api.klingai.com・api.dev.runwayml.com・openrouter.ai へ接続不可）で
確認した範囲です（特記がなければ 2026-10-12、OpenRouter の追加後）。

**検証済み（実際に動かして確認）**

- バックエンドの全テスト 1268 件（PostgreSQL 16 の `mvf_test`、約 8 分）。外部 API は HTTP モック（respx）と SDK モックのみ。
  うち OpenRouter 関連は 3 ファイルの 144 件（動画プロバイダー、Claude の経路、パイプライン）。
- `scripts/e2e_demo.py`: inline モードと Celery モード（実ワーカー、API とワーカーで同じ `SECRET_KEY`）の両方で、
  解析 → 歌詞（同じ歌詞の保存し直しで行 ID・タイミング・権利確認が保たれること）→ 生成ガード → mock / library のバッチ →
  編集・再レンダリング → MP4・ZIP のダウンロード（署名付き ZIP リンクを含む）→ ffprobe と元曲との相互相関による検証まで
  （上の「E2E デモ」）。キーなしの OpenRouter が「未設定」と表示され、見積もりの比較に入ることも確認。
- 既存のデータベース（開発用の `mvf`）の起動時の自動更新: `cost_records.project_id` の NULL 可・`ON DELETE SET NULL` 化、
  `cost_records.owner_id` の追加と補完（134 件。2026-10-11）。
- Playwright のブラウザ E2E（本番ビルド `next start` :3000 と inline モードの API :8000、14 件）: 画面操作による一連の流れ、
  library での生成、歌詞スタイル「自動」、Runway 表記（Runway は設定済みと見せかけた応答で確認）、署名付きリンクでの ZIP、
  キャンセル済みの非表示、「左右反転を使う」、権利確認のやり直しの表示、歌詞の見出し行・間奏前の表示終了の注意、
  絵コンテのプレビューの権利の案内と回数制限（Claude は設定済みと見せかけた応答、429 はテスト側で用意）、
  OpenRouter の表示（設定画面・生成設定・絵コンテの経路・利用額。設定済みの状態は応答の書き換えで確認）。
  `review-fixes.spec.ts`（6 件）は 2026-10-11 にトークン認証（`AUTH_MODE=token`）の API でも成功（ZIP は認証ヘッダーなしで取得）。
- フロントエンドの `npm run check`（`tsc --noEmit` と ESLint）/ `npm run build`。
- FFmpeg による 1080×1920 / H.264 / AAC / 30fps の出力、歌詞の焼き込み、品質チェック、Range 対応の署名付き URL。
  ゆっくりした Ken Burns のパン・ズームの連続フレームを目視し、テクスチャ画像で動きを計測（縦長素材のパンはフレーム間の
  移動量の揺れが 0.04〜0.05 px）。暗部の階調（輝度 48 未満に 27〜40 段階）も確認。
- Celery ワーカーの強制終了（kill -9）からの回収（ワーカー再起動時の回収処理で完了まで進むこと）は、以前の修正時に一度確認。

- **OpenRouter の実 API（2026-10-13、`scripts/openrouter_live_check.py`）**:
  - `GET /videos/models`（無料）: 30 モデルを取得し、料金（`pricing_skus`）の読み取りを全モデルで確認（`tests/fixtures/openrouter_videos_models_2026-10-13.json`）。
  - Claude の絵コンテ（`anthropic/claude-opus-5.5`、`zdr: true`・`data_collection: "deny"`・json_schema 指定）: 3 シーンを 18 秒で生成。
    構造化出力はスキーマどおりで、アプリが記録した費用 $0.05564 と OpenRouter の請求額（`GET /key` の増分）が一致。
  - 動画（`google/veo-3.1-lite`、4 秒・9:16・720p・無音）: アカウントで ZDR を強制していたため `POST /videos` が 404 で拒否（課金なし）。
    生成・ダウンロード・`usage.cost` の確認はまだです。

**未検証（キー・接続先・設定の都合で）**

- Veo / Runway / Kling / OpenRouter への実際の生成リクエスト、料金、課金の挙動（失敗したジョブが課金されるか、出力 URL の有効期限など）。
  ジョブ ID の保存と再開（`on_submitted` / `resume`）も HTTP モックでのみ確認しています。
- OpenRouter のモデル一覧（`GET /videos/models`）の実際の内容と料金のキー名、`POST /videos` の成功ステータス（202 か 200 か）、
  `/content` の 409 / 410 の意味、BYOK の費用の内訳。OpenRouter の利用規約と各モデルの商用利用条件。
- Anthropic 直接の Claude（Anthropic SDK を偽の HTTP 応答で呼ぶテストのみ）。OpenRouter 経由の Claude は上のとおり 1 回成功したが、
  拒否（refusal）の表し方と翻訳（`translate_to_english`）は実 API では未確認。
- Whisper による歌詞整列（モデルを取得できないため 503。整列ロジックは偽の文字起こしでのみテスト）。
- 実 S3 / MinIO / R2（moto のみ）。H.264 を再生できる Chrome での動画再生（同梱の Chromium は H.264 非対応のため、
  E2E では VP9 に変換したコピーで再生を確認）。

## 外部 API に送るデータ

- 動画生成 API（Veo / Runway / Kling / OpenRouter）には、英語のプロンプト（世界観・シーンの説明）と、画像→動画のときは入力画像を送ります。
  楽曲・歌詞の本文は送りません。OpenRouter はプロンプトを各モデルの提供元（Google・Kuaishou・MiniMax・Alibaba など）に中継します。
  動画生成は OpenRouter のゼロデータ保持（ZDR）の対象外とされ、動画のリクエストにはデータ保持の指定を付けられません。
- Claude の経路が設定されていると（`ANTHROPIC_API_KEY`、またはそれがなく `OPENROUTER_API_KEY`）、絵コンテの生成に
  **楽曲名・ムード・目的・カスタム世界観・歌詞の本文・楽曲解析の要約** を Claude に送ります。アーティスト名と別表記は
  「描写してはいけない名前」としてのみ送り、他の項目からは取り除きます。送信したくない場合はどちらのキーも設定しないでください
  （テンプレートで絵コンテを作ります）。
- OpenRouter 経由の場合、上の内容（歌詞を含む）は OpenRouter と、その先の Claude の提供元（Anthropic / Amazon Bedrock /
  Google Vertex / Azure など。どれが使われたかは応答から分かりません）を通ります。毎回、データを収集しない提供元
  （`data_collection: "deny"`）・データを保持しないエンドポイント（`zdr: true`）だけを指定しますが、実際の扱いは未検証です。
  条件を満たす提供元がない場合はテンプレートで絵コンテを作ります。OpenRouter のアカウント設定でプロンプトのログ記録はオフのままにしてください。
  OpenRouter に送るユーザー ID は、内部の ID を `SECRET_KEY` で HMAC した仮名です。
  権利確認の「歌詞の権利」が未確認のあいだ、絵コンテのプレビューは歌詞を送らずテンプレートで作ります（バッチの作成は
  権利確認の全項目が必要です）。詳しくは `docs/RIGHTS_AND_SECURITY.md` §5 を参照してください。

## 制限事項・既知の問題

- 実際の AI 動画生成（Veo / Runway / Kling / OpenRouter）と Claude による絵コンテ生成（Anthropic 直接・OpenRouter 経由とも）は、
  上記のとおり未検証です。
- OpenRouter（実キーでの確認が必要な点。詳しくは `docs/VIDEO_PROVIDERS.md` §8.8・§8.10）:
  - モデル一覧の料金（`pricing_skus`）は、2026-10-13 に取得した実際の一覧で見られた SKU の系統だけを読み取ります（セント表記は
    USD に換算、無音・テキスト→動画・要求する解像度の単価を使用）。トークン単位の料金（Seedance）や未知の系統のモデルは、
    推測せずに料金不明として選べなくなります。SKU の意味は名前からの推定で、実際の請求額との突き合わせは未確認です。
    各モデルが 9:16・720p・`generate_audio: false`・画像の data URI を受け付けるかも未確認です。
  - キャンセル API がないため、中断・タイムアウトしたジョブは課金されたものとして記録します（推定）。生成リクエストの結果が
    分からなかった場合（応答のタイムアウト、500 / 502 / 504 / 524 など）も、二重課金を避けて再送せず、推定額を課金済みとして記録します。
  - BYOK（OpenRouter に自分の提供元キーを登録）の場合、OpenRouter が返す費用が手数料だけの可能性があるため、見積もり額
    （Claude はトークン数 × 定価）を下回らないように記録します。実際の請求は提供元のアカウントで確認してください。
  - Claude の経路では、`zdr: true` と `data_collection: "deny"` を満たす Opus 5.5 の提供元があるかが未確認です。ない場合、
    OpenRouter 経由の絵コンテは毎回テンプレートになります（警告が出ます）。
  - 動画のダウンロード先は https の公開ホストに限りますが、DNS で内部アドレスに解決されるホスト名までは防げません。
  - Veo / Runway / Kling の送信は従来どおり、タイムアウトや 5xx で再送されることがあります（OpenRouter だけが「結果不明なら再送しない」）。
  - OpenRouter 経由の Runway のモデルに「Powered by Runway」の表示が必要かは未確認です（OpenRouter のジョブには表示しません）。
- Whisper による歌詞整列は、この環境ではモデルを取得できないため 503 になります。
- 楽曲解析・自動タイミングの精度は、合成したデモ曲でのみ確認しています。実際の楽曲（特に 4/4 拍子以外）では精度が下がります。
  拍の位置は約 30 ms 遅れて検出される傾向があります。
- 見積もり: 生成画面は 1 つのシードで見積もりとバッチ作成を行うため、確認ダイアログの金額は実際の計画と一致します。
  API を直接呼んでシードを省略した場合、`POST /estimate` はシード 0 で計画し、バッチはランダムなシードで実行するため、
  新規生成数が多少ずれることがあります。プロバイダー比較の他モデルの金額は、キャッシュ済み素材があると過大・過小になる
  ことがあります（選択中のモデルの金額は正確です）。
- Claude を使う場合、同じ世界観・楽曲・歌詞の 2 回目以降のバッチは前回の Claude の絵コンテを再利用します
  （「絵コンテを Claude で新しく作り直す」で作り直し）。初回は見積もりの絵コンテ（テンプレート）と実際の絵コンテが異なります。
- 肖像の保護は語句ベースです。1 語だけの人名（look-alike 表現を伴わないもの）や、漢字のアーティスト名のローマ字表記は、
  別表記に登録するか Claude を使わないと除去されません。逆に、辞書にない 2 語の地名（例: Times Square）が人名として除去される
  ことがあります（警告が出ます）。生成された映像に実在の人物が写っていないかは、公開前に人が確認してください。
- 歌詞の見出し行の判定は書式によります。括弧だけの行（`[サビ]`、`【Aメロ】`、`<Chorus>`、`＜間奏＞` など）と、既知のセクション名に
  コロンが付いた行（`Verse 1:`、`Pre-Chorus:`、`サビ：`、`2番:` など）だけを見出しとして除きます（警告に一覧を出します）。
  コロンのない `サビ` や `★2番★` のような行は歌詞として扱うので、不要なら削除してください。`(Oh oh)` のような丸括弧の行は
  コーラスとして残します。
- 終了タグのない LRC の行は次の行まで表示しますが、次の行まで 10 秒以上空く（間奏など）行は、文字数から推定した歌唱時間
  （2.5〜8 秒）で表示を終えます（警告あり）。推定なので実際の歌唱より短い・長いことがあります。終了の時刻タグ（歌詞のない
  `[mm:ss.xx]` の行）か、タイミング表での手動調整で直せます。
- Ken Burns（ズーム／パン）はフレームごとにサブピクセル位置で再サンプリングする（FFmpeg の `perspective`、3 次補間）ため、
  素材がすべて 1080×1920（モックの素材など）のとき、レンダリング時間が修正前より約 6 割長くなります（修正時の計測で
  15 秒の動画 1 本・2 コアあたり 19.5 秒 → 31.4 秒。最終確認時は約 35 秒）。720p や横長の素材が混ざる場合の増加は 2 割程度以下です。
- ディテールの少ない平坦な横長素材をゆっくりパンすると、H.264 のエンコード（CRF 18・veryfast）で動きが小刻みに止まっては進む
  ことがあります（ぼかしたノイズの合成素材で確認。フィルターの出力自体は滑らかで、ほぼ無劣化（CRF 1）で書き出すとフレーム間の揺れが
  0.23 px から 0.08 px に減ります）。
- Claude なしの英訳（簡易辞書）では一部の名詞が落ちることがあります（警告あり）。
- 権利確認は自己申告です。音源をアップロードする（差し替え・初回とも）と「音源の権利」が、歌詞の本文（見出し行を除く行の文字）を
  変えると「歌詞の権利」が、確認済みだった場合に未確認に戻ります（タイミングだけの変更では戻りません）。どの項目が戻ったか
  （「要再確認」の表示）は、変更したブラウザの localStorage にだけ記録します。ほかのブラウザ・端末では通常の「未確認」と表示されます。
- Claude による絵コンテのプレビューの回数制限（ユーザーごとに 10 分間で 10 回）は、API プロセスのメモリで数えます。
  API を再起動すると数え直し、複数のプロセスで動かすとプロセスごとに数えます。
- プロジェクトのタブ・ナビの件数（素材 N・動画一覧 N）には、キャンセルした素材・動画も含まれます。一覧は既定でキャンセル済みを
  表示せず、表示していない本数を添えます（「キャンセル済みを表示」で表示）。
- 素材キャッシュはプロジェクト内でのみ共有します。
- ZIP は無圧縮のストリーミング（データディスクリプタ付き）です。`unzip` と Python の `zipfile` では確認済みですが、
  一部の古い解凍ツールは非対応の可能性があります。
- HDR / 10bit 素材のトーンマッピングには対応していません。
- Alembic マイグレーション、監査ログ、マルチテナント課金はありません。レート制限は Claude による絵コンテのプレビューだけです。

## ロードマップ

- **Phase 2（実生成の本番化）**: Veo / Runway / Kling / OpenRouter（動画と Claude の経路）の実キーでの検証と料金の確定、画像→動画（キャラクター一貫性のための参照画像）、
  Whisper モデルの配布と整列精度の評価、Alembic マイグレーション、S3 互換ストレージでの運用検証、生成ジョブの優先度とレート制限。
- **Phase 3（制作体験）**: タイムライン上でのショット編集・トリミング、歌詞のフレーズ単位アニメーション、複数アスペクト比（4:5・1:1・16:9）同時出力、
  BGM のみ区間の自動演出、テンプレート共有、チーム・権限管理。
- **Phase 4（配信と分析）**: Instagram / TikTok / YouTube Shorts への予約投稿（公式 API）、A/B テスト、視聴データと連動したバリエーション最適化、
  ブランドセーフティ・肖像権の自動チェック、課金・請求管理。
