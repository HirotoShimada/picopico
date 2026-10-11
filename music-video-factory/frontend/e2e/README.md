# ブラウザ E2E テスト（Playwright）

本番ビルドのフロントエンドと実際のバックエンドを、ブラウザから操作して通しで確認します。
モックは使いません（例外は下記「H.264 を再生できない Chromium」と「Runway の帰属表示」のみ）。

| テスト | 内容 |
|---|---|
| `full journey: mock provider …` | ダッシュボード → プロジェクト作成（アーティスト名の別表記つき）＋`demo_song.mp3` アップロード → 解析完了・波形表示・おすすめ 15 秒区間の保存・「感情・世界観の推定」カード（API で入力のムード「切ない」が反映されていること）→ 歌詞の貼り付け・自動タイミング・1 行を +0.1 秒・保存 → 素材 3 本アップロード → 権利確認 5 項目 → 生成設定（推定のおすすめ表示、プロジェクトで選んだ世界観が選ばれたままであること、モック／15 秒／3 本、見積もり表示）→ 生成ジョブ画面で完了まで待機 → 動画一覧（3 本）→ プレビューの `<video>` が約 15 秒のメタデータを読み込む → MP4 をダウンロードして ffprobe で 1080×1920・H.264・AAC を確認 → エディターで歌詞スタイル変更・1 行の微調整・ショット差し替え → 再レンダリング（`render_version` 2）→ 設定画面 |
| `library provider: …` | API で準備したプロジェクトに対し、生成設定で「アップロード素材」・15 秒・3 本・歌詞スタイル「自動（動画ごとに変える）」を選んで生成し、3 本の動画がアップロード素材だけで構成されていること、ジョブ画面の生成設定が「自動」と表示され動画ごとに歌詞スタイルが異なること（`plan.lyric_style` が 2 種類以上）、生成設定を開き直すと「自動」が選ばれていることを確認 → 「Powered by Runway」の表示（設定画面・プロバイダー選択・ジョブ画面・動画一覧・プレビュー、runwayml.com へのリンク）を確認 |

## 実行方法

```bash
# 1. サービスとデモ素材
pg_ctlcluster 16 main start; redis-server --daemonize yes --port 6379
backend/.venv/bin/python backend/scripts/make_demo_assets.py

# 2. フロントエンドをビルド
cd frontend && npm run build

# 3. 実行（:8000 / :3000 で起動済みならそれを使い、なければ playwright.config.ts の webServer が起動します）
npx playwright test
```

所要時間は 4 CPU で約 5 分です（レンダリング 7 本）。テストはプロジェクトを新規作成するので、開発用 DB にデータが残ります。

### 環境変数

- `E2E_BASE_URL`（既定 `http://localhost:3000`）、`E2E_API_URL`（既定 `http://localhost:8000`）
- `PW_CHROMIUM_PATH`（既定 `/opt/pw-browsers/chromium`）: 使用するブラウザの実行ファイル

## H.264 を再生できない Chromium

`/opt/pw-browsers/chromium` はオープンソース版の Chromium で、H.264/AAC のデコーダーを含みません
（`canPlayType('video/mp4; codecs="avc1…"')` が空文字）。そのため、このブラウザではバックエンドが出力する
MP4 を `<video>` で再生できません。実際のユーザーが使う Chrome / Edge / Safari / Firefox では再生できます。

テストはブラウザの対応状況を調べ、未対応のときだけ、署名付き `/media/…mp4` へのリクエストをテスト側で受け取り、
バックエンドから取得した実際の MP4 を ffprobe で確認したうえで同じ長さの VP9/Opus WebM に変換してから渡します
（テスト結果に `codec-workaround` の注記が付きます）。ダウンロードした MP4 の検証は常に元のファイルで行います。

本物の MP4 再生まで確認するには、H.264 に対応した Chrome を指定します。例: Chrome for Testing

```bash
curl -sSLO https://storage.googleapis.com/chrome-for-testing-public/141.0.7390.54/linux64/chrome-linux64.zip
unzip -q chrome-linux64.zip -d /tmp/cft
PW_CHROMIUM_PATH=/tmp/cft/chrome-linux64/chrome npx playwright test
```

## Runway の帰属表示

この環境には Runway の API キーがないため、`library provider` テストの最後の手順だけ、バックエンドの実際の応答のうち
`GET /api/system/capabilities` の Runway の `configured` / `available` と、そのテストのバッチの `settings.provider`
を `runway` に書き換えてから画面に渡します（`support.ts` の `patchApiJson`）。設定画面の確認は書き換えなしで行います。

## スクリーンショット

各画面のフルページのスクリーンショットを `e2e/screenshots/` に保存します（1440×900、トースト通知は非表示）。

| ファイル | 画面 |
|---|---|
| `01-dashboard` / `15-dashboard-after` | ダッシュボード（開始時 / 終了時） |
| `02-new-project` | プロジェクト作成 |
| `03`〜`06-workspace-*` | プロジェクト: 楽曲解析 / 歌詞 / 素材 / 権利確認 |
| `07-generate` / `16-generate-library` | 生成設定（モック / アップロード素材） |
| `08-batch-progress` / `09-batch-complete` / `17-batch-library-complete` | 生成ジョブ（進行中 / 完了） |
| `10-gallery` / `11-preview-dialog` | 動画一覧 / プレビュー |
| `12-editor` / `13-editor-rerendered` | エディター（変更 3 件 / 再レンダリング後 v2） |
| `14-settings` | 設定 |

## 補足

- この環境ではロケール（`LANG`）が未設定のため、Chromium が日本語のダウンロードファイル名を `download` に置き換えます。
  `playwright.config.ts` でブラウザに `LANG=C.UTF-8` を渡しています。
- 失敗時のトレースとスクリーンショットは `test-results/`、HTML レポートは `playwright-report/` に出力されます（どちらも git 管理外）。
