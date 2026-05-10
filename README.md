# ピコピコバトル! — Party Mini Games

メイドインワリオ風の、数秒で終わるミニゲームが連続出題される 2 人対戦用パーティーゲームです。
ローカルの 1 台の PC で 2 人がキーボードを分け合って遊びます。

## 起動方法

```bash
npm install
npm run dev
```

ブラウザで `http://localhost:5173` を開いてください。

ビルド:

```bash
npm run build
npm run preview
```

## ネット対戦

同じWi-Fiや社内LAN内の別PCからアクセスして、1P/2Pとして参加できます。

```bash
npm run build
npm start
```

起動後、ホストPCでは `http://localhost:4173`、別PCからは `http://<ホストPCのIPアドレス>:4173` を開いてください。
タイトル画面で「ネット対戦」を選び、片方が部屋を作成して、もう片方が招待リンクまたは部屋コードで参加します。

公開サーバーに置く場合は、WebSocketを維持できるNode.js対応ホスティングにアップロードしてください。
`PORT` 環境変数が指定されている環境では、そのポートで起動します。

Vercelにフロントだけ置き、WebSocketサーバーを別ホストに置く場合は、Vercelの環境変数に
`VITE_WS_URL=wss://<WebSocketサーバーのURL>/ws` を設定してください。

### Render でWebSocketサーバーを公開する

このリポジトリには Render Blueprint 用の `render.yaml` を含めています。

1. Render Dashboard で **New +** → **Blueprint** を選択
2. このプロジェクトを置いた GitHub リポジトリを選択
3. `render.yaml` が検出されたら、そのまま作成
4. デプロイ完了後、Render の公開URLを確認
5. Render 側のゲームを使う場合は `https://<service>.onrender.com` を開く
6. Vercel のフロントから Render のWebSocketだけ使う場合は、Vercel の環境変数に以下を追加して再デプロイ

```bash
VITE_WS_URL=wss://<service>.onrender.com/ws
```

Render は公開インターネットでは `wss://` を使って接続してください。ローカル確認では `ws://localhost:4173/ws` を使います。

## 操作

| プレイヤー | 左 | 右 | 決定 |
| --- | --- | --- | --- |
| 1P | `A` | `D` | `Space` |
| 2P | `←` | `→` | `Enter` |

## ゲームの流れ

1. 初回起動時にプレイヤー名と顔写真プロフィールを作成
2. タイトル画面でローカル対戦またはネット対戦を選択
3. 全 10 ラウンド、毎回ランダムなミニゲームが出題されます
4. 成功したプレイヤーに +1 点
5. 全ラウンド終了後に最終スコアと勝者を表示

## プロフィール

初回起動時にプレイヤー名を入力し、カメラで顔写真を撮影できます。
撮影時は「マンガ集中線」「ネオン発光」「パーティー王冠」「レトロ写真」「ピクセル風」などのフィルターを選択できます。
カメラが使えない環境では、仮アイコンでそのまま進めます。

## 収録ミニゲーム (5 種)

| 名前 | 内容 |
| --- | --- |
| 光ったら押せ | ランプが緑になった瞬間に決定キー。早押しはフライング。 |
| 大きい数字を選べ | 左右の数字のうち、大きい方の方向キーを押す。 |
| おちものをキャッチ | 落ちてくるアイテムの真下に左右で移動して立つ。 |
| 正しい色を選べ | 「あか/あお/きいろ」の意味どおりの色を選ぶ。文字色に惑わされるな。 |
| 連打バトル | 制限時間中に決定キーを連打。多く押した方が勝ち。 |

## 構成

```
src/
  App.tsx                 # 全体のフェーズ管理
  engine/
    GameEngine.tsx        # ミニゲームをHUD/タイマーでラップ
  games/
    registry.ts           # ミニゲームのリストと出題キュー生成
    LightTapGame.tsx      # ミニゲーム本体は1ファイル1個
    BiggerNumberGame.tsx
    CatchItemGame.tsx
    ColorWordGame.tsx
    MashBattleGame.tsx
  hooks/
    useInput.ts           # キーボード入力 (1P/2P)
    useCountdown.ts       # ミリ秒カウントダウン
    useDelay.ts
  screens/
    TitleScreen.tsx
    RoundIntroScreen.tsx
    RoundResultScreen.tsx
    FinalResultScreen.tsx
  components/
    Hud.tsx               # 残り時間バー / スコア
    ControlsGuide.tsx     # 操作ガイド
  types.ts                # 共通型 (MiniGameProps など)
  index.css               # Tailwind + カスタムスタイル
```

## ミニゲームの追加方法

1. `src/games/MyGame.tsx` を新規作成し、以下の `MiniGameProps` を受け取るコンポーネントを書く

   ```tsx
   import type { MiniGameProps } from '../types';

   export function MyGame({ durationMs, input, onFinish }: MiniGameProps) {
     // input.id が変わったら新しい入力。 onFinish は1回だけ呼ぶ。
     return <div>...</div>;
   }
   ```

2. `src/games/registry.ts` の `MINI_GAMES` に登録

   ```ts
   { id: 'my-game', name: 'マイゲーム', instruction: '〜せよ', durationMs: 5000, Component: MyGame },
   ```

3. これだけで出題ローテーションに加わります。

## 設計メモ

- **入力**: グローバルな `keydown` リスナーを `useInput` に集約。各イベントに連番 `id` を付け、コンポーネント側は `useEffect` の依存に `input?.id` を入れて新規押下を検出します。
- **タイマー**: `useCountdown` は `requestAnimationFrame` ベース。`GameEngine` は安全装置として、ミニゲームが `onFinish` を呼ばずに時間切れになった場合でもラウンドを必ず終了させます。
- **スコア管理**: `App` の `scores` ステートが唯一の真実。各ラウンド終了時に `outcome.winners` から +1 します。
- **再マウント**: ラウンドが切り替わるたびに `engineKey` を更新し、`GameEngine` を再マウントしてミニゲーム内部の状態を毎回リセットしています。
