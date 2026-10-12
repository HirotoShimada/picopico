# AI Music Video Factory — Frontend

Next.js 16.4（App Router / React 19.3 / Tailwind CSS v4 / Turbopack）製の管理画面です。
ブラウザから FastAPI バックエンド（`docs/ARCHITECTURE.md` §5）へ直接アクセスします。

## セットアップ

```bash
cd frontend
npm install
cp .env.example .env.local   # 必要に応じて NEXT_PUBLIC_API_BASE_URL を変更
npm run dev                  # http://localhost:3000
```

バックエンド: `cd backend && .venv/bin/uvicorn app.main:app --port 8000`
（バックエンドの `CORS_ORIGINS` にフロントエンドのオリジンを含めてください）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000` | ブラウザから見たバックエンドの URL。**ビルド時に埋め込まれます** |

`AUTH_MODE=token` のバックエンドに接続する場合は、`/settings` で API トークンを保存します
（localStorage に保存され、`Authorization: Bearer` ヘッダーで送信）。

## スクリプト

| コマンド | 内容 |
|---|---|
| `npm run dev` | 開発サーバー |
| `npm run build` / `npm start` | 本番ビルド / 起動 |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | ESLint（next/core-web-vitals + TypeScript） |
| `npm run check` | typecheck + lint |

## 構成

```
src/
  app/
    layout.tsx            ルートレイアウト（フォント、Providers、AppShell）
    globals.css           デザイントークン（Tailwind v4 @theme）
    page.tsx              / ダッシュボード
    projects/new/         /projects/new プロジェクト作成（作成 → 音源アップロード → 遷移）
    settings/             /settings 接続・トークン・プロバイダー・料金表
  components/
    ui/                   UI キット（shadcn/ui 風、radix-ui ベース）
    layout/               AppShell, Sidebar, TopBar, PageHeader, ProjectHeader, ProjectNav, Providers
    dashboard/            ダッシュボードの各パネル
  hooks/                  SWR フック（ポーリング込み）
  lib/
    api.ts                型付き API クライアント（§5 の全エンドポイント）
    types.ts              API 型（backend/app/schemas.py と対応）
    format.ts             表示用フォーマッター（ja-JP）
    status.ts             ジョブ／動画／クリップ／解析ステータスのラベルと判定
    constants.ts          権利確認項目、世界観プリセット（フォールバック）、既定値、プロバイダー名
    llm.ts                Claude の経路（Anthropic 直接 / OpenRouter 経由 / テンプレート）と OpenRouter のモデル一覧の表示用ヘルパー
    utils.ts              cn() など
```

### API クライアント（`src/lib/api.ts`）

- エンドポイントごとの関数: `listProjects`, `createProject`, `uploadAudio(id, file, { onProgress, signal })`,
  `getAnalysis`, `getWorldEstimate`, `updateSegment`, `updateLyrics`, `autoTimeLyrics`, `uploadClips`, `estimateBatch`,
  `createBatch`, `getBatch`, `cancelBatch`, `updateBatchBudget`（`PATCH /api/batches/{id}`）, `downloadBatchZip`,
  `downloadVideosZip`, `listVideos`, `editVideo`, `downloadVideo` など。
- ZIP（ジョブの完成分・選択した動画）は `POST /api/batches/{id}/zip-link` / `POST /api/videos/zip-link` で署名付きリンク
  （`/media-zip/…`、認証ヘッダー不要）を受け取り、ブラウザがそのリンクを直接ダウンロードします。トークン認証でも
  ページのメモリに読み込まず、ブラウザの通常のダウンロード（進捗表示・中断）になります。ファイル名はサーバーが決めます。
- 一覧のフィルター: `listVideos(id, { include_cancelled: false })`、`listClips(id, { include_cancelled: false })` で
  キャンセル済みを除きます。キーが変わるため、一覧の再取得は `mutate(isListKey(apiPaths.clips(id)))` を使います。
- `apiPaths` は SWR のキーを兼ねます（例: `mutate(apiPaths.project(id))`）。
- エラーはすべて `ApiError`（`status`, `code`, `detail`, 日本語の `message`）。バックエンドに到達できない場合は
  `status === 0`（`isBackendUnreachable(error)`）。422 の検証エラーは項目名つきの日本語に変換されます。
- アップロードは `XMLHttpRequest` で進捗を通知します（`fetch` にはアップロード進捗がないため）。

### SWR フック（`src/hooks`）

`useProject`, `useProjects`, `useAnalysis`, `useLyrics`, `useClips`, `useBatch`, `useProjectBatches`,
`useVideos`, `useVideo`, `useWorldEstimate`, `useDashboard`, `useCapabilities`, `usePresets`, `useLimits`, `useHealth`,
`useApiToken`, `useNow`, `useHydrated`。処理中（解析中・生成中・レンダリング中）のみ自動ポーリングします。
API トークンを変更すると、SWR のキャッシュをすべて破棄してから取得し直します（前のユーザーのデータを残さない）。

### 画面ごとの補足（実装済み）

- プロジェクト作成・プロジェクト情報: アーティスト名の別表記（`artist_aliases`、カンマ区切り）。生成プロンプトから除外されます。
- 楽曲解析タブ: 「感情・世界観の推定」カード（`GET /api/projects/{id}/world-estimate`）。
- 歌詞タブ: プレビューは同じ時刻（0.05 秒以内）に始まる行を上下に並べて表示し（カラオケのスイープは 1 行目のみ）、
  背景ボックスのスタイルでは影を描かず縁取りを残します（レンダリング結果に合わせた近似表示）。
  自動タイミングで何も変わらなかった場合は保存も「元に戻す」履歴の追加もしません。
- 生成設定: 推定のおすすめ世界観（初回でプロジェクトの世界観が既定のままなら初期値に使用）、歌詞スタイル「自動」、
  Claude 設定時の「絵コンテを Claude で新しく作り直す」（`new_storyboard`、次回には引き継がない）。見積もりとバッチ作成には
  フォームごとに 1 つのランダムなシードを送るため、確認ダイアログの金額は実際の計画と一致します。
  「左右反転を使う」（`allow_mirror`、既定オフ。素材内の文字が鏡文字になる旨を表示）は見積もりとバッチ作成に送り、
  前回のジョブの設定として引き継ぎます。ジョブ画面の生成設定にも表示します。
- 絵コンテのプレビュー: Claude 設定時、歌詞があり「歌詞の権利」が未確認なら、歌詞を送らずテンプレートで作ることを事前に表示
  （権利確認タブへのリンク）。Claude のプレビュー回数の上限（429 `rate_limited`）は専用のメッセージで表示します
  （生成開始には影響しない旨を添える）。
- 権利確認のやり直し: 音源の差し替え（`ProjectOut.warnings`）や歌詞本文の変更（`LyricsParseOut.warnings` /
  `LyricLinesOut.warnings`）でバックエンドが「音源の権利」「歌詞の権利」を未確認に戻すと、変更した場所（楽曲ファイルの
  カード・歌詞タブ）に「権利確認をやり直してください」を表示し、権利確認タブに「要再確認」バッジ、該当項目に「要再確認」、
  プロジェクトの見出しに「権利の再確認が必要」を出します。API はこの情報を変更時の応答でしか返さないため、ブラウザが
  プロジェクトごとに `localStorage`（`mvf.rightsReset`）へ記録し、その項目が再び確認されるまで表示します
  （`components/rights/rights-reset.ts`）。
- 歌詞の読み込み: パーサーの注意（除外した見出し行、間奏前で表示終了を推定した LRC の行、文字コードなど）は
  「読み込み時の注意」に、権利確認のやり直しはそれとは別に表示します。
- 動画一覧・素材: キャンセル済みの動画・素材は既定で非表示（`include_cancelled=false`）。「キャンセル済みを表示」で
  表示します（動画一覧は `?cancelled=1`）。非表示の本数を添えます。素材の「N本で使用」はキャンセルした動画を数えません。
- 生成ジョブ: 予算超過で止まったジョブの「予算を変更」（`PATCH /api/batches/{id}`）→ 止まった動画を再試行、
  作り直し中（`reopened_from`）の表示と前回の停止理由（`previous_error`）、キャンセルした動画だけが残った `partial` は
  「一部キャンセル」と表示。代替素材を使ったショット数（`plan.substitutions`）。
- 動画一覧・プレビュー・エディター: 作り直しが失敗・キャンセルされて前回の動画に戻った場合の説明（完成済み動画の `error`）、
  代替素材のショット（`shots[].substituted_from`）、Runway 素材の「Powered by Runway」表示。
  再生成・編集・再試行の 422（`rights_unconfirmed` / `budget_exceeded`）には次にすべきことを添えて表示します。
- 動画プレーヤー: 読み込みに失敗したとき、ブラウザが H.264 を再生できない場合はその旨を、そうでなければ署名付き URL の
  期限切れとして再読み込みを案内します。サムネイルは期限切れで読み込めないと API データを取り直します（30 秒に 1 回まで）。
- OpenRouter（動画生成プロバイダー `openrouter` と Claude の経路。実際の API キーでは未検証）:
  - 生成設定: プロバイダーカードに「未検証」。モデルの選択肢は `GET /api/system/capabilities` の一覧（OpenRouter のモデル一覧、
    取得できないときは組み込みの候補）で、提供元（`google/…` → Google など、モデル ID の先頭から）・解像度・秒数・1 秒あたりの
    料金を表示します。料金を読み取れないモデル（`price_known: false`、`price_per_second_usd: null`）は「料金未取得」として
    選べません（予算を確保できないため）。選択中は「OpenRouter での生成について」に、音声の生成はオフ（`generate_audio: false`）、
    プロンプトが OpenRouter を経由してモデルの提供元に送られること、OpenRouter の規約に加えてモデルの提供元の規約が適用されること、
    モデル一覧の出どころ（`models_note`）を表示します。9:16 への対応が不明なモデルには「9:16 未確認」。確認ダイアログと見積もりの
    「未検証」には、料金が OpenRouter のモデル一覧（または調査値）に基づく概算で、実費は完了時に OpenRouter が返す金額で
    記録する旨を表示します。前回のジョブが OpenRouter なら OpenRouter とそのモデルを引き継ぎ、そのモデルがモデル一覧から
    消えていた場合は既定のモデルを表示して `model: null`（プロバイダーの既定）で見積もり・作成します。見積もりはプロバイダー一覧の
    取得（またはその失敗）を待ってから要求します。
  - 設定画面: 「AI 機能」に Claude の経路（`capabilities.llm.route`: Anthropic 直接 / OpenRouter 経由 / テンプレート）、モデル、
    「未検証」、経路の優先順（ANTHROPIC_API_KEY → OPENROUTER_API_KEY → テンプレート）と `data_policy_note`（送信するデータ）。
    プロバイダー一覧の OpenRouter には、モデル一覧の出どころ（OpenRouter から取得 / キャッシュ / 組み込みの候補）・モデル数・
    料金未取得の数・取得からの経過時間（`models_fetched_at`）と `models_note`。料金表の料金未取得のモデルは「料金未取得」。
    `llm` のない古いバックエンドでは `claude_configured` を Anthropic 直接として扱います（`resolveLlm`）。
  - 絵コンテ: プレビューと生成ジョブの絵コンテに、作成した経路とモデル（例: 「Claude（OpenRouter 経由）」と
    `anthropic/claude-opus-5.5`、`Storyboard.route` / `model`）を表示します。OpenRouter 経由で呼び出したあとテンプレートに
    切り替わった場合は「Claude（OpenRouter 経由）の応答を使えなかったため、テンプレートで作成しました」（理由は警告に表示）。
    Claude 設定時のプレビューの説明に経路とモデル、OpenRouter 経由なら歌詞の本文が OpenRouter を通ることを表示します。
  - 費用: ダッシュボードの API 利用状況はプロバイダー `openrouter` を「OpenRouter」、Claude の直接呼び出し `anthropic` を
    「Claude（Anthropic 直接）」と表示し、OpenRouter の金額には OpenRouter 経由の Claude の費用も含む旨を添えます。
    素材のプレビューには OpenRouter 経由で生成した素材のモデルの提供元を表示します。

### プロジェクト配下のページ

`ProjectHeader`（タイトル・ステータス・サブナビ）または `ProjectNav` を各ページ上部に置きます。

```tsx
<ProjectHeader projectId={id} active="generate" />
<ProjectNav projectId={batch.project_id} active="jobs" batchId={batch.id} />
```

`active`: `"assets"`（楽曲・歌詞・素材・権利）/ `"generate"`（生成設定）/ `"jobs"`（生成ジョブ = `/batches/[id]`、
既定はプロジェクトの最新バッチ）/ `"videos"`（動画一覧）。

## デザインルール（ARCHITECTURE.md §8）

- ダークのみ。背景 `#0a0a0a`、サーフェス `#111113` / `#18181b`、ボーダー `#27272a`、文字 `#e4e4e7` / `#a1a1aa`。
- 青（`primary`, `#3b82f6`）は**主要アクションと進捗表示のみ**。1 画面に primary ボタンは原則 1 つ。
  選択状態・チェック・フォーカスリングはニュートラル（明るいグレー）。
- 赤はエラーのみ。ステータスは必ずアイコン + 日本語ラベル（`StatusBadge`）。
- 装飾的なアニメーションはなし（進行中を示すスピナーのみ）。`prefers-reduced-motion` を尊重。
- 9:16 プレビューは `Thumbnail`（署名付き URL を `<img>` で表示）。

## Next.js 16 の注意点

- `cacheComponents` / `partialPrefetching` が有効です。ページは静的シェルとしてプリレンダリングされ、
  データはクライアント（SWR）で取得します。
- 動的ルート（`/projects/[id]` など）で `usePathname()` / `useParams()` / `params` を読む部分は
  `<Suspense>` の内側に置いてください（`AppShell` も `children` を `<Suspense>` で包んでいます）。
- レンダー中に `Date.now()` / `new Date()` を呼ばないでください（相対時刻は `useNow()` を使用）。
- ページ遷移後も前のページは `<Activity>` で非表示のまま保持されます。フォームは送信成功時にリセットし、
  一時的なメニューは閉じる処理を入れてください（`DropdownMenu` は対応済み）。
