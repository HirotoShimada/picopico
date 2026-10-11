# AI Music Video Factory

アーティストの楽曲と歌詞から、Instagram ストーリーズ向けの縦型（9:16・1080×1920）ミュージックビデオを
**まとめて何本も**作る Web アプリです。映像素材は AI 動画生成（Veo / Runway / Kling の公式 API）、
アップロード済みの素材、または動作確認用のモック映像から選べます。歌詞は楽曲に合わせてタイミングを取り、
映像に焼き込みます。

- 楽曲解析: BPM・拍・小節頭・セクション（イントロ／A メロ／サビ…）・サビ候補・おすすめ区間（15/30/60 秒）
- 感情・世界観推定: 音源のムード・歌詞・入力したムードと目的から感情とおすすめの世界観を推定（外部 API なし）。
  初回の生成画面ではおすすめの世界観を初期値にする（プロジェクトで世界観を明示した場合はそちらを優先）
- 歌詞: TXT / LRC / SRT 入力（Shift_JIS 等も自動判定）、ヒューリスティック自動タイミング、
  Whisper による整列（ローカルにモデルがある場合のみ）、手動修正、LRC / SRT 書き出し。
  同じ時刻の行（対訳・デュエットの LRC）は上下に並べて表示
- 生成: 世界観プリセット → 絵コンテ（Claude またはテンプレート）→ 素材プール → 動画ごとに
  カット割り・開始位置・反転・ズーム／パン・カラーグレード・歌詞スタイル（「自動」で動画ごとに変える）を変えたバリエーション
- 肖像の保護: アーティスト名・別表記（ローマ字など）・「〜似」「lookalike」などの表現と、自由入力に書かれた
  人名らしき語をプロンプトから除去し、全プロンプトに「架空の人物のみ」の指定を付ける
- コスト: 事前見積もり（プロバイダ／モデル比較つき）、予算上限（試行ごとに DB で予約し、上限を超えない）、
  生成済み・生成中の素材の再利用、課金済みの失敗も記録、停止したジョブの予算引き上げと再開
- 仕上げ: 5 種類の歌詞スタイル（simple / cinematic / minimal / karaoke / fade）、ストーリーズのセーフエリア、
  FFmpeg による H.264 / AAC 出力、品質チェック、個別・ZIP ダウンロード、動画ごとの手動編集と再レンダリング
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
   run_batch    : storyboard.py（Claude / テンプレート）→ variation.py（素材プール・動画プラン）
                  → providers/（mock / library / veo / runway / kling）で素材生成
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

テーブルは API の起動時に自動作成されます。マイグレーション（Alembic）はまだありません。既存のデータベースに
後から追加された **NULL 可の列**（例: `projects.artist_aliases`）は、起動時に `ALTER TABLE … ADD COLUMN` で自動的に
追加します（`db.add_missing_columns`）。それ以外のスキーマ変更は手動での対応が必要です。

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
- レンダリングは 1 本（15 秒）あたり CPU 2 コアで約 20〜30 秒です。inline のレンダリング並列数は 2、
  Celery では `-c` で指定したワーカー数です。

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

2026-10-11 にこの開発コンテナで、レビュー指摘の修正後に実行した結果を示します。inline モードと Celery モード
（`celery -A app.worker.celery_app worker -c 2`、API とワーカーに同じ `SECRET_KEY`）の両方で成功しました（inline: 合計 173.6 秒）。
以下は Celery モードの出力です。

```
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
| video    | style     | grade       | start | shots | MB   | resolution | vcodec | fps  | acodec | dur(s) | lag(ms) | corr  | quality | result |
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
| A#1      | cinematic | cool_night  | 24.03 | 7     | 7.2  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| A#2      | simple    | natural     | 24.03 | 7     | 8.1  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| A#3      | fade      | teal_orange | 24.03 | 6     | 7.2  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#1      | karaoke   | teal_orange | 24.03 | 7     | 10.5 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#2      | karaoke   | warm_film   | 22.04 | 5     | 12.2 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
| B#3      | karaoke   | cool_night  | 8.03  | 6     | 12.2 | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.999 | pass    | OK     |
| A-edit#1 | karaoke   | cool_night  | 24.03 | 7     | 9.4  | 1080x1920  | h264   | 30/1 | aac    | 15.000 | 0.0     | 0.998 | pass    | OK     |
+----------+-----------+-------------+-------+-------+------+------------+--------+------+--------+--------+---------+-------+---------+--------+
timings: analysis 6.5s, batch_a 69.9s, batch_b 63.6s, edit_rerender 26.4s, total 171.5s
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
Runway の利用規約に従い、UI には runwayml.com へのリンク付きで「Powered by Runway」を表示します（Runway の
プロバイダーカード、Runway 選択時の詳細、Runway を使ったジョブの設定欄・動画一覧・プレビュー、Runway の素材、
設定画面のプロバイダー一覧）。表示が必須かどうかの最終確認は `docs/VIDEO_PROVIDERS.md` §6 の未確定事項です。

## 検証済み / 未検証

この開発コンテナ（4 CPU、API キーなし、huggingface.co・api.klingai.com・api.dev.runwayml.com へ接続不可）で
2026-10-11 に確認した範囲です。

**検証済み（実際に動かして確認）**

- バックエンドの全テスト（PostgreSQL 16 の `mvf_test`）。外部 API は HTTP モック（respx）と SDK モックのみ。
- `scripts/e2e_demo.py`: inline モードと Celery モード（実ワーカー、API とワーカーで同じ `SECRET_KEY`）の両方で、
  解析 → 歌詞 → 生成ガード → mock / library のバッチ → 編集・再レンダリング → MP4・ZIP のダウンロード → ffprobe と
  元曲との相互相関による検証まで（下の「E2E デモ」）。
- Playwright のブラウザ E2E（本番ビルド `next start` と inline モードの API）: 画面操作による一連の流れ、
  library での生成、歌詞スタイル「自動」、Runway 表記（Runway は設定済みと見せかけた応答で確認）。
- フロントエンドの `npm run typecheck` / `npm run lint` / `npm run build`。
- FFmpeg による 1080×1920 / H.264 / AAC / 30fps の出力、歌詞の焼き込み、品質チェック、Range 対応の署名付き URL。
- Celery ワーカーの強制終了（kill -9）からの回収（ワーカー再起動時の回収処理で完了まで進むこと）は、修正時に一度確認。

**未検証（キー・接続先がないため）**

- Veo / Runway / Kling への実際の生成リクエスト、料金、課金の挙動（失敗したジョブが課金されるか、出力 URL の有効期限など）。
  ジョブ ID の保存と再開（`on_submitted` / `resume`）も HTTP モックでのみ確認しています。
- Claude による絵コンテ生成・翻訳（Anthropic SDK を偽の HTTP 応答で呼ぶテストのみ）。
- Whisper による歌詞整列（モデルを取得できないため 503。整列ロジックは偽の文字起こしでのみテスト）。
- 実 S3 / MinIO / R2（moto のみ）。H.264 を再生できる Chrome での動画再生（同梱の Chromium は H.264 非対応のため、
  E2E では VP9 に変換したコピーで再生を確認）。

## 外部 API に送るデータ

- 動画生成 API（Veo / Runway / Kling）には、英語のプロンプト（世界観・シーンの説明）と、画像→動画のときは入力画像を送ります。
  楽曲・歌詞の本文は送りません。
- `ANTHROPIC_API_KEY` を設定すると、絵コンテの生成に **楽曲名・ムード・目的・カスタム世界観・歌詞の本文・楽曲解析の要約**
  を Anthropic（Claude）に送ります。アーティスト名と別表記は「描写してはいけない名前」としてのみ送り、
  他の項目からは取り除きます。送信したくない場合はキーを設定しないでください（テンプレートで絵コンテを作ります）。
  詳しくは `docs/RIGHTS_AND_SECURITY.md` §5 を参照してください。

## 制限事項・既知の問題

- 実際の AI 動画生成（Veo / Runway / Kling）と Claude による絵コンテ生成は、上記のとおり未検証です。
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
- 映像・音声の細部: TXT の見出し行（`[サビ]` など）にも歌唱時間が割り当てられ次の行が約 1 秒遅れる、終了タグのない LRC の行は
  間奏中も表示され続ける（空の時刻タグで終了できます）、音圧の非常に高い音源では AAC 出力でわずかにクリップする、
  一部のカラーグレードは暗部がつぶれやすい、Ken Burns のパンは 1 ピクセル単位で動く、Claude なしの英訳（簡易辞書）では
  一部の名詞が落ちる（警告あり）、左右反転で素材内の文字が鏡文字になる、素材の再利用率の警告は原因に関係なく同じ助言を出す。
- 歌詞を保存し直すと行 ID が振り直されるため、行を追加・削除すると、動画ごとに調整した歌詞タイミングが別の行に付くことがあります。
- 音源や歌詞を差し替えても権利確認はリセットされません（チェックリストは自己申告です）。
- プロジェクトを削除すると、その費用記録も削除されます（ダッシュボードの利用額の合計が減ります）。
- キャンセルした動画・素材も一覧に表示され、素材の「使用本数」に数えられます。
- 選択した動画の ZIP（およびトークン認証時のジョブ ZIP）は、ブラウザのメモリに読み込んでから保存します（進捗表示・中断なし）。
- 素材キャッシュはプロジェクト内でのみ共有します。
- ZIP は無圧縮のストリーミング（データディスクリプタ付き）です。`unzip` と Python の `zipfile` では確認済みですが、
  一部の古い解凍ツールは非対応の可能性があります。
- HDR / 10bit 素材のトーンマッピングには対応していません。
- Alembic マイグレーション、レート制限、監査ログ、マルチテナント課金はありません。

## ロードマップ

- **Phase 2（実生成の本番化）**: Veo / Runway / Kling の実キーでの検証と料金の確定、画像→動画（キャラクター一貫性のための参照画像）、
  Whisper モデルの配布と整列精度の評価、Alembic マイグレーション、S3 互換ストレージでの運用検証、生成ジョブの優先度とレート制限。
- **Phase 3（制作体験）**: タイムライン上でのショット編集・トリミング、歌詞のフレーズ単位アニメーション、複数アスペクト比（4:5・1:1・16:9）同時出力、
  BGM のみ区間の自動演出、テンプレート共有、チーム・権限管理。
- **Phase 4（配信と分析）**: Instagram / TikTok / YouTube Shorts への予約投稿（公式 API）、A/B テスト、視聴データと連動したバリエーション最適化、
  ブランドセーフティ・肖像権の自動チェック、課金・請求管理。
