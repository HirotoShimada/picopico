# AI Music Video Factory

アーティストの楽曲と歌詞から、Instagram ストーリーズ向けの縦型（9:16・1080×1920）ミュージックビデオを
**まとめて何本も**作る Web アプリです。映像素材は AI 動画生成（Veo / Runway / Kling の公式 API）、
アップロード済みの素材、または動作確認用のモック映像から選べます。歌詞は楽曲に合わせてタイミングを取り、
映像に焼き込みます。

- 楽曲解析: BPM・拍・小節頭・セクション（イントロ／A メロ／サビ…）・サビ候補・おすすめ区間（15/30/60 秒）
- 歌詞: TXT / LRC / SRT 入力（Shift_JIS 等も自動判定）、ヒューリスティック自動タイミング、
  Whisper による整列（ローカルにモデルがある場合のみ）、手動修正、LRC / SRT 書き出し
- 生成: 世界観プリセット → 絵コンテ（Claude またはテンプレート）→ 素材プール → 動画ごとに
  カット割り・開始位置・反転・ズーム／パン・カラーグレード・歌詞スタイルを変えたバリエーション
- コスト: 事前見積もり（プロバイダ／モデル比較つき）、予算上限、生成済み素材のキャッシュ再利用
- 仕上げ: 5 種類の歌詞スタイル（simple / cinematic / minimal / karaoke / fade）、ストーリーズのセーフエリア、
  FFmpeg による H.264 / AAC 出力、品質チェック、個別・ZIP ダウンロード、動画ごとの手動編集と再レンダリング

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
   ├── TASK_MODE=inline : API プロセス内のスレッドプール（2 並列）
   └── TASK_MODE=celery : Redis ──► Celery ワーカー（mvf.analyze / mvf.run_batch / mvf.render_video）
                                          │（同じタスク本体: app/pipeline.py）
                                          ▼
   analyze      : services/audio_analysis.py（librosa）
   run_batch    : storyboard.py（Claude / テンプレート）→ variation.py（素材プール・動画プラン）
                  → providers/（mock / library / veo / runway / kling）で素材生成
                    （予算チェック・リトライ・キャンセル・キャッシュ再利用）
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

テーブルは API の起動時に自動作成されます（MVP のためマイグレーションはまだありません）。

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
| `RUNWAY_API_KEY` / `RUNWAY_API_BASE` | – / `https://api.dev.runwayml.com` | Runway |
| `KLING_API_KEY` / `KLING_ACCESS_KEY` / `KLING_SECRET_KEY` / `KLING_API_BASE` | – / – / – / `https://api-singapore.klingai.com` | Kling（AK/SK は旧 API 用） |
| `DEFAULT_BUDGET_USD` | `50` | バッチの既定予算上限 |
| `WHISPER_MODEL` | `small` | faster-whisper のモデル名またはローカルディレクトリ |
| `FONTS_DIR` | – | libass に追加で渡すフォントディレクトリ |

API キーはサーバー側でのみ読み込み、どの API レスポンスにも含めません（`configured` の真偽値のみ）。

## 起動

```bash
# API（inline モード: ワーカー不要）
cd backend && .venv/bin/uvicorn app.main:app --port 8000

# Celery モード（API とワーカーに同じ設定を渡す）
cd backend && TASK_MODE=celery .venv/bin/uvicorn app.main:app --port 8000
cd backend && TASK_MODE=celery .venv/bin/celery -A app.worker.celery_app worker -l info -c 2

# フロントエンド
cd frontend && npm run dev    # http://localhost:3000
```

- inline モードは単一プロセス前提です（`uvicorn --workers` を増やさないでください）。API が途中で落ちた場合、
  再起動時に未完了の解析・バッチ・レンダリングを自動で再開します。
- Celery モードではタスクを `acks_late` で受け取るため、ワーカーが落ちても Redis から再配信されます。
  Redis に接続できないときは API が 503（`queue_unavailable`）を返し、バッチは `failed` になります。
- レンダリングは 1 本（15 秒）あたり CPU 2 コアで約 20〜30 秒です。並列数は inline / Celery とも 2 です。

## テスト

```bash
cd backend
.venv/bin/python -m pytest tests/ -q                       # 全テスト（PostgreSQL の mvf_test を使用）
TEST_DATABASE_URL=sqlite:////tmp/mvf.sqlite3 .venv/bin/python -m pytest tests/ -q   # SQLite で実行
.venv/bin/python -m pytest tests/test_pipeline.py tests/test_api_*.py -q            # 統合部分のみ
```

テストは有料 API を呼びません（API キーは空に上書きされ、外部 API は HTTP モックのみ）。

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
5. 生成ガードを確認する（権利未確認・キー未設定・素材なし → 422）。そのあと権利を確認する
6. デモ素材を 3 本アップロードし、見積もりを取る
7. バッチ A を実行する（mock、3 本 × 15 秒、歌詞スタイルは「auto」で動画ごとに変わる）
8. バッチ B を実行する（アップロード素材、3 本 × 15 秒）
9. 1 本を編集して再レンダリングする（歌詞スタイル変更・歌詞タイミング +0.5 秒・ショットの素材差し替え）
10. 全 MP4 と ZIP をダウンロードする
11. ffprobe で各ファイルを検証する（1080×1920 / H.264 / 30fps / AAC / 15.0 秒）
12. 音声を元曲の該当区間と相互相関で照合する

失敗が 1 つでもあると終了コード 1 になります。

2026-10-11 にこの開発コンテナで実行した結果を示します。inline モードと Celery モード（`celery -A app.worker.celery_app worker -c 2`）の両方で成功しました。以下は Celery モードの出力です。

```
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
| video    | style     | grade       | start | shots | MB   | resolution | vcodec | fps  | acodec | dur(s) | lag(ms) | corr  | quality | result |
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
| A#1      | cinematic | natural     | 24.03 | 5     | 7.9  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| A#2      | simple    | teal_orange | 24.03 | 7     | 7.2  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| A#3      | fade      | warm_film   | 24.03 | 6     | 9.2  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#1      | karaoke   | warm_film   | 24.03 | 7     | 12.9 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#2      | karaoke   | cool_night  | 44.02 | 6     | 9.7  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#3      | karaoke   | natural     | 0.03  | 5     | 13.5 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.999 | pass    | OK     |
| A-edit#1 | karaoke   | natural     | 24.03 | 5     | 12.6 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
timings: analysis 4.1s, batch_a 66.9s, batch_b 57.6s, edit_rerender 20.4s, total 153.5s
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

詳細（H.264 非対応の Chromium での扱い、スクリーンショットの一覧）は `frontend/e2e/README.md` を参照してください。

## 動画生成プロバイダの状況（2026-10-11 時点）

| プロバイダ | 状態 | 備考 |
|---|---|---|
| モック（`mock`） | **動作確認済み** | FFmpeg で合成するダミー映像。左上に「MOCK」と表示。無料 |
| アップロード素材（`library`） | **動作確認済み** | アップロードした動画だけでカット割り。生成なし・無料 |
| Veo（Gemini API） | 未検証（キーなし） | 実装・HTTP モックテスト済み。無効キーで公式エンドポイントの 400 応答のみ確認 |
| Runway | 未検証（キーなし・ホストへ接続不可） | 実装・HTTP モックテスト済み |
| Kling | 未検証（キーなし・ホストへ接続不可） | 実装・HTTP モックテスト済み |

API モデルの料金は公式料金表の調査値（`verified: false`）です。実キーでの検証が済むまでは概算として扱ってください。
Runway を使う場合は、利用規約に従い UI に「Powered by Runway」の表記が必要です。

## 制限事項・未検証事項

- 実際の AI 動画生成（Veo / Runway / Kling）と Claude による絵コンテ生成は、API キーがないため未検証です
  （HTTP モック・SDK モックでのみテスト）。
- Whisper による歌詞整列は、この環境ではモデルを取得できない（huggingface.co に接続不可）ため 503 になります。
  整列ロジックは偽の文字起こしでのみテスト済みです。
- 楽曲解析・自動タイミングの精度は、合成したデモ曲でのみ確認しています。実際の楽曲（特に 4/4 拍子以外）では精度が下がります。
- 実 S3 / MinIO / R2 は未検証です（moto のみ）。
- 見積もり（`POST /estimate`）は乱数シードを固定せずに計画するため、実際のバッチの新規生成数と多少ずれることがあります。
  バッチ作成時の見積もりは、実際に使うシードで計算します。Claude を使う場合は絵コンテが変わるため、さらに差が出ます。
- 素材キャッシュはプロジェクト内でのみ共有します。
- ZIP は無圧縮のストリーミング（データディスクリプタ付き）です。`unzip` と Python の `zipfile` では確認済みですが、
  一部の古い解凍ツールは非対応の可能性があります。
- HDR / 10bit 素材のトーンマッピングには対応していません。
- マイグレーション（Alembic）、レート制限、監査ログ、マルチテナント課金はありません。

## ロードマップ

- **Phase 2（実生成の本番化）**: Veo / Runway / Kling の実キーでの検証と料金の確定、画像→動画（キャラクター一貫性のための参照画像）、
  Whisper モデルの配布と整列精度の評価、Alembic マイグレーション、S3 互換ストレージでの運用検証、生成ジョブの優先度とレート制限。
- **Phase 3（制作体験）**: タイムライン上でのショット編集・トリミング、歌詞のフレーズ単位アニメーション、複数アスペクト比（4:5・1:1・16:9）同時出力、
  BGM のみ区間の自動演出、テンプレート共有、チーム・権限管理。
- **Phase 4（配信と分析）**: Instagram / TikTok / YouTube Shorts への予約投稿（公式 API）、A/B テスト、視聴データと連動したバリエーション最適化、
  ブランドセーフティ・肖像権の自動チェック、課金・請求管理。
