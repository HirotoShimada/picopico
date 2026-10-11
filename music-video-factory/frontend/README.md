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
    constants.ts          権利確認項目、世界観プリセット（フォールバック）、既定値
    utils.ts              cn() など
```

### API クライアント（`src/lib/api.ts`）

- エンドポイントごとの関数: `listProjects`, `createProject`, `uploadAudio(id, file, { onProgress, signal })`,
  `getAnalysis`, `getWorldEstimate`, `updateSegment`, `updateLyrics`, `autoTimeLyrics`, `uploadClips`, `estimateBatch`,
  `createBatch`, `getBatch`, `cancelBatch`, `updateBatchBudget`（`PATCH /api/batches/{id}`）, `downloadBatchZip`,
  `listVideos`, `editVideo`, `downloadVideo` など。
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
- 生成ジョブ: 予算超過で止まったジョブの「予算を変更」（`PATCH /api/batches/{id}`）→ 止まった動画を再試行、
  作り直し中（`reopened_from`）の表示と前回の停止理由（`previous_error`）、キャンセルした動画だけが残った `partial` は
  「一部キャンセル」と表示。代替素材を使ったショット数（`plan.substitutions`）。
- 動画一覧・プレビュー・エディター: 作り直しが失敗・キャンセルされて前回の動画に戻った場合の説明（完成済み動画の `error`）、
  代替素材のショット（`shots[].substituted_from`）、Runway 素材の「Powered by Runway」表示。
  再生成・編集・再試行の 422（`rights_unconfirmed` / `budget_exceeded`）には次にすべきことを添えて表示します。
- 動画プレーヤー: 読み込みに失敗したとき、ブラウザが H.264 を再生できない場合はその旨を、そうでなければ署名付き URL の
  期限切れとして再読み込みを案内します。サムネイルは期限切れで読み込めないと API データを取り直します（30 秒に 1 回まで）。

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
