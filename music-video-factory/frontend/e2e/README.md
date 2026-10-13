# ブラウザ E2E テスト（Playwright）

本番ビルドのフロントエンドと実際のバックエンドを、ブラウザから操作して通しで確認します。
モックは使いません（例外は下記「H.264 を再生できない Chromium」「Runway の帰属表示」「Claude の絵コンテのプレビュー」「OpenRouter」のみ）。

| テスト | 内容 |
|---|---|
| `full journey: mock provider …` | ダッシュボード → プロジェクト作成（アーティスト名の別表記つき）＋`demo_song.mp3` アップロード → 解析完了・波形表示・おすすめ 15 秒区間の保存・「感情・世界観の推定」カード（API で入力のムード「切ない」が反映されていること）→ 歌詞の貼り付け・自動タイミング・1 行を +0.1 秒・保存 → 素材 3 本アップロード → 権利確認 5 項目 → 生成設定（推定のおすすめ表示、プロジェクトで選んだ世界観が選ばれたままであること、モック／15 秒／3 本、見積もり表示）→ 生成ジョブ画面で完了まで待機 →「完成分を ZIP で保存」（署名付き `/media-zip/…` リンクをブラウザが直接ダウンロードし、ZIP に 3 本入っていること）→ 動画一覧（3 本）→ プレビューの `<video>` が約 15 秒のメタデータを読み込む → MP4 をダウンロードして ffprobe で 1080×1920・H.264・AAC を確認 → エディターで歌詞スタイル変更・1 行の微調整・ショット差し替え → 再レンダリング（`render_version` 2）→ 設定画面 |
| `library provider: …` | API で準備したプロジェクトに対し、生成設定で「アップロード素材」・15 秒・3 本・歌詞スタイル「自動（動画ごとに変える）」を選んで生成し、3 本の動画がアップロード素材だけで構成されていること、ジョブ画面の生成設定が「自動」と表示され動画ごとに歌詞スタイルが異なること（`plan.lyric_style` が 2 種類以上）、生成設定を開き直すと「自動」が選ばれていることを確認 → 「Powered by Runway」の表示（設定画面・プロバイダー選択・ジョブ画面・動画一覧・プレビュー、runwayml.com へのリンク）を確認 |
| `openrouter.spec.ts`（6 テスト） | OpenRouter（動画生成プロバイダーと Claude の経路）の表示。① キーなしの実際の応答で、設定画面の OpenRouter が「未設定」・組み込みの候補（モデル数と `models_note`）、Claude の経路が「テンプレート」と送信データの説明 ② OpenRouter 設定済み（取得したモデル一覧・料金を読み取れないモデル 1 件・Claude は OpenRouter 経由）の応答で、経路「OpenRouter 経由」・モデル・「未検証」・`data_policy_note`、モデル一覧の出どころ・「取得 35分前」・「料金未取得 1・選択不可」、料金表の「料金未取得」。ANTHROPIC_API_KEY もあるときは「Anthropic 直接」 ③ 生成設定の OpenRouter カード（「未検証」）、モデルの選択肢の料金と提供元、料金未取得のモデルが選べないこと、「OpenRouter での生成について」（音声オフ・OpenRouter 経由で提供元に送信・提供元の規約）、モデル変更で見積もり（実際のバックエンド）に `provider: "openrouter"` とモデルが送られ単価と「未検証」が表示されること、生成開始の確認ダイアログの説明、バッチ作成の要求がバックエンドで拒否される（キーなし: `OPENROUTER_API_KEY`）ことの表示 ④ 前回のジョブが OpenRouter（モデル一覧から消えたモデル）なら OpenRouter が選ばれたまま既定のモデルを表示し、見積もりに `model: null` を送ること ⑤ 絵コンテのプレビューで「Claude（OpenRouter 経由）」とモデル・LLM 費用、OpenRouter 呼び出し後のテンプレートへの切り替えの表示 ⑥ ダッシュボードの利用額（「OpenRouter」「Claude（Anthropic 直接）」と注記）、ジョブ画面の生成エンジン・絵コンテの経路、OpenRouter で生成した素材のプレビュー（モデルの提供元） |
| `review-fixes.spec.ts`（6 テスト） | API で準備したプロジェクト（アップロード素材のジョブ 2 本、生成中にキャンセルしたモックのジョブ）に対して: ① ZIP（選択した動画・ジョブの完成分）が `POST …/zip-link` の署名付きリンク経由でダウンロードされ（`download.url()` が `/media-zip/…`、旧 `…/zip` エンドポイントは呼ばない）、中身が正しいこと ② 動画一覧と素材タブでキャンセル済みが既定で非表示・「キャンセル済みを表示」で表示されること、非表示の本数、素材の「N本で使用」がキャンセルした動画を数えないこと ③ 生成設定の「左右反転を使う」が既定オフで、オンにすると見積もりとバッチ作成に `allow_mirror: true` が送られ、ジョブ画面に表示され、次回の生成設定に引き継がれること ④ 音源の差し替え・歌詞本文の変更（貼り付け／タイミング表での表記の編集）で、変更した場所の「権利確認をやり直してください」、権利確認タブの「要再確認」、見出しの「権利の再確認が必要」が表示され、再確認すると消えること（再読み込み後も残ること）⑤ 歌詞の見出し行の除外・LRC の間奏前の表示終了の推定が「読み込み時の注意」に表示され、権利確認のやり直しはそれと別に表示されること ⑥ 絵コンテのプレビューで、歌詞の権利が未確認ならテンプレートで作る旨を表示すること、Claude のプレビュー回数の上限（429）を専用のメッセージで表示すること（Claude キーがないため、`claude_configured` と 429 応答はテスト側で用意） |
| `no-repeat.spec.ts`（2 テスト） | 1本の動画の中で同じ映像を繰り返さない構成（`docs/ARCHITECTURE.md` §6.4・§6.5）を、費用が発生する前に表示すること。バッチは開始しません。① モック: 上限なしの 3 本・1 本で「動画内の同じ素材」が「なし」、「1本あたりのショット」「ショットの長さ・速度」の行が見積もり（実際のバックエンド）と一致し、「動画間の映像バリエーション」のバーは 2 本以上のときだけ表示されること → 新規生成の上限 1 本で、再登場するショット数・補足（素材 1 本のため「連続しない」「別の区間」を含まない）・`repeat_warning`（1 回だけ表示）・準備状況の警告が表示され、生成開始は押せること ② OpenRouter（`GET /api/system/capabilities` だけを書き換えて設定済みにする。見積もりは実際のバックエンド）: 上限なしの見積もりより少し低い予算で `budget_fit` の注意と「上限を{k}本にする」が表示され、押すと上限が k になり、再計算中は古い提案のボタンを押せず、次の見積もりが提案と一致すること → 確認ダイアログの「1本あたりNショット（最長L秒・…）」と「動画内で同じ素材は繰り返しません。」/ 再登場の警告 → 上限 1 本で確認ダイアログが再登場を警告すること |

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

所要時間は 4 CPU で約 8 分です（`full-flow` のレンダリング 7 本、`review-fixes` と `openrouter` がそれぞれ約 1 分）。
この時間は `no-repeat.spec.ts` を追加する前の計測です（`no-repeat.spec.ts` はレンダリングせず、見積もりだけを待ちます）。テストはプロジェクトを新規作成するので、開発用 DB にデータが残ります。

`review-fixes.spec.ts` はトークン認証（`AUTH_MODE=token`）のバックエンドでも実行できます（ZIP のダウンロードが
トークン認証でもブラウザの通常のダウンロードになることの確認）。`full-flow.spec.ts` は dev モード専用です。

```bash
# バックエンドを AUTH_MODE=token API_TOKENS=e2e:<トークン> で起動してから
E2E_API_TOKEN=<トークン> npx playwright test e2e/review-fixes.spec.ts
```

### 環境変数

- `E2E_BASE_URL`（既定 `http://localhost:3000`）、`E2E_API_URL`（既定 `http://localhost:8000`）
- `E2E_API_TOKEN`: トークン認証のバックエンドで `review-fixes.spec.ts` を実行するときのトークン（ページには `/settings` と同じく
  `localStorage` で渡します）
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

## Claude の絵コンテのプレビュー

この環境には Anthropic の API キーがないため、`review-fixes.spec.ts` の最後のテストだけ、`GET /api/system/capabilities` の
`claude_configured` を `true` に書き換え、回数制限のメッセージを確認する 1 回の `POST /api/projects/{id}/storyboard` に
バックエンドと同じ形の 429 応答（`{"detail": …, "code": "rate_limited"}`）を返します。

## OpenRouter

この環境には OPENROUTER_API_KEY がなく、openrouter.ai にも接続できないため、`openrouter.spec.ts` の「設定済み」の画面は
バックエンドの実際の応答を書き換えて確認します（`patchApiJson`）。`GET /api/system/capabilities` の OpenRouter を設定済み・
モデル一覧を取得済み（`models_source: "live"`、35 分前の `models_fetched_at`、料金を読み取れないモデル 1 件を追加）にし、
`llm` を OpenRouter 経由（または Anthropic 直接）にします。絵コンテのプレビューは実際の応答（テンプレート）の `source` /
`route` / `model` / `cost_usd` を、ダッシュボードは `usage.by_provider` を、ジョブ・素材は `provider` / `source` / `storyboard`
を書き換えます。見積もりとバッチ作成の要求は書き換えずに実際のバックエンドへ送ります（バッチはキーがないため 422 で拒否されます）。
キーなしの設定画面（テスト ①）は書き換えなしです。OpenRouter との実際の通信は、このテストでは一切確認していません。

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
| `18-gallery-cancelled-hidden` / `19-materials-cancelled-hidden` | キャンセル済みを非表示にした動画一覧 / 素材タブ |
| `20-generate-mirror` | 生成設定（「左右反転を使う」オン） |
| `21-rights-reset-song` / `22-rights-reset-lyrics` / `23-rights-reconfirm` | 音源の差し替え・歌詞の変更後の「権利確認をやり直してください」/ 権利確認タブの「要再確認」 |
| `24-lyrics-heading-note` | 見出し行を除外したときの「読み込み時の注意」 |
| `25-storyboard-rights-note` / `26-storyboard-rate-limit` | 絵コンテのプレビュー: 歌詞の権利が未確認のときの案内 / Claude のプレビュー回数の上限 |
| `27-settings-openrouter-unconfigured` / `28-settings-openrouter-configured` | 設定: キーなし（OpenRouter は組み込みの候補、Claude はテンプレート）/ OpenRouter 設定済み（Claude は OpenRouter 経由） |
| `29-generate-openrouter-models` / `30-generate-openrouter` | 生成設定: OpenRouter のモデルの選択肢（料金未取得を含む、画面内のみ）/ OpenRouter 選択時の説明と見積もり |
| `31-storyboard-openrouter` | 絵コンテのプレビュー: Claude（OpenRouter 経由）とモデル |
| `32-dashboard-usage-openrouter` / `33-clip-openrouter` / `34-batch-openrouter` | 利用額（OpenRouter / Claude 直接）/ OpenRouter の素材（画面内のみ）/ ジョブ画面の生成エンジンと絵コンテの経路 |
| `35-generate-no-repeat` | 生成設定（モック・1 本・上限なし）: 「1本あたりのショット」「ショットの長さ・速度」「動画内の同じ素材」なし |
| `36-generate-repeat-warning` | 生成設定（新規生成の上限 1 本）: 再登場するショット数と補足、`repeat_warning`、準備状況の警告 |
| `37-generate-budget-fit` | 生成設定（OpenRouter・予算超過）: `budget_fit` の注意と「上限を{k}本にする」 |

## 補足

- この環境ではロケール（`LANG`）が未設定のため、Chromium が日本語のダウンロードファイル名を `download` に置き換えます。
  `playwright.config.ts` でブラウザに `LANG=C.UTF-8` を渡しています。
- 失敗時のトレースとスクリーンショットは `test-results/`、HTML レポートは `playwright-report/` に出力されます（どちらも git 管理外）。
