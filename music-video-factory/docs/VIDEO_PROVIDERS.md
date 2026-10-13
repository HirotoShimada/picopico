# 動画生成プロバイダ調査（公式 API のみ）

- 調査日: **2026-10-11**（以下、特記なき「as of」はこの日付）
- 目的: AI Music Video Factory で 9:16（Instagram ストーリーズ）向けのフォトリアル素材クリップを生成するための公式 API の比較と、アダプタ実装（`app/services/providers/{kling,runway,veo}.py`）・料金表（`app/services/cost.py` の `PRICING`）の根拠をまとめる。
- 対象: 公式（ファーストパーティ）API のみ。非公式ラッパー、再販業者（piapi / kie.ai / useapi.net / klingapi.com 等）、ブラウザ自動操作は対象外。
  - 例外: 利用者の判断で、複数社のモデルを 1 本のキーで呼べる集約 API の **OpenRouter** を、動画生成と Claude（絵コンテ・翻訳）の両方に使う。OpenRouter には公式 SDK があるため、その SDK を一次情報として §8 にまとめた（2026-10-12 調査）。

## 調査方法と信頼度ラベル

このサンドボックスでは `docs.dev.runwayml.com` / `kling.ai` / `ai.google.dev` / `docs.cloud.google.com` / `developers.openai.com` / `lumalabs.ai` / `platform.minimax.io` / `docs.byteplus.com` / `alibabacloud.com` などのドキュメントホストへの直接アクセスがプロキシで拒否された（WebFetch・curl ともに失敗）。そのため、記述ごとに次のラベルで確認の度合いを示す。

| ラベル | 意味 |
|---|---|
| **[A] 一次確認** | 本セッションで公式の成果物を直接取得して読んだ。対象は公式 SDK のソース（npm `@runwayml/sdk@4.21.0`（2026-10-02 公開）、PyPI `google-genai==2.29.0`（2026-10-07 公開）、PyPI `openai==3.28.0`（2026-10-09 公開））、Google の API Discovery 文書（`generativelanguage` v1beta rev.20261010、`aiplatform` v1 rev.20261007）、`cloud.google.com/vertex-ai/generative-ai/pricing` の HTML、Gemini API エンドポイントへの実リクエスト（キーなしで 403 を確認）。 |
| **[B] 公式・検索経由** | 公式ページの内容を Web 検索エンジンの抜粋で確認した（URL は公式ドメイン）。ページ本体は開けていないため、実装前に再確認する。 |
| **[C] 推定・前提** | 推定、計算上の仮定、2026 年以前の知識。**未検証として扱う**。 |

金額はすべて USD、税抜の定価。クレジットやユニットからの換算式は各節に示す。

---

## 0. 結論（TL;DR）

1. **最初に実装するのは Veo（Gemini API, `veo-3.1-lite-generate-preview`）**。理由は以下のとおり。
   - このサンドボックスから届く唯一の生成 API ホスト（`generativelanguage.googleapis.com`）であり、キーさえあれば E2E 検証ができる。
   - 認証は API キー 1 本で済む。
   - 9:16 をネイティブで出力でき、1080p にも対応する。
   - $0.05/s（720p）と安い。
   - リクエスト形状が公式 SDK と Discovery 文書で確定できる。
   - ただし preview モデル ID しかない、Tier 1 の 1 日あたりリクエスト数（RPD）が極めて小さいという報告がある、seed 非対応、音声が常に生成・課金される、出力の保持が 2 日、という制約がある。
2. **次に Runway（`gen4.5` テキスト→動画、画像があるときは `gen4_turbo`）**。
   - 型付き公式 SDK（2026-10-02 版）でリクエスト・レスポンスの全形状が確定している。
   - 同時実行数を超えた分は `THROTTLED` としてサーバー側で待機させるため、運用しやすい。
   - 同じキーで `veo3.1_fast` や `seedance2` なども呼べる。
   - $0.12/s（gen4.5）。
3. **Kling は 3 番目**。
   - 単価は最安水準（`kling-v2-5-turbo` std が $0.042/s、`kling-3.0` は $0.112〜0.14/s）。
   - ただし 2026-06 に新 API へ移行中で、仕様が流動的。認証は API キー方式に変わり、ホストは `api-singapore.klingai.com` へ移った。ステータス文字列も `succeed` と `succeeded` が混在している。
   - 最小の通常パックは $700（お試しパックは $9.8）。
   - `config.py` への `KLING_API_KEY` の追加と `KLING_API_BASE` の既定値変更は対応済み（§2.4）。
4. **OpenAI Sora API は 2026-09-24 に提供終了済み**のため対象外 [A]。Luma、MiniMax Hailuo、ByteDance Seedance（BytePlus）、Alibaba Wan には公式 API がある。ただし今回の 3 社で足り、かつ Seedance、Hailuo、Wan の一部は Runway 経由でも呼べるため、MVP では実装しない。
5. 30 本 × 15 秒の場合、クリップを再利用（`balanced`、60 クリップ）すると、Veo 3.1 Lite 720p で **$18**（再利用なしなら $54）、Runway gen4.5 で **$36**（同 $108）になる。詳しくは §3。
6. **OpenRouter**（§8）を使うと、1 本のキーで Veo 3.1 / Kling / Hailuo / Seedance / Wan などの動画モデルと Claude を呼べる。ただし次の制約がある。
   - 実際の API に送ったのは無料の `GET /videos/models`（2026-10-13）だけで、動画の生成は一度も通していない。すべて未検証（`verified: false`）。
   - 動画生成はゼロデータ保持（ZDR）の対象外。
   - Claude 経由の絵コンテでは、歌詞が OpenRouter とその先の提供元（Anthropic / Amazon Bedrock / Google Vertex / Azure / Claude Platform on AWS のいずれか）を通る。

---

## 1. 比較表（主要 3 社）

| 項目 | Google Veo（Gemini API） | Google Veo（Vertex AI） | Runway API | Kling AI API |
|---|---|---|---|---|
| 公式 API | ○ [A] | ○ [A] | ○ [A] | ○ [B] |
| ベース URL | `https://generativelanguage.googleapis.com/v1beta` [A] | `https://{LOCATION}-aiplatform.googleapis.com/v1` [A] | `https://api.dev.runwayml.com` [A] | `https://api-singapore.klingai.com`（旧 `api.klingai.com`）[B] |
| 認証 | ヘッダ `x-goog-api-key: <API key>` [A] | OAuth2 `Authorization: Bearer <access token>`（サービスアカウント）[C] | `Authorization: Bearer <key>` と `X-Runway-Version: 2024-11-06` [A] | 新: `Authorization: Bearer <API Key>`。旧: AK/SK から HS256 JWT を作り Bearer で送る [B] |
| 推奨モデル | `veo-3.1-lite-generate-preview` [B] | `veo-3.1-lite-generate-001`（preview）[B] | `gen4.5` / `gen4_turbo` [A] | `kling-3.0`（新 API）/ `kling-v2-5-turbo`（旧 API）[B] |
| クリップ長 | 4 / 6 / 8 秒（1080p と 4k は 8 秒のみ）[B] | 4 / 6 / 8 秒 [B] | gen4.5: 2〜10 秒（整数）[A]。gen4_turbo: 2〜10 秒 [B] | kling-3.0: 3〜15 秒（整数）[B]。v2.5 Turbo: 5 / 10 秒 [C] |
| 解像度 | 720p / 1080p（Lite は 4k 不可。標準と Fast は 4k 可）[B] | 720p / 1080p / 4k（Lite は 1080p まで）[A][B] | 720p（`720:1280`）[A] | 720p / 1080p / 4k [B] |
| 9:16 | ○ `aspectRatio:"9:16"` [A][B] | ○ [B] | ○ `ratio:"720:1280"` [A] | ○ `aspect_ratio:"9:16"` [B] |
| T2V / I2V | 両方（I2V は `instances[0].image`）[A] | 両方 [A] | gen4.5 は両方。**gen4_turbo は画像必須（I2V のみ）**[A] | 両方（I2V は別エンドポイント）[B] |
| seed | **不可**（SDK が送信を拒否）[A] | 可 `parameters.seed` [A] | 可 `seed` [A] | 未確認 [C] |
| 音声 | 常に生成・課金（`generateAudio` は送れない）[A][B] | `generateAudio:false` で無音・低価格 [A] | gen4.5 は無音（音声パラメータなし）[A] | `settings.audio:"off"` が既定 [B] |
| 出力 URL の保持 | サーバー上に **2 日** [B] | `storageUri` 指定時は自分の GCS バケット（永続）[A] | URL は **24〜48 時間**で失効（タスクを再取得すれば新しい URL）[A] | **30 日**で削除 [B] |
| 単価（推奨構成） | Lite 720p **$0.05/s**、1080p $0.08/s（音声込み）[B] | Lite 無音 720p **$0.03/s**、1080p $0.05/s [A] | gen4.5 **$0.12/s**、gen4_turbo $0.05/s [B] | v2.5 Turbo std **$0.042/s**、3.0 で $0.112〜0.14/s [B] |
| レート制限 | プロジェクト単位の RPM / RPD。Tier 1 で Veo 各 2 RPM / 10 RPD という報告あり [B（フォーラム）] | クォータ制（申請で増枠）[B] | ティアごと・モデルごとの同時実行数と 1 日の生成数。超過分は `THROTTLED` で待機、1 日の上限超過は 429 [B] | 購入パックごとの同時実行数（お試し 5 / 通常 20）。超過は 429（1302 / 1303）[B] |
| 透かし | SynthID（不可視）[B] | SynthID と C2PA に対応 [B] | 不可視透かしと C2PA（2 年前の公式記事）[B] | 既定 URL は透かしなし。透かし付き版は `watermark_url` で任意 [B] |
| 商用利用 | 可（Google は所有権を主張しない）[B] | 可（ただし preview 製品の本番利用には条件がある）[B] | 可（出力の所有権は利用者）[B] | マーケティングページでは可と記載。API 規約本文は未確認 [B][C] |
| このサンドボックスから | **到達可**（キーなしで 403 を確認）[A] | 到達可（Discovery 文書は取得できた）[A] | **到達不可** | **到達不可** |

---

## 2. 本アプリ向けの実装推奨

### 2.1 実装順と既定モデル

| 順 | provider id | 既定モデル | 既定パラメータ | 理由 |
|---|---|---|---|---|
| 1 | `veo` | `veo-3.1-lite-generate-preview` | `aspectRatio:"9:16"`, `resolution:"720p"`, `durationSeconds:6` | 到達でき、API キー 1 本で使え、最安クラスで高品質。形状は SDK と Discovery 文書で確定 |
| 2 | `runway` | `gen4.5`（画像入力ありなら `gen4_turbo`） | `ratio:"720:1280"`, `duration:5` | 公式型定義があり、運用面が成熟（`THROTTLED` での待機、`estimatedCost`、`failureCode`） |
| 3 | `kling` | `kling-3.0`（新 API）。AK/SK しかない場合は旧 API の `kling-v2-5-turbo` std | `aspect_ratio:"9:16"`, `resolution:"720p"`, `duration:5`, `audio:"off"`, **`multi_shot:false`** | 最安だが API 移行中。既定の `multi_shot:true` のままだとクリップ内にカットが入る |

`clip_duration_for(model, wanted)` は「要求以上で最も近い対応秒数」を返す。対応秒数は次のとおり。

- Veo: [4, 6, 8]（1080p は [8]）
- gen4.5 / gen4_turbo: 2〜10 の整数
- kling-3.0: 3〜15 の整数
- 旧 kling: [5, 10]

### 2.2 アダプタ共通の設計メモ（全社共通。[C] 設計判断）

- **作成 → ポーリング → 即ダウンロード**。出力 URL は 24 時間〜2 日で失効するため、成功を検知したらすぐ `out_path` へ保存する。保存後の参照には自前の Storage を使う。
- **成功したジョブは課金済みなので、再送信しない**。ダウンロードや ffprobe での検証が一時的に失敗したときは、取得だけを最大 3 回（`OUTPUT_FETCH_ATTEMPTS`）までやり直す。間隔はポーリングと同じで、タイムアウトの範囲内に限る。やり直す前にジョブをもう一度取得し、新しい出力 URL を使う（Runway の署名付き URL は失効するため。Veo と Kling は同じ URI が返る）。それでも取得できない場合は、`cost_usd`（実額か表の単価）を持つ再試行不可の `ProviderTaskError` にする。取得中のキャンセル（`ProviderCancelled`）にも `cost_usd` を付ける。パイプラインはこの金額を CostRecord と `batch.cost_usd` に計上し、同じクリップを新しいジョブで作り直さない。
- **ジョブ ID は作成直後に保存する**（`generate(..., on_submitted=)` が `on_submitted(job_id, resume_token)` を呼び、パイプラインが `Clip.external_job_id` と `params.open_job` に保存する）。プロセスやワーカーが落ちた後は、`resume(resume_token, ...)` で同じジョブのポーリングと取得を再開し、新しいジョブを送信しない。`resume_token` はジョブ ID と同じだが、Kling は照会に `external_task_id` が要るため `new:<external_task_id>:<task id>` / `legacy:<task id>` とする。ジョブ ID を報告しない古いアダプタでは、中断した試行を課金済みとみなして計上してから再送信する。
- **失敗したジョブの課金**: Runway の FAILED / CANCELLED タスクは、報告された `cost.credits × $0.01`（多くは 0）を `ProviderTaskError.cost_usd` に載せ、パイプラインはこれを確定値として計上する（0 なら計上なし）。リモートでエラー終了したジョブ（`provider_failed`）は課金なしとみなして再試行できる。キャンセル API がない Veo / Kling でタイムアウト・ポーリング喪失・キャンセルにより放棄したジョブは、課金されたものとみなして表の単価で計上し、再送信しない（`supports_remote_cancel = False`）。いずれも実キーでは未検証。
- ポーリング間隔は 5 秒から始め、指数バックオフで最大 30 秒、ジッタ ±20%。Runway は「同じタスクを 5 秒未満の間隔で取得しても更新されない」と明記しており [A]、SDK は 6±1.5 秒間隔でポーリングしている [A]。`timeout=900` 秒を超えたら、Runway は `DELETE /v1/tasks/{id}` でキャンセルする。
- `cancel_check()` が真になったら同様にキャンセルを試みる（Runway のみ API がある。Veo と Kling ではキャンセル方法を確認できていない）。
- **再試行可否の分類**（`ProviderError(retryable=...)`）:
  - 再試行可: HTTP 429、5xx、タイムアウト、接続エラー。Runway の `failureCode` が `INTERNAL*`、`null`、`INTERNAL.BAD_OUTPUT.*` の場合。Veo の `operation.error.code` が 13（INTERNAL）または 14（UNAVAILABLE）の場合。
  - 再試行不可: 400 / 401 / 403 / 404、`SAFETY.*`、`ASSET.INVALID`。Veo の `raiMediaFilteredCount > 0` で動画が 0 本の場合。Kling の 1102（リソースパック枯渇）。
  - キーがない、または無効な場合は `ProviderNotConfigured`。
- コストは「秒単価 × 実クリップ秒」で計上する。Runway はタスク完了時の `cost.credits` が正なので、`credits × $0.01` で実額に置き換える [A]。
- プロンプトは `prompting.sanitize_prompt` を通したものを送る（実在人物を禁止し、"no text, no captions, no watermark" を付ける）。Kling の新 API にはネガティブプロンプト欄がないため、ネガティブ内容は本文に「Avoid: ...」として連結する [B]。
- seed を送れない API（Gemini API の Veo）では `cache_key` の seed は単なる識別子になる。同一キーのクリップは DB のキャッシュから再利用するので、再現性の問題は起きない。
- 生成音声（Veo）は使わない。レンダラは元の楽曲音声だけを使う（ARCHITECTURE §6.6）。

### 2.3 `cost.py` の `PRICING` 推奨値

すべて `verified: false`。実キーでのテストがまだないため。`as_of` は 2026-10-11。

| provider | model | clip_durations | default_duration | resolution | price_per_second_usd | source_url |
|---|---|---|---|---|---|---|
| veo | `veo-3.1-lite-generate-preview` | [4, 6, 8] | 6 | 720x1280 | 0.05 | https://ai.google.dev/gemini-api/docs/pricing |
| veo | `veo-3.1-fast-generate-preview` | [4, 6, 8] | 6 | 720x1280 | 0.10 | 同上 |
| veo | `veo-3.1-generate-preview` | [4, 6, 8] | 6 | 720x1280 | 0.40 | 同上 |
| runway | `gen4.5` | [2, 3, 4, 5, 6, 7, 8, 9, 10] | 5 | 720x1280 | 0.12 | https://docs.dev.runwayml.com/guides/pricing/ |
| runway | `gen4_turbo` | [2, 3, 4, 5, 6, 7, 8, 9, 10] | 5 | 720x1280 | 0.05 | 同上（**画像必須**。最低 10 クレジット/回） |
| runway | `veo3.1_fast` | [4, 6, 8] | 6 | 720x1280（`1080:1920` も可） | 0.10 | 同上（`audio:false`） |
| kling | `kling-3.0` | [3, 4, …, 15] | 5 | 720x1280 | 0.112 | https://kling.ai/document-api/updates/api（0.8 units/s × $0.14） |
| kling | `kling-v2-5-turbo` | [5, 10] | 5 | 720x1280（std） | 0.042 | https://kling.ai/document-api/pricing/base/video（0.3 units/s × $0.14） |

1080p で出す場合の単価は次のとおり。モデル ID に解像度の別名を設ける（例: `veo-3.1-lite-generate-preview@1080p`）かどうかは、アダプタ担当者が決める [C]。

- Veo Lite: $0.08/s（8 秒固定）
- Veo Fast: $0.12/s（8 秒固定）
- kling-3.0: $0.14/s

### 2.4 統合担当への申し送り（他担当のファイルに関わる事項）

- `config.py`（**対応済み**。2026-10-11 時点の `config.py` で確認）
  - `KLING_API_KEY` を追加した（新 API はキー方式のみ。AK/SK は旧 API 専用で、新モデルには非対応）[B]。
  - `KLING_API_BASE` の既定値を `https://api-singapore.klingai.com` に変えた（旧既定値は `https://api.klingai.com`）[B]。
  - `KLING_ACCESS_KEY` / `KLING_SECRET_KEY` は旧 API へのフォールバック用として残した。
- `RUNWAY_API_KEY` はそのまま使える。公式 SDK の環境変数名は `RUNWAYML_API_SECRET` だが、HTTP で直接呼ぶなら関係ない [A]。
- Vertex AI の Veo（無音で最安）を使う場合は、`GOOGLE_CLOUD_PROJECT`、`GOOGLE_CLOUD_LOCATION` とサービスアカウント認証が要る。MVP の範囲外とする。

### 2.5 アダプタ実装時の判断（2026-10-11、`app/services/providers/`）

調査で確定しなかった点は、次のように実装した。いずれも実キーでは未検証で、テストは respx のモック HTTP のみ。全モデルが `verified: false`。

- **Veo（Gemini API）**
  - `personGeneration` と `sampleCount` は送らない。許容値がモードと地域で異なるため、サーバーの既定値に任せる。
  - `durationSeconds` は SDK に合わせて整数で送る。
  - 1080p は別名モデル `veo-3.1-lite-generate-preview@1080p`（8 秒固定、$0.08/s）として提供する。
  - ダウンロード時、API キーは API ホストにだけ送る。リダイレクト先には送らない。
  - キャンセル API を確認できていないため、キャンセルやタイムアウトの後もサーバー側の生成は続き、課金され得る。
- **Runway**
  - 入力画像は data URI（5MB 以下）だけに対応する。`/v1/uploads` の 2 段階アップロードは形状を一次確認していないため、使っていない。
  - `contentModeration.publicFigureThreshold` は `"auto"` を明示して送る。
  - `gen4_turbo` は画像→動画専用として扱う（`text_to_video: false`）。画像のないリクエストは送信前に拒否し、料金比較にも含めない。
  - キャンセル時とタイムアウト時は `DELETE /v1/tasks/{id}` を送る。
  - 実費はタスクの `cost.credits × $0.01` で記録する。
- **Kling**
  - 新 API の照会には、公式例で確認できた `external_task_ids` を使う。`external_task_id` は毎回 `mvf-<uuid>` を発行する。
  - 照会結果に作成したタスクが 6 回続けて含まれない場合（`MAX_LOOKUP_MISSES`、約 1.5 分）は、900 秒のタイムアウトを待たずに再試行不可のエラーにする（照会の仕様違いで 15 分待ったうえに再送信して重複課金するのを防ぐ）。想定外の応答形状は再試行可の `provider_bad_response`（連続 5 回まで）とする。
  - 画像→動画の `contents[].url` には生の Base64 を送る。data URI を受け付けるかは未確認 [C]。
  - 失敗タスクの再試行可否は暫定のヒューリスティックで判定する。メッセージに internal、timeout、busy、try again などの語があれば再試行可とする。
  - 業務コード 1302 と 1303、5xxx は再試行可。1102 を含むそれ以外の 1xxx は再試行不可。
  - 旧 API の JWT は、PyJWT を使わず標準ライブラリの hmac で HS256 署名する。
  - モデルと認証の対応: `kling-3.0` には `KLING_API_KEY`、`kling-v2-5-turbo` には AK/SK が必要。AK/SK だけが設定されている場合は `kling-v2-5-turbo` が既定モデルになる。

---

## 3. コスト試算

### 3.1 前提 [C]

- 30 本 × 15 秒、120 BPM。1 ショットは平均 2.5 秒（1〜2 小節）なので、1 本あたり 6 ショット、`shots_total = 180`。
- プールサイズ K（ARCHITECTURE §6.4）は次のとおり。
  - `max_variety`: min(180, 60) = 60
  - `balanced`: ≈180/3 = 60
  - `economy`: ≈180/6 = 30
  - 再利用なし（Estimate の `naive_total_usd` 相当）: 180
- クリップ長は各モデルの「5 秒以上で最も近い対応秒数」。Veo 720p は 6 秒、1080p は 8 秒、それ以外は 5 秒。
- **計算式: 費用 = クリップ数 × クリップ秒 × 秒単価**。LLM の費用（数セント程度）は含まない。

### 3.2 試算表

| モデル（構成） | $/s | クリップ秒 | 1 本だけ（6 クリップ） | 30 本 economy（30） | 30 本 balanced（60） | 30 本 再利用なし（180） |
|---|---|---|---|---|---|---|
| Kling `kling-v2-5-turbo` std 720p（旧 API、無音） | 0.042 | 5 | $1.26 | $6.30 | $12.60 | $37.80 |
| Kling `kling-v2-5-turbo` pro 1080p（旧 API、無音） | 0.07 | 5 | $2.10 | $10.50 | $21.00 | $63.00 |
| Kling `kling-3.0` 720p（新 API） | 0.112 | 5 | $3.36 | $16.80 | $33.60 | $100.80 |
| Kling `kling-3.0` 1080p（新 API） | 0.14 | 5 | $4.20 | $21.00 | $42.00 | $126.00 |
| Runway `gen4_turbo`（画像必須） | 0.05 | 5 | $1.50 | $7.50 | $15.00 | $45.00 |
| Runway `gen4.5` | 0.12 | 5 | $3.60 | $18.00 | $36.00 | $108.00 |
| Veo 3.1 Lite / Gemini API 720p | 0.05 | 6 | $1.80 | $9.00 | **$18.00** | $54.00 |
| Veo 3.1 Lite / Gemini API 1080p | 0.08 | 8 | $3.84 | $19.20 | $38.40 | $115.20 |
| Veo 3.1 Fast / Gemini API 1080p | 0.12 | 8 | $5.76 | $28.80 | $57.60 | $172.80 |
| Veo 3.1 Lite / Vertex 無音 720p | 0.03 | 6 | $1.08 | $5.40 | $10.80 | $32.40 |
| Veo 3.1 Lite / Vertex 無音 1080p | 0.05 | 8 | $2.40 | $12.00 | $24.00 | $72.00 |
| Veo 3.1 / Gemini API 1080p | 0.40 | 8 | $19.20 | $96.00 | $192.00 | $576.00 |

計算例: Veo 3.1 Lite 720p の balanced は 60 × 6 秒 × $0.05 = **$18.00**。再利用なしでは 180 × 6 × $0.05 = $54.00 で、差額 $36.00（67% 削減）が `savings_usd` になる。

補足:

- 既定予算（`DEFAULT_BUDGET_USD=50`）に収まるのは、balanced なら Veo Lite（720p・1080p とも）、Runway gen4.5、Kling の全構成。再利用なしの場合は Veo Lite 720p（$54）でも超過する。
- `gen4_turbo` は安いが入力画像が必要。画像生成の費用は別にかかる（Runway の `gen4_image` など。本書では単価未調査）。
- **レート制限が実質の上限になり得る**。Gemini API の Tier 1 で「Veo 各モデル 10 RPD」という報告どおりなら、60 クリップの生成に 6 日かかる。Runway の Tier 1 は 1 日 50〜200 生成で、同時実行は 1〜2。量産には上位ティアへの昇格か増枠申請が前提になる（§6）。

---

## 4. プロバイダ詳細

### 4.1 Google Veo — Gemini API（Gemini Developer API）

**公式 API**: あり。ドキュメントは https://ai.google.dev/gemini-api/docs/veo [B]、リクエスト形状は公式 SDK `google-genai==2.29.0` と Discovery 文書で確認した [A]。

**モデル ID**:

- [B] `veo-3.1-generate-preview`、`veo-3.1-fast-generate-preview`、`veo-3.1-lite-generate-preview`。いずれも preview。標準と Fast は停止日未発表。Lite は 2026-03-31 に提供開始。
  - 出典: https://ai.google.dev/gemini-api/docs/deprecations 、https://ai.google.dev/gemini-api/docs/changelog
- [B] `veo-2.0-generate-001`、`veo-3.0-generate-001`、`veo-3.0-fast-generate-001` は **2026-06-30 に提供終了**。

**仕様** [B]（https://ai.google.dev/gemini-api/docs/veo 、as of 2026-10-11）:

- `durationSeconds`: 4 / 6 / 8。1080p と 4k、延長、参照画像を使う場合は 8 のみ。
- `resolution`: `"720p"`（既定）/ `"1080p"` / `"4k"`。Lite は 4k 不可。
- `aspectRatio`: `"16:9"`（既定）/ `"9:16"`。Lite で 9:16 と 1080p を組み合わせられる（8 秒時）。
- Lite はテキスト入力 1,024 トークンまで、1 リクエスト 1 本まで。
- `personGeneration`:
  - T2V と延長では `"allow_all"` のみ。
  - I2V、補間、参照画像では `"allow_adult"` のみ。
  - EU / UK / CH / MENA では `"allow_adult"` のみ。
  - フォーラムには食い違う報告がある。
- SynthID による不可視透かしが入る。
- 生成動画はサーバー上に 2 日保持された後に削除される。

**送れないパラメータ** [A]: SDK（`_GenerateVideosConfig_to_mldev`）は、Gemini Developer API モードで `seed`、`generateAudio`、`storageUri`（`output_gcs_uri`）、`fps`、`compressionQuality`、`labels`、`resizeMode`、`mask`、`pubsubTopic` を送ろうとすると `ValueError` で拒否する。したがって **seed は使えず、音声は常に生成される**。

**作成** [A]（Discovery 文書の `models.predictLongRunning`、SDK の実装、実リクエストによる 403 応答で確認）:

```http
POST https://generativelanguage.googleapis.com/v1beta/models/{model}:predictLongRunning
x-goog-api-key: $GOOGLE_API_KEY
Content-Type: application/json

{
  "instances": [{
    "prompt": "…英語プロンプト…",
    "image": {"bytesBase64Encoded": "<base64>", "mimeType": "image/png"}   // I2V のときのみ
  }],
  "parameters": {
    "aspectRatio": "9:16",
    "resolution": "720p",
    "durationSeconds": 6,
    "negativePrompt": "text, captions, watermark, celebrity, …",
    "personGeneration": "allow_all",     // T2V。I2V は "allow_adult"
    "sampleCount": 1
  }
}
→ 200 {"name": "models/veo-3.1-lite-generate-preview/operations/<op-id>"}
```

その他の任意フィールド [A]:

- `instances[0].lastFrame`（画像。I2V で最終フレームを指定）
- `instances[0].referenceImages[]`（参照画像。Lite は非対応 [B]）
- `instances[0].video`（延長）
- `parameters.enhancePrompt`
- トップレベルの `webhookConfig`（`{uris:[…], userMetadata:{…}}`。Gemini API 専用で、ポーリングを省ける）

`durationSeconds` は SDK では整数として送られる [A]。ドキュメントの表では `"4"` / `"6"` / `"8"` と文字列で書かれている [B]。

**ポーリング** [A]（Discovery 文書の `models.operations.get`。SDK は Gemini API に対しては GET を使い、`fetchPredictOperation` は Vertex 専用として拒否する）:

```http
GET https://generativelanguage.googleapis.com/v1beta/{name}
x-goog-api-key: $GOOGLE_API_KEY
```

応答（`google.longrunning.Operation` 形式）は次のように解釈する。

- `done` が無いか `false` のとき: 実行中。進捗率は返らない。
- `done: true` で `error` があるとき: 失敗。`error` は `{code, message, status}`。
- `done: true` で `response.generateVideoResponse.generatedSamples[0].video.uri` があるとき: 成功。
- `done: true` で動画が 0 本、かつ `response.generateVideoResponse.raiMediaFilteredCount > 0` のとき: 安全フィルタで除外された。理由は `raiMediaFilteredReasons[]`。再試行不可として扱う。

**ダウンロード**:

- `GET {video.uri}` にヘッダ `x-goog-api-key` を付けて取得する。SDK は URI から `files/{id}` を取り出し、`files/{id}:download?alt=media` を呼んでいる [A]。
- 公式の curl 例は `-L` でリダイレクトを追うが、リダイレクト先のストレージにまで API キーが送られるという指摘がある [B（GitHub PR）]。**キーは API ホストにだけ送り、リダイレクトはキーなしで追う**実装にする [C]。

**レート制限** [B]:

- プロジェクト単位で RPM / TPM / RPD がある。RPD は太平洋時間の 0 時にリセット。
- Tier 1 は請求先アカウントの紐付けで到達する。Tier 2 は累計支払い $100 以上かつ初回支払いから 3 日経過が条件。
- モデル別の上限値は AI Studio で確認する（https://ai.google.dev/gemini-api/docs/rate-limits ）。
- フォーラムでは「Tier 1 で Veo 各モデル 2 RPM / 10 RPD」という報告や、空きがあるのに 429 `RESOURCE_EXHAUSTED` になるという報告が複数ある（未検証）。

**価格** [B]（https://ai.google.dev/gemini-api/docs/pricing 、as of 2026-10-11、有料ティア。Veo 3.1 は無料枠なし）:

| モデル | 720p | 1080p | 4k |
|---|---|---|---|
| Veo 3.1（音声込み） | $0.40/s | $0.40/s | $0.60/s |
| Veo 3.1 Fast（音声込み） | $0.10/s | $0.12/s | $0.30/s |
| Veo 3.1 Lite（音声込み） | $0.05/s | $0.08/s | 非対応 |

Gemini API では音声を切れないため、上の「音声込み」の単価が適用される [A（パラメータ拒否）+ B（料金表）]。

**商用利用・権利** [B]:

- Gemini API 追加利用規約（https://ai.google.dev/gemini-api/terms ）では、Google は生成コンテンツの所有権を主張しない。生成物の利用責任は利用者が負う。
- Generative AI 禁止使用ポリシーに従う必要があり、不正利用の監視がある。
- 実在人物の扱いは、`personGeneration` の制限とポリシーで制御される。肖像権の判断は利用者の責任。
- 本アプリ側でも、実在人物を禁止するプロンプト規則（§6.3 `sanitize_prompt`）を必ず適用する。

### 4.2 Google Veo — Vertex AI（2026 年に「Gemini Enterprise Agent Platform」へ改称）

改称は次の 2 点で確認した [A]。

- Discovery 文書の title が "Agent Platform API" になっている。
- SDK のエラーメッセージに "Gemini Enterprise Agent Platform mode" とある。

**モデル ID** [B]（https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/veo/3-1-generate ）:

- `veo-3.1-generate-001`（GA）、`veo-3.1-fast-generate-001`（GA、2025-11-17 公開、2026-11-17 以降に廃止予定）、`veo-3.1-lite-generate-001`（preview、2026-04-02）。
- いずれも 4 / 6 / 8 秒、9:16 と 16:9 に対応。

**作成 / ポーリング** [A]（Discovery 文書 `aiplatform` v1 と SDK）:

```http
POST https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{model}:predictLongRunning
Authorization: Bearer $(gcloud auth print-access-token)
{
  "instances": [{"prompt": "…", "image": {"bytesBase64Encoded": "…", "mimeType": "image/png"}}],
  "parameters": {"aspectRatio": "9:16", "resolution": "720p", "durationSeconds": 6,
                 "generateAudio": false, "negativePrompt": "…", "personGeneration": "allow_adult",
                 "sampleCount": 1, "seed": 1234, "storageUri": "gs://bucket/prefix/"}
}
→ {"name": "projects/…/locations/…/publishers/google/models/{model}/operations/<id>"}

POST …/publishers/google/models/{model}:fetchPredictOperation
{"operationName": "<上の name>"}
→ {"name": …, "done": true,
   "response": {"videos": [{"gcsUri": "gs://…mp4" | "bytesBase64Encoded": "…", "mimeType": "video/mp4"}],
                "raiMediaFilteredCount": 0, "raiMediaFilteredReasons": []}}
```

補足:

- 画像は `gcsUri` でも渡せる。他に `compressionQuality`、`resizeMode`、`fps`、`pubsubTopic`、`labels`（課金トラッキング用）が使える [A]。
- `storageUri` を省略すると base64 で返る、と SDK の解析コードから推定している [C]。
- プロジェクトを指定しないパス `/v1/publishers/google/models/{model}:predictLongRunning` も Discovery 文書にある（API キーで使う express mode 用と推定）[A][C]。

**価格** [A]（https://cloud.google.com/vertex-ai/generative-ai/pricing の HTML を 2026-10-11 に取得。表の単位表記は "/ 1 count"。料金体系から 1 count = 生成動画 1 秒と解釈した [C]）:

| モデル | 音声あり 720p | 音声あり 1080p | 音声あり 4k | 無音 720p | 無音 1080p | 無音 4k |
|---|---|---|---|---|---|---|
| Veo 3.1 | $0.40 | $0.40 | $0.60 | $0.20 | $0.20 | $0.40 |
| Veo 3.1 Fast | $0.10 | $0.12 | $0.30 | $0.08 | $0.10 | $0.25 |
| Veo 3.1 Lite | $0.05 | $0.08 | – | **$0.03** | **$0.05** | – |
| Veo 3 / 3 Fast | $0.40 / $0.10〜0.12 | | | $0.20 / $0.08〜0.10 | | |
| Veo 2 | – | – | – | $0.50（720p） | | |

**クォータ** [B]: GA ページには「リージョンごと、ベースモデルごとに毎分 50」（単位の表記が不明瞭）とあり、旧 preview ページには「10 RPM / プロジェクト、1 リクエスト最大 4 本」とあった。上限値は申請で調整できる（https://docs.cloud.google.com/vertex-ai/generative-ai/docs/quotas ）。

**商用利用** [B]: Google Cloud の「Generative AI Preview Products」追加規約では、preview 製品の商用・本番利用は書面許可がない限り不可。ただし Veo 3.1 Lite は除外リストに名前がある（https://cloud.google.com/terms/genai-preview-products ）。GA モデルは通常の Cloud 規約に従う。C2PA Content Credentials への対応が記載されている。

### 4.3 Runway API

**公式 API**: あり。ドキュメントは https://docs.dev.runwayml.com/ 。リクエスト形状はすべて公式 Node SDK `@runwayml/sdk@4.21.0`（2026-10-02）の型定義とクライアント実装で確認した [A]。

**認証** [A]:

- `Authorization: Bearer <API key>`
- `X-Runway-Version: 2024-11-06`（SDK の既定値。省略不可のバージョンヘッダ）
- ベース URL は `https://api.dev.runwayml.com`。

**テキスト→動画** [A]:

```http
POST https://api.dev.runwayml.com/v1/text_to_video
Authorization: Bearer $RUNWAY_API_KEY
X-Runway-Version: 2024-11-06
Content-Type: application/json

{"model": "gen4.5",
 "promptText": "…(1〜1000 文字。UTF-16 単位)…",
 "ratio": "720:1280",            // gen4.5 T2V は "1280:720" | "720:1280" のみ
 "duration": 5,                  // 必須。2〜10 の整数
 "seed": 1234,                   // 任意（整数）
 "contentModeration": {"publicFigureThreshold": "auto"}   // 任意。"low" は著名人判定を緩める → 本アプリでは使わない
}
→ 200 {"id": "<task uuid>", "estimatedCost": {"credits": 60}}
```

**画像→動画** [A]:

```http
POST https://api.dev.runwayml.com/v1/image_to_video
{"model": "gen4_turbo",                       // gen4_turbo は I2V 専用（promptImage 必須）
 "promptImage": "https://… | runway://… | data:image/png;base64,…",   // または [{"uri": "…", "position": "first"}]
 "promptText": "…",                           // gen4_turbo では任意、gen4.5 では必須
 "ratio": "720:1280",                         // "1280:720"|"720:1280"|"1104:832"|"832:1104"|"960:960"|"1584:672"
 "duration": 5,                               // gen4_turbo は任意（2〜10 [B]）、gen4.5 は必須（2〜10）
 "seed": 1234}
```

- 画像入力は HTTPS URL、Runway のアップロード URI、data URI（5MB まで）のいずれか [A]。
- 一時アップロードは `POST /v1/uploads`（multipart）で行い、返る `uri` を使う [A]。
- Runway 経由で他社モデルも同じエンドポイントから呼べる [A]。例: `{"model":"veo3.1_fast","promptText":…,"ratio":"720:1280"|"1080:1920","duration":4|6|8,"audio":false,"negativePrompt":…,"seed":…}`。
- 他に指定できるモデル: `veo3.1`、`seedance2`、`seedance2_fast`、`seedance2_mini`、`seedance2_5`、`hailuo3`、`wan3`、`wan3_prime`、`grok_imagine_1_5`、`grok_imagine_1_5_lite`、`gemini_omni_flash`、`gemini_omni_flash_1.1`、`happyhorse_1_0`、`h3_max` など [A]。
- `gen3a_turbo` と `gen4_aleph` は 2026-07-30 に Runway Dev で提供終了 [B]。

**ポーリング** [A]:

```http
GET https://api.dev.runwayml.com/v1/tasks/{id}     （同じヘッダ）
```

`status` で分岐する。

| status | 付随するフィールド | 本アプリでの扱い |
|---|---|---|
| `PENDING` / `THROTTLED` | `estimatedCost.credits` | 待機を続ける。`THROTTLED` は同時実行の上限超過で待機中 |
| `RUNNING` | `progress`（0〜1）、`estimatedCost.credits` | 待機を続け、進捗を通知 |
| `SUCCEEDED` | `output: string[]`（動画 URL）、`cost.credits` | `output[0]` をダウンロード。**URL は 24〜48 時間で失効**するので、ダウンロードに失敗したらタスクを再取得し、新しい URL で取り直す（タスクは再送信しない） |
| `FAILED` | `failure`（人向けの文）、`failureCode?`、`cost.credits` | 再試行可否を判定（下記） |
| `CANCELLED` | `cost.credits` | キャンセル済み |

- キャンセルと削除は `DELETE /v1/tasks/{id}`（実行中ならキャンセル、終了済みなら削除）[A]。
- 同じタスクを 5 秒未満の間隔で取得しても更新されない [A]。
- 失敗コードの扱い [B]（https://docs.dev.runwayml.com/errors/task-failures/ ）:
  - 再試行可: `INTERNAL*` と `null`（遅延を入れて再試行）、`INTERNAL.BAD_OUTPUT.*`（プロンプト修正で成功し得る。入力内の文字やロゴ、文字生成の要求が原因になりやすい）。
  - 再試行不可: `ASSET.INVALID`、`SAFETY.INPUT.*` / `SAFETY.OUTPUT.*`。モデレーションに頻繁に引っかかるとアカウント停止の警告がある。

**レート制限** [B]（https://docs.dev.runwayml.com/usage/tiers/ ）:

- 上限はプロジェクトごと、モデル（カテゴリ）ごとに、同時実行数と 24 時間ローリングの生成数で決まる。
- Tier 1 は月間支出上限 $100。同時実行は 1〜2、1 日の生成数は 50〜200（カテゴリ別。表の列名は取得できず）。$50 購入で Tier 2 に自動昇格。
- 毎分のリクエスト数に上限はない。同時実行の超過は `THROTTLED` でキューに入る。1 日の上限超過は作成時に **429**。
- 400 応答には問題のフィールドと理由が含まれる。
- 自組織の上限は `GET /v1/organization` の `tier.models[model].maxConcurrentGenerations` と `maxDailyGenerations` で取得できる（`null` は無制限）[A]。

**価格** [B]（https://docs.dev.runwayml.com/guides/pricing/ 、as of 2026-10-11。1 クレジット = $0.01、最初の購入は $10 以上）:

| モデル | クレジット/秒 | USD/秒（= クレジット × $0.01） |
|---|---|---|
| `gen4.5` | 12 | $0.12 |
| `gen4_turbo` | 5（1 回最低 10 クレジット） | $0.05 |
| `veo3.1`（音声あり / 無音） | 40 / 20 | $0.40 / $0.20 |
| `veo3.1_fast`（音声あり / 無音） | 15 / 10 | $0.15 / $0.10 |
| `seedance2`（480p・720p / 1080p / 4K） | 36 / 40 / 150 | $0.36 / $0.40 / $1.50 |
| `seedance2_mini` | 16（最低 64） | $0.16 |

- mp4 以外の出力形式は追加料金がかかる（ProRes は +5 クレジット/秒など）[A]。
- 開発者向けモデルカタログの $/秒の表示は料金表と一致しない箇所があるという指摘がある [B]。料金表の値を優先する。

**商用利用・権利・透かし** [B]:

- 出力の所有権は利用者にある。Runway は商用利用を制限しない（https://runwayml.com/terms-of-use 、https://dev.runwayml.com/learn/commercial-use ）。
- 肖像権、商標、第三者素材の権利処理は利用者の責任。
- 他の利用者に似た出力が生成される可能性がある。
- API を組み込んだ製品のエンドユーザーには、同等以上の規約に同意させる必要がある。
- Enterprise 契約では学習に利用しないことと IP 補償が付く。
- API 利用時は UI に **"Powered by Runway"** の表記と runwayml.com へのリンクを求めている（https://docs.dev.runwayml.com/usage/attribution/ 。必須かどうかの最終確認は §6）。本アプリは runwayml.com へのリンク付きで表示する: Runway のプロバイダーカード（カード自体がラジオボタンのためテキストのみ）と選択時の詳細、Runway で生成したジョブの設定欄・動画一覧・プレビュー、Runway の素材（素材カード・プレビュー・差し替え候補・エディター）、設定画面のプロバイダー一覧。
- 全生成物に不可視透かしと C2PA を付与すると、2 年前の研究ページに記載がある（https://runwayml.com/research/foundations-for-safe-generative-media ）。

### 4.4 Kling AI API

**公式 API**: あり（https://kling.ai/document-api ）。ページ本体は開けなかったため、以下は検索抜粋による [B]。**2026-06-17 に新 API が公開され、仕様が移行中**。

**ホストと認証** [B]（https://kling.ai/document-api/api/get-started/authentication 、https://kling.ai/document-api/updates/api ）:

- ホストは `https://api-singapore.klingai.com`（中国国外のサーバー向け。旧 `https://api.klingai.com` から変更）。
- **新 API は API キーのみ**: `Authorization: Bearer <API Key>`。キーはコンソールで一度だけ表示される。
- 旧 API の AK/SK も引き続き有効。ただし新モデルには対応しない。旧 API は「当面廃止予定なし」（2026-07-15 の告知）。
- 旧方式のトークンは、AK を `iss` に入れ、`exp = now + 1800`、`nbf = now - 5` 程度にして SK で HS256 署名した JWT を `Authorization: Bearer <JWT>` で送る。

**新 API のテキスト→動画** [B]（https://kling.ai/document-api/apiReference/model/textToVideo ）:

```http
POST https://api-singapore.klingai.com/text-to-video/kling-3.0
Authorization: Bearer $KLING_API_KEY
Content-Type: application/json

{"prompt": "…(最大 3072 文字、推奨 2500 以下。ネガティブ指定も本文に含める)…",
 "settings": {"resolution": "720p",        // "720p"(既定) | "1080p" | "4k"
              "aspect_ratio": "9:16",      // "16:9"(既定) | "9:16" | "1:1"
              "duration": 5,               // 3〜15 の整数（既定 5）
              "audio": "off",              // "off"(既定) | "native"
              "multi_shot": false},        // 既定 true → 1 クリップ内にカットが入るので false を明示
 "options": {"external_task_id": "<クリップ ID。アカウント内で一意>",
             "callback_url": "https://…(任意)",
             "watermark_info": {"enabled": false}}}
→ {"code": 0, "message": "…", "request_id": "…",
   "data": {"id": "<task id>", "status": "submitted", "create_time": <ms>, "update_time": <ms>, "external_id": "…"}}
```

**新 API の画像→動画** [B]（https://kling.ai/document-api/api/video/3-0-omni/image-to-video ）:

- `POST /image-to-video/kling-3.0` に、同じ `prompt` / `settings` / `options` と `"contents": [{"type": "first_frame", "url": "<URL か Base64>"}]` を付けて送る。最終フレームは `"last_frame"` で指定するが、最終フレームだけの指定は不可。
- 画像の条件: JPG / PNG、50MB 以下、辺の長さ 300px 以上、縦横比 1:2.5〜2.5:1。

**新 API のポーリング** [B]:

```http
GET https://api-singapore.klingai.com/tasks?external_task_ids=<id>    // 公式例。task_ids= も使えると記載（パラメータ名は未確認）
→ {"code": 0, "data": [{"id": "…", "status": "submitted|processing|succeeded|failed",
                        "message": "<失敗理由>", "external_id": "…",
                        "outputs": [{"type": "video", "id": "…", "url": "…", "watermark_url": "…", "duration": "5.0"}]}]}
```

- task ID とカスタム ID の同時指定は不可。カンマ区切りで複数件を一括取得できる。
- 期間・状態での一覧は `POST /tasks`（カーソル型ページング）。
- **出力は 30 日で削除される**。

**旧 API**（AK/SK + JWT）[B + C]:

- 作成は `POST /v1/videos/text2video` と `POST /v1/videos/image2video`。照会は `GET /v1/videos/text2video/{task_id}`。
- ボディ [C]: `model_name`（例 `kling-v2-5-turbo`、`kling-v2-6`、`kling-v3`）、`prompt`、`negative_prompt`（各 2500 文字以内 [B]）、`cfg_scale`、`mode: "std"|"pro"`、`aspect_ratio: "9:16"`、`duration: "5"|"10"`、`callback_url`、`external_task_id`。I2V は `image`（URL か Base64）。
- 応答は `data.task_id` と `data.task_status`。値は **`submitted|processing|succeed|failed`**（新 API の `succeeded` と綴りが違う）[B]。
- 結果は `data.task_result.videos[].{id,url,watermark_url,duration}` [B]。
- 実装時は両方の綴りを成功として扱う。

**モデル** [B]（https://kling.ai/document-api/apiReference/model/videoModels ）:

- `kling-v3`（Kling 3.0）、`kling-v2-6`（音声同時生成。旧 API の既定）、`kling-v2-5-turbo` が掲載されている。
- 新 API のパスは `…/kling-3.0`。中国語の更新履歴では、2026-06-17 の公開モデルを「3.0 Turbo」と呼んでいる。

**価格** [B]（https://kling.ai/dev/pricing 、https://kling.ai/document-api/pricing/base/video 。1 unit = $0.14 定価）:

- パック:
  - 5,000 units のパックが $700。有効期限 180 日、繰り越し不可、**同時実行 20**。
  - 30,000 units 以上のパックは $0.126/unit。
  - お試しパックは 100 units で $9.8（定価 $14。初回のみ 30% 引き、最大 5 回）。有効期限 30 日、**同時実行 5**（https://klingai.com/global/dev/model/tryon ）。
- 秒単価（USD/秒 = units/秒 × $0.14）:

| モデル | 720p | 1080p | 4k |
|---|---|---|---|
| 新 API（2026-06-17 告知、T2V / I2V） | 0.8 u = $0.112 | 1.0 u = $0.14 | – |
| Kling 3.0 Turbo（ネイティブ音声） | 0.8 u = $0.112 | 1.0 u = $0.14 | – |
| Kling 3.0（無音 / 音声あり） | 0.6 u = $0.084 / 0.9 u = $0.126 | 0.8 u = $0.112 / 1.2 u = $0.168 | 3.0 u = $0.42 |
| Kling 2.5 Turbo（無音） | 0.3 u = $0.042 | 0.5 u = $0.07 | – |

- 2.5 Turbo の std モードは 5 秒で 1.5 units、10 秒で 3 units と告知されており、0.3 u/秒と一致する。
- 新 API の `kling-3.0` がどの料金行に当たるかは確定できない（§6）。

**エラーとレート制限** [B]（https://kling.ai/document-api/api/get-started/error-codes ）:

- HTTP 429 のコード 1302 は「リクエスト過多」、1303 は「同時実行数か QPS がリソースパックの上限超過」。いずれも再試行可。
- 1102 は「リソースパックの枯渇か期限切れ」。再試行不可。
- SLA は上限超過による拒否を可用性の計算から除外している。

**商用利用・権利・透かし**:

- [B] API の結果には透かしなしの `url` と、任意で生成する `watermark_url`（`options.watermark_info.enabled=true` のとき）がある（2026-01-23 の告知）。
- [B] 一般向けのマーケティングページには「生成物は商用利用可」とあるが、プランによって条件が違うように読める箇所もある。コミュニティガイドラインは他者の肖像権・プライバシーの尊重を求めている。
- [C] 「API 有償サービス利用規約」の本文（出力の所有権、実在人物の扱い）は取得できていない（§6）。

### 4.5 その他の公式 API（概観のみ。MVP では実装しない）

| サービス | 公式 API | 2026-10 時点の状況・要点 | 出典 |
|---|---|---|---|
| **OpenAI Sora**（`sora-2`, `sora-2-pro`） | あった | **2026-09-24 に API を完全終了**。後継の推奨もない。アプリと Web は 2026-04-26 に終了。最新 SDK `openai==3.28.0` にも `@deprecated("The Sora API is scheduled to permanently shut down on September 24, 2026.")` とある [A] | https://developers.openai.com/api/docs/deprecations [B] |
| **Luma**（Ray3.2、Luma Agents API） | ○ | Dream Machine と Ray2 は非推奨。現行は `ray-3.2`（2026-06）で 5 秒 / 10 秒。720p は 5 秒あたり約 $0.30（≈ $0.06/s）、1080p は 5 秒あたり約 $1.20（≈ $0.24/s）。9:16 は reframe 機能側にあり、詳細は未確認 [B] | https://docs.agents.lumalabs.ai/guides/pricing/ 、https://lumalabs.ai/llm-info |
| **MiniMax Hailuo** | ○ | `POST https://api.minimax.io/v1/video_generation`（Bearer）。1 本ごとの料金: Hailuo-2.3 は 768P/6 秒で $0.28（≈ $0.047/s）、768P/10 秒で $0.56、1080P/6 秒で $0.49。2.3-Fast は 768P/6 秒で $0.19。新しい MiniMax-H3 は 768P で $0.08/s。失敗した生成と審査で弾かれた生成は課金されない。照会 API（`file_id` を経てダウンロード）は未確認 [B]。Runway 経由でも `hailuo3` / `h3_max` として呼べる [A] | https://platform.minimax.io/docs/pricing/overview |
| **ByteDance Seedance**（BytePlus ModelArk） | ○ | 非同期タスク方式（作成してから ID で照会）。Seedance 1.0 Pro は出力 100 万トークンあたり $2.5（トークン数 = 幅 × 高さ × fps × 秒 / 1024）。2.5 はトークンパック制で 30 秒の単発生成に対応。9:16 の単価表と 2.x のトークン単価は未確認 [B]。Runway 経由で `seedance2*` として呼べる [A] | https://docs.byteplus.com/en/docs/ModelArk/1587798 、https://www.byteplus.com/en/product/seedance |
| **Alibaba Wan**（Model Studio） | ○ | wan2.6 / 2.7-t2v は 720P で $0.10/s、1080P で $0.15/s（国際リージョン）。wan3.0-video-prime は $0.14 / $0.28 /s で最長 30 秒。入力は無料、失敗は課金されない。非同期タスク方式と推定 [B]。Runway 経由で `wan3` / `wan3_prime` [A] | https://www.alibabacloud.com/help/en/model-studio/model-pricing |
| **Pika** | 不明確 | Pika 2.2 の API は fal.ai 上で提供（fal の公式ブログ）。2026-08 に「API Club」を発表（プレスリリース）。ファーストパーティの直接 API としての仕様・規約は確認できなかったため除外 [B] | https://blog.fal.ai/pika-api-is-now-powered-by-fal/ |

---

## 5. 商用利用・権利・透かしのまとめ（権利チェックリスト `model_terms_checked` 用）

| 観点 | Veo（Gemini API / Vertex） | Runway | Kling |
|---|---|---|---|
| 出力の所有権 | Google は主張しない [B] | 利用者 [B] | 未確認 [C] |
| 商用利用 | 可。Vertex の preview は原則として本番不可、ただし Veo 3.1 Lite は例外 [B] | 可 [B] | 可とする記載あり（規約本文は未確認）[B][C] |
| 実在人物・著名人 | `personGeneration` で制御。禁止使用ポリシーあり [B] | 肖像権の処理は利用者責任。`publicFigureThreshold` で著名人判定を調整できる（本アプリは既定の `auto` 固定）[A][B] | ガイドラインで肖像権の尊重を要求 [B] |
| 透かし・来歴 | SynthID（不可視）、Vertex は C2PA [B] | 不可視透かしと C2PA [B（古い記事）] | 既定は透かしなし、`watermark_url` は任意 [B] |
| 帰属表示 | 不要とみられる [C] | UI に "Powered by Runway" を表示 [B]（本アプリは表示済み） | 未確認 [C] |

いずれの不可視透かし・メタデータも、本アプリの FFmpeg 再エンコードで**メタデータは失われる可能性が高い**。SynthID のような画素に埋め込む透かしは残る可能性がある [C]。Instagram（Meta）の「AI 情報」ラベルが自動で付くかどうかはこの影響を受けるため、投稿者が手動で AI 生成を表示する運用を推奨する [C]。

---

## 6. 未確認事項

1. **Kling の新 API の詳細**:
   - `GET /tasks` の task ID 用クエリ名（`task_ids`?）。公式例で確認できたのは `external_task_ids` だけ。
   - 応答の `outputs[].duration` の型。
   - `kling-3.0` で seed とネガティブプロンプトが使えないこと。
   - 新 API の `kling-3.0` がどの料金行（3.0 Turbo の 0.8 / 1.0 u/s か、3.0 無音の 0.6 / 0.8 u/s か）に当たるか。
   - `settings.audio` の既定 `off` と、Turbo の「ネイティブ音声」課金との関係。
2. **Kling 旧 API のボディ**: `text2video` の `cfg_scale` とモデルごとの `duration` 選択肢は、2026 年版の公式ページでは確認できていない（記述は以前の知識による）。
3. **Kling の API 有償サービス利用規約**: 出力の所有権、商用利用、実在人物に関する本文。
4. **Gemini API の Veo のレート上限**: AI Studio に表示される RPM / RPD の実数（フォーラムでは Tier 1 で 2 RPM / 10 RPD）。Tier 1 で 429 が多発する既知の問題が解消されたかどうか。
5. **Gemini API の `personGeneration`**: T2V の `"allow_all"` と、EU 等で `"allow_adult"` のみという制約が矛盾しないか。運用リージョン（日本）からの T2V で人物が拒否されないか。
6. **Veo の `durationSeconds` の型**: SDK は整数、ドキュメント表は文字列。両方受け付けるかは実キーで確認する。
7. **Vertex の価格表の単位 "/ 1 count"**: 静的 HTML では単位が展開されていなかった。1 count = 1 秒と解釈したが、他の公開情報（Gemini API の $/s）とは一致している。
8. **Vertex の `storageUri` 省略時の挙動**（base64 で返るか）、express mode（API キー）で Veo が使えるか。
9. **Runway**:
   - Tier 1 の同時実行数と 1 日の生成数がモデルカテゴリ別にどの値か（表の列名が取れなかった）。
   - `gen4_turbo` の `duration` の正確な選択肢（SDK の型は `number` のみ）。
   - "Powered by Runway" の表示が API 利用の必須条件かどうか。
   - API 出力にも C2PA と不可視透かしが現在も付くか。
10. **Runway のモデルカタログと料金表の不一致**: カタログ上の $/s が料金表と食い違う。
11. **出力の解像度**: Runway gen4.5 は `720:1280` でピクセル数どおりか。Kling と Veo の 9:16 720p は 720×1280 か（実ファイルで ffprobe して確認する）。
12. **生成にかかる時間**（作成から完了まで）: どの社も公式値を取得できていない。タイムアウト 900 秒が妥当かは実測する。
13. **Instagram の AI ラベル**: 再エンコード後に SynthID や C2PA が検出されるか。
14. **その他**: Luma の 9:16 の指定方法、MiniMax の照会 API の形状、Seedance 2.x のトークン単価と 9:16 の扱い。いずれも MVP の対象外。

---

## 7. 出典一覧（取得・確認日 2026-10-11）

**[A] 直接取得（一次確認）**

- Runway Node SDK 4.21.0（2026-10-02）: https://registry.npmjs.org/@runwayml/sdk/-/sdk-4.21.0.tgz 。主に `client.js`（ベース URL、`X-Runway-Version`、Bearer）、`resources/{text-to-video,image-to-video,tasks,uploads,organization}.d.ts`、`lib/polling.js` を読んだ。リポジトリは https://github.com/runwayml/sdk-node
- google-genai 2.29.0（2026-10-07）: https://pypi.org/project/google-genai/2.29.0/ 。`models.py`（`_GenerateVideosConfig_to_mldev/_to_vertex`、`_GenerateVideosParameters_*`、`_Video_from_*`）、`operations.py`、`files.py`、`types.py`（`GenerateVideosConfig`）を読んだ。
- openai 3.28.0（2026-10-09）: https://pypi.org/project/openai/3.28.0/ 。`resources/videos.py` の deprecated 表示、`types/video_model.py` を読んだ。
- Gemini API の Discovery 文書（rev.20261010）: https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta
- Agent Platform（Vertex AI）API の Discovery 文書（rev.20261007）: https://aiplatform.googleapis.com/$discovery/rest?version=v1
- Vertex（Agent Platform）の価格: https://cloud.google.com/vertex-ai/generative-ai/pricing
- 実リクエスト: キーなしで `POST https://generativelanguage.googleapis.com/v1beta/models/veo-3.1-lite-generate-preview:predictLongRunning` を送り、`403 PERMISSION_DENIED`（"Method doesn't allow unregistered callers…"）を確認した。

**[B] 公式ページ（検索抜粋で確認）**

- Google:
  - https://ai.google.dev/gemini-api/docs/veo
  - https://ai.google.dev/gemini-api/docs/pricing
  - https://ai.google.dev/gemini-api/docs/rate-limits
  - https://ai.google.dev/gemini-api/docs/deprecations
  - https://ai.google.dev/gemini-api/docs/changelog
  - https://ai.google.dev/gemini-api/docs/models/veo-3.1-lite-generate-preview
  - https://ai.google.dev/gemini-api/terms
  - https://blog.google/innovation-and-ai/technology/ai/veo-3-1-lite/
  - https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/veo/3-1-generate
  - https://docs.cloud.google.com/vertex-ai/generative-ai/docs/quotas
  - https://cloud.google.com/terms/genai-preview-products
- Runway:
  - https://docs.dev.runwayml.com/guides/pricing/
  - https://docs.dev.runwayml.com/usage/tiers/
  - https://docs.dev.runwayml.com/guides/models/
  - https://docs.dev.runwayml.com/api-details/api_changelog/
  - https://docs.dev.runwayml.com/errors/task-failures/
  - https://docs.dev.runwayml.com/errors/errors/
  - https://docs.dev.runwayml.com/usage/attribution/
  - https://docs.dev.runwayml.com/api-details/moderation/
  - https://dev.runwayml.com/learn/commercial-use
  - https://runwayml.com/terms-of-use
  - https://help.runwayml.com/hc/en-us/articles/18927776141715-Usage-rights
  - https://runwayml.com/research/foundations-for-safe-generative-media
- Kling:
  - https://kling.ai/document-api/apiReference/model/textToVideo
  - https://kling.ai/document-api/api/video/3-0-omni/image-to-video
  - https://kling.ai/document-api/api/get-started/authentication
  - https://kling.ai/document-api/updates/api
  - https://kling.ai/document-api/api/get-started/error-codes
  - https://kling.ai/document-api/apiReference/model/videoModels
  - https://kling.ai/document-api/api/video/motion-control/legacy
  - https://kling.ai/dev/pricing
  - https://kling.ai/document-api/pricing/base/video
  - https://klingai.com/global/dev/model/tryon
  - https://app.klingai.com/global/docs/community-policy
- その他:
  - https://developers.openai.com/api/docs/deprecations
  - https://docs.agents.lumalabs.ai/guides/pricing/
  - https://lumalabs.ai/llm-info
  - https://platform.minimax.io/docs/pricing/overview
  - https://platform.minimax.io/docs/api-reference/video-generation-t2v
  - https://docs.byteplus.com/en/docs/ModelArk/1587798
  - https://www.byteplus.com/en/product/seedance
  - https://www.alibabacloud.com/help/en/model-studio/model-pricing
  - https://blog.fal.ai/pika-api-is-now-powered-by-fal/
- フォーラム（参考。公式の記述ではない）:
  - https://discuss.ai.google.dev/t/veo-3-1-lite-fast-429-resource-exhausted-for-48h-daily-quota-not-resetting-on-tier-1/172205
  - https://discuss.ai.google.dev/t/veo-3-1-image-to-video-rejects-documented-persongeneration-allow-adult/181139

---

## 8. OpenRouter（動画・Claude）

- 調査日: **2026-10-12**
- 位置づけ: OpenRouter は、複数社の AI モデルを 1 本の API キーと 1 つの請求先で呼べる集約（ルーター）API。動画生成（`/videos`）と LLM（`/chat/completions`）の両方がある。モデルそのものは各提供元（Google、Kuaishou、MiniMax、ByteDance、Alibaba、Anthropic など）が動かし、OpenRouter はリクエストを中継する。
- 本アプリでの使い方（利用者の決定）: 動画生成と Claude（絵コンテ・翻訳）の両方に使う。LLM は次の順で選ぶ。
  1. `ANTHROPIC_API_KEY` がある: Anthropic に直接送る（既存のコード。挙動は変えない）。
  2. なく、`OPENROUTER_API_KEY` がある: OpenRouter 経由で Claude に送る。
  3. どちらもない: テンプレートで作る（外部に送らない）。
- 調査時（2026-10-12）は、このサンドボックスから `openrouter.ai` に接続できなかった（WebFetch は DNS 解決失敗、curl はプロキシの 403）。以下の形状は公式 SDK のソースと Web 検索の抜粋から組み立てたもの。2026-10-13 に無料の `GET /videos/models` だけを実際に取得し、モデル一覧と `pricing_skus` の形を確認した（§8.3）。動画の生成（有料）は一度も送っておらず、生成のテストは respx のモックだけになる。OpenRouter 関連のモデル・料金はすべて `verified: false` として扱う。

### 8.0 情報源と信頼度

§0 冒頭のラベルを次の意味で使う。

| ラベル | この節での意味 |
|---|---|
| **[A]** | 公式 SDK のソースを本セッションで読んだ。主に npm `@openrouter/sdk@1.4.25`（2026-10-06 公開、作者 OpenRouter、Apache-2.0）の zod スキーマと `esm/funcs/*.js`。補助として PyPI `openrouter==1.3.43`（2026-10-11 公開、sha256 `1e549644…25ce` を確認）の `video_generation.py` と `components/*.py`。SDK はどちらも Speakeasy で同じ OpenAPI から生成されていて、内容は一致している。 |
| **[B]** | `openrouter.ai` のページを検索エンジンの抜粋で読んだ。ページ本体は開けていない。 |
| **[C]** | ミラーサイト・第三者の記事・推定。**未検証**。 |

SDK のファイルパスは `esm/` を起点にする（例: `esm/models/videogenerationrequest.js`）。

### 8.1 共通（認証・ヘッダ・エラー）

- ベース URL: `https://openrouter.ai/api/v1` [A]（`esm/lib/config.js` の `ServerList`。SDK では環境変数 `OPENROUTER_BASE_URL` で上書きできる）。
- 認証: `Authorization: Bearer <OPENROUTER_API_KEY>` [A]（`esm/lib/security.js`）。
- 任意のヘッダ [A]（`esm/funcs/*.js`）。いずれもアプリの識別とランキング用で、動作には影響しない。
  - `HTTP-Referer`: アプリの URL
  - `X-OpenRouter-Title`: 表示名
  - `X-OpenRouter-Categories`: カンマ区切りのカテゴリ
  - 本アプリでは `X-OpenRouter-Title: AI Music Video Factory` だけを送れば足りる [C]。
- エラーの本文 [A]（`esm/models/errors/*.js`、`esm/models/*responseerrordata.js`）:

  ```json
  {"error": {"code": 402, "message": "…", "metadata": {"…": "…"}},
   "user_id": "…",
   "openrouter_metadata": {"…": "…"}}
  ```

  `error.metadata`、`user_id`、`openrouter_metadata` は省略または null のことがある。

- SDK が型を持つエラーステータス [A]:

  | ステータス | SDK の説明 | 本アプリでの扱い [C] |
  |---|---|---|
  | 400 | 不正なパラメータ | 再試行不可 |
  | 401 | 認証なし・無効なキー | `ProviderNotConfigured` 相当 |
  | 402 | クレジット・クォータ不足 | 再試行不可。「OpenRouter のクレジットが不足しています」と表示 |
  | 403 | 権限不足（ガードレール・モデレーションでの拒否を含む [B]） | 再試行不可 |
  | 404 | リソースなし | 再試行不可 |
  | 408 | タイムアウト | 再試行可 |
  | 409 / 410 | 競合 / 削除済み（動画のダウンロードのみ） | §8.2 を参照 |
  | 413 | ペイロードが大きすぎる | 再試行不可 |
  | 422 | 意味的な検証エラー | 再試行不可 |
  | 429 | レート制限 | 再試行可。`Retry-After` に従う |
  | 500 / 502 / 503 | サーバー・上流の障害。503 は「ルーティング条件を満たす提供元がない」場合も含む [B] | Claude: 再試行可（503 で条件不一致の場合を除く）。**`POST /videos` は 503 だけ再試行可**、500 / 502 は再送しない（下の注） |
  | 524 / 529 | エッジのタイムアウト / 提供元の過負荷 | Claude: 再試行可。**`POST /videos` は 529 だけ再試行可**、524 は再送しない（下の注） |

  注（実装、2026-10-12）: `POST /videos` には冪等キーがないため、ジョブが作られていないと言える失敗だけを再試行する。
  再試行するのは、接続前のエラー（接続できない・接続のタイムアウト・プロキシ）と 408 / 425 / 429 / 503 / 529。
  読み取り・書き込みのタイムアウト、途中で切れた接続、それ以外の 5xx（500 / 502 / 504 / 524 など）、読み取れない 2xx の応答は
  「ジョブが作られた可能性がある」として再送せず、見積もり額を課金済み（推定）として記録する（§8.10）。

- SDK 既定の再試行 [A]（`esm/lib/retries.js`、各 `funcs`）:
  - 5XX と接続エラーを指数バックオフで再試行する（初期 0.5 秒、指数 1.5、最大間隔 60 秒、合計 1 時間まで）。
  - `retry-after-ms` と `Retry-After` を尊重する。
  - **`POST /videos` も再試行の対象になっている**。API に冪等キーはないため、ジョブ作成後の 5xx やタイムアウトで再送すると二重に課金されうる。本アプリは SDK を使わず、既存アダプタと同じ方針（作成前の失敗だけ再試行可、作成後はジョブ ID で再開）をとる [C]。

### 8.2 動画 API（`/videos`）

**エンドポイント** [A]（`esm/funcs/videoGeneration*.js`）:

| 操作 | メソッドとパス | 成功時 | SDK が型を持つエラー |
|---|---|---|---|
| 作成 | `POST /videos`（`Content-Type: application/json`） | **202** と `VideoGenerationResponse` | 400 401 402 403 404 413 429 500 |
| 状態取得 | `GET /videos/{jobId}` | 200 と `VideoGenerationResponse` | 401 403 404 500 |
| ダウンロード | `GET /videos/{jobId}/content?index=0`（`Accept: video/mp4`） | 200、`video/mp4` のストリーム（説明は「上流の提供元から動画をストリームする」） | 400 401 403 404 409 410 500 502 |
| モデル一覧 | `GET /videos/models` | 200 `{"data": [VideoModel, …]}` | 400 500 |

- **キャンセル API はない**（SDK に `DELETE` などの操作がない）[A]。`supports_remote_cancel = False` とし、Veo / Kling と同じく、放棄したジョブは課金されたものとして表の単価で計上する。

**作成リクエスト** [A]（`esm/models/videogenerationrequest.js` の outbound スキーマ。Python 版の `components/videogenerationrequest.py` も同じ）。ワイヤ上の名前はスネークケース。

| フィールド | 型・値 | 説明 |
|---|---|---|
| `model` | string（必須） | 例 `google/veo-3.1-lite` |
| `prompt` | string | 画像だけで生成できるモデル以外では必須 |
| `duration` | **整数**（秒） | |
| `aspect_ratio` | `16:9` `9:16` `1:1` `4:3` `3:4` `3:2` `2:3` `21:9` `9:21` | |
| `resolution` | `360p` `480p` `720p` `768p` `1080p` `1K` `2K` `4K` | |
| `size` | `"WIDTHxHEIGHT"`（例 `720x1280`） | `resolution` と `aspect_ratio` の組の代わりに使える |
| `generate_audio` | bool | 省略時はモデルの `generate_audio` フラグに従う（フラグがなければ false） |
| `seed` | 整数 | 決定性はすべての提供元で保証されるわけではない |
| `frame_images` | `[{"type":"image_url","image_url":{"url":"…"},"frame_type":"first_frame"\|"last_frame"}]` | 画像→動画 |
| `input_references` | `image_url` / 音声 / 動画のコンテンツパーツの配列 | 参照素材。音声・動画は対応する提供元だけが使う |
| `callback_url` | HTTPS URL | 完了時の Webhook。ワークスペース既定の URL を上書きする |
| `provider` | `{"options": {"<提供元スラッグ>": {…}}}` | **提供元ごとのパススルーだけ**。一致した提供元の分だけが転送され、未知のキーは黙って捨てられる |
| `user` / `session_id` / `trace` | string / string（256 文字以下）/ object | 観測用。提供元には送られない |
| `previous_job_id` | string | 完了済みジョブの編集・延長（対応モデルのみ） |
| `creativity` / `upscale_factor` | 整数 / 数値 | アップスケーラー専用。生成モデルでは使えない |

本アプリの推奨形 [C]:

```http
POST https://openrouter.ai/api/v1/videos
Authorization: Bearer $OPENROUTER_API_KEY
Content-Type: application/json
X-OpenRouter-Title: AI Music Video Factory

{"model": "google/veo-3.1-lite",
 "prompt": "…sanitize_prompt 済みの英語プロンプト… Avoid: text, captions, watermark, …",
 "duration": 6,
 "aspect_ratio": "9:16",
 "resolution": "720p",
 "generate_audio": false}
→ 202 {"id": "gen-vid-<timestamp>-<英数字 20 文字>", "polling_url": "…", "status": "pending"}
```

- ネガティブプロンプトの欄はない。Kling の新 API と同様に、本文の末尾に `Avoid: …` として足す [C]。
- `seed` は、モデル一覧で `seed: true` のモデルにだけ送る [C]。
- 生成音声は使わないため `generate_audio: false` を明示する。モデルによっては無音の単価のほうが安い（§8.3）[C]。
- `size` と `resolution` + `aspect_ratio` の両方を送らない [C]。
- `callback_url` は使わない（本アプリには公開 URL がなく、ポーリングで足りる）。Webhook の署名方式も未確認。

**応答 `VideoGenerationResponse`** [A]（`esm/models/videogenerationresponse.js`、Python 版の `components/videogenerationresponse.py`）:

| フィールド | 型 | 説明 |
|---|---|---|
| `id` | string | ジョブ ID（`gen-vid-<timestamp>-<20 alphanumerics>`） |
| `polling_url` | string | 状態取得の URL |
| `status` | `pending` `in_progress` `completed` `failed` `cancelled` `expired` | 未知の値も来うる（open enum） |
| `error` | string? | 失敗理由（文字列） |
| `unsigned_urls` | string[]? | 完了後の取得先 |
| `usage` | `{"cost": number\|null, "is_byok": bool}`? | **USD の実費**。完了後に入る |
| `generation_id` | string? | 処理後に入る生成 ID |
| `expires_at` | 整数?（Unix 秒） | OpenRouter が保存した出力のコピーが消える時刻。Python SDK 1.3.43 にだけある（TS 1.4.25 にはない） |

**ポーリングと状態の扱い** [C]（既存アダプタの方針に合わせた設計判断）:

- `GET {ベース URL}/videos/{id}` を、id から自分で組み立てて呼ぶ。`polling_url` は、API と同じオリジンのときだけ使う（キーを他のホストに送らないため）。
- 間隔は既存と同じ（5 秒から始めて最大 30 秒、ジッタ ±20%、`timeout=900`）。
- 状態ごとの扱い:

  | status | 扱い |
  |---|---|
  | `pending` / `in_progress` | 待機を続ける |
  | `completed` | ダウンロードへ進む |
  | `failed` | `ProviderTaskError`（`error` の文字列を付ける）。課金されたかどうかは不明（§8.6）。`usage.cost` があればそれを計上する |
  | `cancelled` / `expired` | 再試行不可の `ProviderTaskError` |

- `resume_token` はジョブ ID そのものでよい。

**ダウンロード**:

- 第一候補は `GET /videos/{id}/content?index=0`（`Authorization` 付き、`Accept: video/mp4`）[A]。
  - 409 は「まだ取得できない」と解釈し、タイムアウトの範囲内で取り直す [C]。
  - 410 は「出力が消えた」と解釈し、再試行不可にする [C]。
- 代替は `unsigned_urls[0]`。
  - 公式の例には、`openrouter.ai` 上の URL（認証が要る）と外部ストレージの URL（認証不要）の両方がある [B]。
  - **キーは URL のオリジンが API と同じときだけ付ける**。リダイレクト先には付けない（既存の `ApiVideoProvider` と同じ）[C]。
- 成功したジョブは課金済みなので再送信しない。取得だけを `OUTPUT_FETCH_ATTEMPTS` 回までやり直す（§2.2）。

**費用**: 完了時の `usage.cost`（USD）を実額として `ClipResult.cost_usd` に使う [A]。null の場合は、モデル一覧の単価または表の単価 × 秒数で計上する [C]。

### 8.3 モデル一覧（`GET /videos/models`）と料金

**`VideoModel` の形** [A]（`esm/models/videomodel.js`）:

```json
{"id": "google/veo-3.1-lite", "name": "…", "canonical_slug": "…", "created": 1776902400,
 "description": "…", "hugging_face_id": null,
 "supported_durations": [4, 6, 8],
 "supported_aspect_ratios": ["16:9", "9:16"],
 "supported_resolutions": ["720p", "1080p"],
 "supported_sizes": ["720x1280", "…"],
 "supported_frame_images": ["first_frame", "last_frame"],
 "generate_audio": true, "seed": false,
 "allowed_passthrough_parameters": ["…"],
 "pricing_skus": {"per-video-second": "0.05", "…": "…"},
 "creativity": null, "upscale_factor": null}
```

- 上の値は形の例で、実際の値ではない。
- `supported_*`、`generate_audio`、`seed`、`creativity`、`upscale_factor` は null がありうる。
- 認証が必要かどうかは不明。SDK はキーが設定されていれば付けて送る [A]。本アプリでも付けて送る [C]。

**`pricing_skus` の形**:

- 型は「SKU 名 → 価格の文字列」の辞書で、SDK の説明は「Pricing SKUs with provider prefix stripped, values as strings」[A]。
- 公式ドキュメントの例のキー `per-video-second` / `per-video-second-1080p` [B] は、実際の一覧には存在しない。
- **実際の一覧**（2026-10-13 に `GET /videos/models` を取得。30 モデル。`backend/tests/fixtures/openrouter_videos_models_2026-10-13.json`）で見られた SKU の系統と、本アプリでの扱い:

  | SKU 名 | 単位 | 扱い |
  |---|---|---|
  | `duration_seconds[_<解像度>]` | USD/秒 | 出力 1 秒の単価 |
  | `duration_seconds_with_audio[_<解像度>]` / `duration_seconds_without_audio[_<解像度>]` | USD/秒 | 音声あり・なしの単価 |
  | `text_to_video_duration_seconds_<解像度>` / `image_to_video_duration_seconds_<解像度>` | USD/秒 | テキスト→動画・画像→動画それぞれの単価 |
  | `cents_per_second_output[_<解像度>]` / `cents_per_video_output_second_<解像度>` | セント/秒 | 100 で割って USD/秒 |
  | `minimum_cents_per_generation` | セント/本 | 1 本あたりの最低料金。見積もりは `max(単価 × 秒数, 最低料金)` |
  | `cents_per_image_input` | セント/枚 | 入力画像 1 枚ごとの料金。画像→動画のときだけ加算 |
  | `reference_duration_seconds_<解像度>` / `reference_images` | — | 参照入力の料金。本アプリは参照入力を送らないため無視 |
  | `cents_per_second_video_continuation_<解像度>` | — | 既存動画の続きの生成。使わないため無視 |
  | `cents_per_megapixel_second_*` | — | アップスケーラー。候補に出さない |
  | `video_tokens*` | USD/トークン | 1 本あたりのトークン数が分からないため未対応。そのモデルは料金不明として選べない |

**USD/秒の求め方**（`price_from_skus`。[C]）:

1. 解像度: 要求する解像度（既定は 720p。なければ一覧が料金を載せている解像度を優先順に選ぶ）の SKU を使い、なければ解像度を名前に含まない SKU を使う。別の解像度の単価は使わない。`@1080p` の候補は、1080p 専用の SKU があるときだけ作る。
2. 生成方法: 入力画像がなければ `text_to_video_*`（と、生成方法を名前に含まない SKU）を使う。
3. 音声: `generate_audio: true` のモデルは常に音声をオフにして送るため、`without_audio` → 音声の記載なし → `with_audio` の順に選ぶ。音声をオフにできないモデル（`generate_audio` が false / null）は、記載なし → `with_audio` の順で、`without_audio` は使わない。音声ありの単価を使う場合は料金の注記にそう書く（無音の単価が解像度の指定なし・別の解像度・別の生成方法にしかない場合は、その SKU 名と、要求する解像度の単価を優先したことを書く）。
4. 同じ条件の候補が複数あれば、生成方法を名前に含むほうを優先し、さらに高いほうを採用する（見積もりを過小にしない）。
5. 上のどれにも当たらない（未知の系統だけ、トークン単位）場合は、推測せずに料金不明とし、そのモデルは選べない。未知の SKU や、使わない SKU の読めない値が既知の単価と並んでいる場合は、見積もりに含めていないことを注記する。
6. 使うはずの SKU（上の 1〜4 で選ばれる単価、最低料金、画像→動画の入力画像料金）の値が使えない場合も料金不明とし、ほかの SKU（解像度の指定がない単価や別の解像度）で代わりに見積もらない（安く見積もるおそれがあるため）。その解像度の料金が不明なら、別の解像度にも切り替えない。使えない値は次のとおり（注記に理由を書く）:
   - 数値として読めない値・負の値（例: `"$0.30"`）。
   - 1 秒あたり 0 の値（`"0"`・`"-0"` など）。無料とは確認できず、予算を確保できないため。最低料金・入力画像料金の 0 は「料金なし」として扱う。
   - 想定範囲の外の値。単位は SKU 名だけで決まるため、ドルとセントの取り違え（例: `cents_per_second_output: "0.12"` → $0.0012/秒、`duration_seconds: "12"` → $12/秒）を検出する。範囲は 1 秒あたり $0.005〜$3、最低料金 $0.01〜$20/本、入力画像 $0.0005〜$1/枚（実際の一覧は $0.02〜$0.60/秒・$0.56/本・$0.002〜$0.01/枚）。
7. 料金の注記には、使った SKU 名と元の単位を書く（例: `cents_per_second_output: 12¢ = $0.12/秒`）。
8. 実際の請求額は、完了時の `usage.cost` で置き換える。

**候補に出すモデル**: `supported_durations` が空でなく、`supported_aspect_ratios` に 9:16 があるモデルだけ。編集（`black-forest-labs/flux-video-edit`）・アップスケール（`black-forest-labs/flux-video-upscale`）・動画→動画（`runway/aleph-2`）・アバター（`heygen/avatar-iv`）のモデルは秒数の指定がなく、`minimax/hailuo-2.3` は 16:9 のみのため出さない。

**2026-10-13 の一覧から求めた単価**（テキスト→動画・無音・720p またはモデルの既定解像度。カッコ内は `@1080p`）:

| モデル | USD/秒 | 使う SKU |
|---|---|---|
| `google/veo-3.1-lite` | 0.03 | `duration_seconds_without_audio_720p` |
| `google/veo-3.1-fast` | 0.08 | `duration_seconds_without_audio_720p` |
| `google/veo-3.1` | 0.20 | `duration_seconds_without_audio` |
| `kwaivgi/kling-v3.0-std` / `-pro` | 0.084 / 0.112 | `text_to_video_duration_seconds_720p` |
| `kwaivgi/kling-video-o1` | 0.112 | `duration_seconds` |
| `runway/gen-4.5` | 0.12 | `cents_per_second_output`（12¢） |
| `x-ai/grok-imagine-video` | 0.07 | `cents_per_video_output_second_720p`（7¢） |
| `x-ai/grok-imagine-video-1.5` / `-1.5-lite` | 0.14（0.25）/ 0.03（0.14） | `cents_per_video_output_second_720p` / `_1080p` |
| `black-forest-labs/flux-3-video` | 0.17（0.29） | `cents_per_second_output_720p` / `_1080p` |
| `heygen/heygen-video-1` | 0.03（768p） | `duration_seconds_768p` |
| `minimax/hailuo-3-max` | 0.08（768p） | `duration_seconds_768p` |
| `minimax/hailuo-3` | 0.13（2K のみ） | `duration_seconds` |
| `alibaba/wan-2.6` | 0.08（0.12） | `text_to_video_duration_seconds_720p` / `_1080p` |
| `alibaba/wan-2.7` | 0.10 | `duration_seconds` |
| `alibaba/wan-3.0` / `wan-3.0-prime` | 0.10（0.20）/ 0.14（0.28） | `duration_seconds_720p` / `_1080p` |
| `alibaba/happyhorse-1.0` / `-1.1` | 0.0988（0.1694）/ 0.0988（0.1278） | `duration_seconds_720p` / `_1080p` |
| `bytedance/seedance-*`（5 モデル） | 不明（選べない） | `video_tokens*`（トークン単位） |

- 一覧を取得できないとき（キー未設定・通信エラー）は、この一覧の一部（Veo 3.1 Lite / Fast、Kling v3.0 std、Kling O1、Wan 2.7、Runway Gen-4.5、Grok Imagine Video）を写した組み込みの候補を、同じ規則で使う。

以下の「既知のモデル」と推奨値の表は、一覧を取得する前（2026-10-12）の検索による調査の記録として残す。単価は上の表が優先される。

**既知のモデル**（**検索による。未検証**。秒数と単価はモデルページの抜粋で、「from」付きの最低価格を含む）:

| モデル ID | 秒数 | USD/秒 | 根拠・備考 |
|---|---|---|---|
| `google/veo-3.1-lite` | 4 / 6 / 8 | 0.05〜 | [B] 2026-04-23 公開。Veo 3.1 Fast の半額未満 |
| `google/veo-3.1-fast` | 4 / 6 / 8 | 0.10〜 | [B] 2026-04-24 公開 |
| `google/veo-3.1` | 4 / 6 / 8 | 0.40〜 | [B]（ドキュメントの例は 0.50 / 1080p 0.75） |
| `kwaivgi/kling-v3.0-std` | 3〜15 | 0.126（音声あり）/ 0.084（無音） | [B] モデルページは「$0.126/s から」。音声の内訳はミラーによる [C]。9:16 対応 |
| `kwaivgi/kling-v3.0-pro` | 3〜15 | 0.168〜 | [C] ミラーのみ。同じページに $0.112 という値もあり、意味は不明 |
| `kwaivgi/kling-video-o1` | 5 / 10 | 0.112 | [B] 2026-04-20 公開。提供元は 1 社。16:9 / 9:16 / 1:1 |
| `minimax/hailuo-2.3` | 6 / 10 | 0.0817 | [B] 2026-04-20 公開。画像入力は最初のフレームのみ |
| `minimax/hailuo-3` / `minimax/hailuo-3-max` | 未確認 | 未確認 | [C] |
| `bytedance/seedance-2.0`（`-fast` / `-mini`、`seedance-2.5`、`seedance-1-5-pro`） | mini は 4〜15 | 2.0 は 480p で 0.06726〜（音声あり）。mini は 0.01345〜 | [C] 2.0 はミラー、mini は [B]。720p の単価は未確認 |
| `alibaba/wan-2.7` | 2〜10 | 0.10 | [C] ミラー（2026-04-15 公開）。`alibaba/wan-2.6` は未確認 |
| `runway/gen-4.5` | 未確認 | 0.12 | [B] 比較ページ。Runway の帰属表示（§4.3）が OpenRouter 経由でも必要かは未確認 |

- モデル ID には日付付きの別名もある（例 `google/veo-3.1-fast-20260320`、`kwaivgi/kling-v3.0-std-20260429`）[B]。
- 9:16 で出せるか、何秒にできるかは、実装時に `/videos/models` の `supported_aspect_ratios` と `supported_durations` で確かめる。静的な表は、一覧を取得できないときの代わりとしてだけ使う。

**`cost.py` の `PRICING` に入れる推奨値**（すべて `verified: false`、`as_of` は 2026-10-12、`source_url` は各モデルのページ `https://openrouter.ai/<model id>`）:

| model | clip_durations | default_duration | resolution | price_per_second_usd |
|---|---|---|---|---|
| `google/veo-3.1-lite`（既定） | [4, 6, 8] | 6 | 720x1280 | 0.05 |
| `google/veo-3.1-fast` | [4, 6, 8] | 6 | 720x1280 | 0.10 |
| `kwaivgi/kling-v3.0-std` | [3, 4, …, 15] | 5 | 720x1280 | 0.084（`generate_audio:false` の前提。音声ありなら 0.126） |
| `kwaivgi/kling-video-o1` | [5, 10] | 5 | 720x1280 | 0.112 |
| `minimax/hailuo-2.3` | [6, 10] | 6 | 未確認 | 0.0817 |
| `alibaba/wan-2.7` | [2, 3, …, 10] | 5 | 720x1280 | 0.10 |

**費用試算**（§3 と同じ前提: 30 本 balanced、60 クリップ）[C]:

| モデル | 計算 | 費用 |
|---|---|---|
| `google/veo-3.1-lite` | 60 × 6 秒 × $0.05 | $18.00（Gemini API 直接と同額） |
| `kwaivgi/kling-v3.0-std`（無音） | 60 × 5 秒 × $0.084 | $25.20 |
| `minimax/hailuo-2.3` | 60 × 6 秒 × $0.0817 | 約 $29.41 |

OpenRouter は提供元の価格に上乗せしない、とする第三者の記事がある [C]。

### 8.4 Claude（`/chat/completions`）

**エンドポイント** [A]（`esm/funcs/chatSend.js`）:

- `POST /chat/completions`。成功時は 200 の JSON（`stream: true` のときは SSE）。
- SDK が型を持つエラー: 400 401 402 403 404 408 413 422 429 500 502 503 524 529。

**モデル ID** [B]:

- `anthropic/claude-opus-5.5`（2026-09-22 公開。日付付きは `anthropic/claude-opus-5.5-20260921`。バッチ用の `:batch` 版もある）。
- `anthropic/claude-sonnet-5.5`（2026-09-28 公開。日付付きは `anthropic/claude-sonnet-5.5-20260928`）。
- 補足:
  - SDK 1.4.25 の `ModelId` 型（`esm/types/models.d.ts`）は古く、Claude は `anthropic/claude-opus-4.5` までしか載っていない [A]。
  - 「常に最新」を指す別名 `~anthropic/claude-opus-latest` が SDK の説明文にある [A]。黙って中身が変わるため、本アプリでは使わない。
  - 推奨: 設定 `OPENROUTER_CLAUDE_MODEL` の既定値を `anthropic/claude-opus-5.5` にする [C]。Anthropic 直接のときの `ANTHROPIC_MODEL=claude-opus-5-5` とは表記が違う（ドット区切りで、前に `anthropic/` が付く）。

**価格（USD / 100 万トークン）**:

| モデル | 入力 | 出力 | キャッシュ読み | キャッシュ書き | 根拠 |
|---|---|---|---|---|---|
| Opus 5.5 | 4.00 | 20.00 | 0.20 | 5.00（1 時間保持は 8.00） | [B] Anthropic 直接と同額 |
| Sonnet 5.5 | 2.00 | 10.00 | 0.20 | — | Anthropic 直接の価格。OpenRouter のページの抜粋は $1 / $5 と食い違っていて未確認 [C] |

- 文脈長は 100 万トークン、最大出力は 12.8 万トークン [B]。
- 提供元は Anthropic、Amazon Bedrock、Google Vertex、Azure、Claude Platform on AWS の 5 つで、自動でフェイルオーバーする [B]。

**リクエスト（絵コンテ）** [A]（`esm/models/chatrequest.js`、`chatformatjsonschemaconfig.js`、`chatjsonschemaconfig.js`、`chatcontenttext.js`、`providerpreferences.js`）:

```http
POST https://openrouter.ai/api/v1/chat/completions
Authorization: Bearer $OPENROUTER_API_KEY
Content-Type: application/json
X-OpenRouter-Title: AI Music Video Factory

{"model": "anthropic/claude-opus-5.5",
 "messages": [
   {"role": "system",
    "content": [{"type": "text", "text": "<SYSTEM_PROMPT>", "cache_control": {"type": "ephemeral"}}]},
   {"role": "user", "content": "<既存と同じ user_message の JSON 文字列>"}],
 "max_completion_tokens": 16000,
 "response_format": {"type": "json_schema",
                     "json_schema": {"name": "storyboard", "strict": true, "schema": {"…": "STORYBOARD_SCHEMA"}}},
 "reasoning": {"effort": "low"},
 "provider": {"data_collection": "deny", "zdr": true, "require_parameters": true},
 "stream": false}
```

- `max_tokens` は SDK で「非推奨。`max_completion_tokens` を使う」とされている [A]。
- `reasoning.effort` が取る値は `max` `xhigh` `high` `medium` `low` `minimal` `none` [A]。同じ意味の省略形としてトップレベルの `reasoning_effort` もある。
  - Opus 5.5 は思考を無効にできない（Anthropic 直接では無効化が 400 になる）ため、`none` と `minimal` は送らない [C]。
  - Anthropic の `output_config.effort` にどう変換されるかは未確認 [C]。
- `cache_control` には、トップレベル（自動キャッシュ）とコンテンツブロック単位の 2 つの付け方がある [A]。
- `usage: {"include": true}` は SDK のスキーマにない [A]。ドキュメントでも非推奨で、効果がない（使用量は常に返る）とされている [B]。送らない。
- Anthropic 直接で使っている `betas` / `fallbacks: "default"`（サーバー側フォールバック）は、OpenRouter のスキーマにない [A]。拒否されたときはテンプレートに切り替える既存の処理で対応する。
  - OpenRouter 独自の `models: [...]`（候補モデルのフォールバック）はあるが [A]、モデルが黙って変わるため使わない [C]。
- `user` には、エンドユーザーを表す安定した仮名 ID を入れる [A]。
  - 省略すると、アカウント単位の ID として扱われ、提供元のポリシーによるブロックがアカウント全体に及びうる [A]。
  - 生の値は上流に転送されないとされている [A]。
  - 本アプリでは所有者 ID を `SECRET_KEY` で HMAC したものを推奨する [C]。
- 翻訳（`translate_to_english`）は同じ形で送る。`response_format` を付けず、`max_completion_tokens` は 4000 にする。

**応答 `ChatResult`** [A]（`chatresult.js`、`chatchoice.js`、`chatassistantmessage.js`、`chatusage.js`、`costdetails.js`）:

```json
{"id": "gen-…", "object": "chat.completion", "created": 1791763200, "model": "anthropic/claude-opus-5.5",
 "system_fingerprint": null,
 "choices": [{"index": 0,
              "finish_reason": "stop",
              "message": {"role": "assistant",
                          "content": "{…JSON…}",
                          "refusal": null,
                          "reasoning": null}}],
 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
           "cost": 0.0123, "is_byok": false,
           "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
           "completion_tokens_details": {"reasoning_tokens": 0},
           "cost_details": {"upstream_inference_prompt_cost": 0, "upstream_inference_completions_cost": 0,
                            "upstream_inference_cost": null}}}
```

数値は形の例で、実際の値ではない。

- `finish_reason` が取る値は `stop` `length` `content_filter` `tool_calls` `error`、または null [A]。
- `message.content` は文字列か `[{type:"text", text}, …]` の配列、または null [A]。配列のときはテキスト部分を連結する。
- `usage.cost` は USD の請求額 [A][B]。`cost_details.upstream_inference_cost` は BYOK のときだけ意味がある [B]。

**応答の扱い**（既存の `_call_claude` / `_claude_translate` に合わせる。[C]）:

| 応答 | 扱い |
|---|---|
| `message.refusal` が null 以外、または `finish_reason == "content_filter"` | 拒否とみなし、テンプレートに切り替える。費用は計上する |
| `finish_reason == "length"` | JSON が途中で切れるため、解析エラーとしてテンプレートに切り替える |
| `finish_reason == "error"` | 上流のエラーとしてテンプレートに切り替える |
| 402 | 「OpenRouter のクレジットが不足しているため、テンプレートで絵コンテを作成しました」 |
| 403 | モデレーション・ガードレールでの拒否 |
| 404 / 503（ZDR などの条件を満たす提供元がない） | 「データを保持しない提供元が見つからなかったため、テンプレートで絵コンテを作成しました」 |

**費用の記録**:

- `usage.cost` があればそれを使う。
- ない場合は、トークン数から Opus 5.5 の単価で計算する。
  - 計算式: (prompt_tokens − cached_tokens − cache_write_tokens) × $4 + cached_tokens × $0.20 + cache_write_tokens × $5 + completion_tokens × $20（いずれも 100 万トークンあたり）
  - `prompt_tokens` がキャッシュ分を含むという前提は未確認 [C]。
- CostRecord の `model` には、応答の `model`（例 `anthropic/claude-opus-5.5`）を記録する。

### 8.5 データの扱い（ZDR・データ収集）

**Claude（`/chat/completions`）** — 歌詞を送る経路:

- `provider.zdr: true`: ZDR（ゼロデータ保持）のエンドポイントにだけ送る [A]（`providerpreferences.d.ts`）。
- `provider.data_collection: "deny"`: ユーザーデータを収集しない提供元にだけ送る。既定は `"allow"` [A]。
- **どちらも常に送る**（利用者の決定）。`require_parameters: true` も付け、`response_format` などに対応しない提供元を除く [A][B]。
- 補足:
  - リクエスト単位の `zdr` は ZDR を追加するだけで、アカウントやガードレールで有効にした ZDR を外すことはできない [B]。
  - メモリ上のプロンプトキャッシュは「保持」とみなさないため、ZDR でもキャッシュは使える [B]。
  - OpenRouter が提供元のポリシーを確認できない場合は、「保持し、学習にも使う」とみなす [B]。
  - ZDR はプラグインやツール（Web 検索など）には適用されない [B]。本アプリはどちらも使わない。
  - **Opus 5.5 に ZDR を満たすエンドポイントがあるかは未確認** [C]。1 つもない場合はリクエストが失敗し、テンプレートに切り替わる（上の表）。

**動画（`/videos`）** — 送るのは英語のシーン説明だけで、歌詞は含まない:

- **リクエストのスキーマに `zdr` と `data_collection` がない**。`provider` に入れられるのは `options`（パススルー）だけ [A]（`videogenerationrequest.js`、Python 版も同じ）。
  - これに対し、TTS や STT（`speechrequest`、`sttrequest`）の `provider` には両方ある [A]。
- 公式ドキュメントには「動画生成はゼロデータ保持（ZDR）の対象外」とある [B]。
- ミラーサイトには、非同期のため提供元が出力をしばらく保持すること、ZDR を強制しているとルーティングされないことも書かれている [C]。
- 方針:
  - 動画のリクエストに `zdr` と `data_collection` は送らない。スキーマにないため、無視されるのか 400 になるのかは不明。
  - **アカウント全体の ZDR 強制は有効にしない**（動画生成が使えなくなるおそれがある）。Claude だけに ZDR を強制したい場合は、ガードレールの提供元別の項目（`enforce_zdr_anthropic` など。[A] `guardrail.d.ts`）を使う。動画にどう効くかは未確認。

**OpenRouter 自身の保持**:

- プロンプトのログ記録を自分で有効にしない限り、プロンプトを保持しない [B]。
- ログ記録を有効にすると、OpenRouter に広い利用許諾（匿名化した内容の商用利用を含む）を与えることになる [C]（利用規約の要約サイトによる）。**アカウント設定でプロンプトのログ記録をオフのままにする**。

**利用者への開示**（README と RIGHTS_AND_SECURITY §5 に反映する事項。[C]）:

- `ANTHROPIC_API_KEY` がなく `OPENROUTER_API_KEY` がある場合、絵コンテの生成で **歌詞の本文を含む同じ内容が、OpenRouter と、その先の Claude の提供元（Anthropic / Amazon Bedrock / Google Vertex / Azure / Claude Platform on AWS のいずれか）を通る**。
- ZDR とデータ収集拒否は毎回指定するが、実際にどの提供元が使われたかは応答からは分からない（`openrouter_metadata` は `X-OpenRouter-Metadata` ヘッダーで要求した場合だけ返る [A]）。

### 8.6 商用利用・権利

- OpenRouter の利用規約では、出力の権利は **モデルごとの規約（Model Terms）** に従うとされる [C]（規約の要約サイトによる。本文は取得できていない）。
  - 出力が特定の用途に適することは保証しない [C]。
  - 企業向け規約では、入力と出力も「顧客データ」に含めている [B]。
- そのため、`model_terms_checked` では OpenRouter の規約と、実際に使うモデルの規約の **両方** を確認する必要がある。
  - Veo: §4.1 の Gemini API 追加規約。ただし OpenRouter 経由で Google のどのエンドポイント（Gemini API か Vertex か）に送られるかは未確認。
  - Kling: §4.4 のとおり規約本文は未確認。
  - MiniMax、ByteDance、Alibaba: 未確認。
  - Runway: OpenRouter 経由でも "Powered by Runway" の表示が必要かは未確認。
- 実在人物を禁止するプロンプト規則（`sanitize_prompt`）と、公開前の目視確認は、ほかのプロバイダと同じく必須。
- 透かし（SynthID や C2PA など）は、各モデル側の仕様に従う [C]。OpenRouter が透かしを追加するかどうかは未確認。

### 8.7 Python SDK を使うか

- PyPI の `openrouter` は公式（ファーストパーティ）の SDK である [A]。
  - パッケージ情報: 最新は 1.3.43（2026-10-11 公開）。作者は OpenRouter、リポジトリは https://github.com/OpenRouterTeam/python-sdk 、ライセンスは Apache-2.0。説明文は "Official Python Client SDK for OpenRouter."。
  - 中身: TS SDK と同じ生成器（Speakeasy 2.914.0）で作られている。`video_generation.py` に `/videos` の 4 操作がある。
- **ただし本アプリでは使わず、httpx で直接呼ぶ。** 理由は次のとおり。
  - 依存が衝突する。`openrouter` は `pydantic<2.13,>=2.11.2` を要求するが、バックエンドは `pydantic>=2.14,<3`（ロックは 2.14.0）。
  - リリースが頻繁（3 日で 1.3.39〜1.3.43 の 5 版）で、既定で `POST /videos` も再試行してしまう（§8.1）。
  - 既存のアダプタ（Veo / Runway / Kling）は httpx と respx のテストで揃っている。
- ワイヤ上の形式は、上の SDK のスキーマに合わせる。

### 8.8 未確認事項（OpenRouter）

1. **実リクエスト全般**: 2026-10-13 に確認済みなのは `GET /videos/models`、`GET /key`、Claude の `POST /chat/completions`（`zdr: true`・`data_collection: "deny"`・`require_parameters: true`・json_schema strict で成功。記録費用 $0.05564 = 請求額）。**`POST /videos` は、アカウント全体で ZDR を強制していると 404 `0 endpoints … matching your guardrail restrictions and data policy … ZDR violation (account settings)` で拒否される（課金なし）ことを確認**。ZDR 強制を外した後の `POST /videos`（`google/veo-3.1-lite`、duration 4、aspect_ratio 9:16、resolution 720p、generate_audio false）は**202 で受理され、`pending`→`completed` まで約 50 秒、`GET /videos/{id}/content` で 720×1280・24fps・4.0 秒・音声なしの MP4 を取得**。`usage.cost` = 0.12 = 見積もり = アカウントの請求増分。プロンプト中の製品名（フィルム名）は映像内に文字として描かれることを確認（対策済み）。
2. **動画の作成**:
   - `POST /videos` の成功ステータス（SDK は 202 だけを成功として扱う）。
   - 各モデルが 9:16 と 720p を受け付けるか。
   - ネガティブプロンプトを `provider.options` で渡せるか（`allowed_passthrough_parameters` の内容）。
   - `frame_images[].image_url.url` が data URI を受け付けるか。
3. **動画の課金**:
   - `failed` / `cancelled` / `expired` のジョブが課金されるか、そのとき `usage.cost` が返るか。
   - アカウント単位の同時実行数とレート制限。
4. **ダウンロード**:
   - `/content` の 409 / 410 の正確な意味。
   - `unsigned_urls` の保持期間と `expires_at`。
   - `/content` がリダイレクトを返すか。
5. **`pricing_skus` の意味**: キー名は 2026-10-13 の実際の一覧で確認した（§8.3）。ただし各 SKU の意味（例: `duration_seconds` が無音か、`text_to_video_*` が音声の有無を含むか）は名前からの推定で、実際の請求額（`usage.cost`）との突き合わせは未確認。`/videos/models` が認証を要するかも未確認。
6. **データ保持**:
   - 動画のリクエストに `zdr` / `data_collection` を入れたときの挙動（無視か 400 か）。
   - ~~アカウントで ZDR を強制したとき、動画が拒否されるか~~ → 拒否される（2026-10-13 確認、上記 1）。
7. **Opus 5.5 の提供元**: `zdr: true` と `data_collection: "deny"` を両方満たす提供元は**ある**（2026-10-13、`anthropic/claude-opus-5.5` で成功）。ないときのエラーの形は未確認。
8. **Claude のパラメータ変換**:
   - `reasoning.effort` が Anthropic の `output_config.effort` にどう対応するか。`none` で 400 になるか。
   - `response_format`（json_schema、strict）が Anthropic の構造化出力にどう変換されるか。
9. **Claude の応答**:
   - 拒否の表し方（`finish_reason: "content_filter"` か、`message.refusal` か、その両方か）。
   - `usage.prompt_tokens` にキャッシュ分が含まれるか。`usage.cost` が常に返るか。
10. **価格**: Sonnet 5.5 の OpenRouter 価格（$2 / $10 か $1 / $5 か）。Kling v3.0 pro・Seedance 2.0・Wan 2.6 の正確な単価。
11. **規約**: OpenRouter の利用規約本文、各モデルの規約（出力の所有権・商用利用・実在人物）、OpenRouter 経由の Runway モデルの帰属表示。
12. **Webhook**（`callback_url`）の署名方式と再送の仕様（本アプリでは使わない）。

### 8.9 出典（取得・確認日 2026-10-12）

**[A] 直接取得**

- npm `@openrouter/sdk@1.4.25`（2026-10-06）。主に読んだファイル:
  - `esm/lib/config.js`、`esm/lib/security.js`、`esm/lib/retries.js`
  - `esm/funcs/videoGenerationGenerate.js`、`videoGenerationGetGeneration.js`、`videoGenerationGetVideoContent.js`、`videoGenerationListVideosModels.js`、`chatSend.js`、`generationsGetGeneration.js`
  - `esm/models/videogenerationrequest.*`、`videogenerationresponse.*`、`videogenerationusage.*`、`videomodel.*`、`frameimage.*`
  - `esm/models/chatrequest.*`、`chatresult.*`、`chatchoice.*`、`chatassistantmessage.*`、`chatusage.*`、`costdetails.*`、`chatfinishreasonenum.*`
  - `esm/models/chatformatjsonschemaconfig.*`、`chatjsonschemaconfig.*`、`chatcontenttext.*`、`anthropiccachecontroldirective.*`
  - `esm/models/providerpreferences.*`、`speechrequest.*`、`guardrail.*`、`errors/*`、`*responseerrordata.*`
  - `esm/types/models.d.ts`、`esm/models/fusionplugin.d.ts`、`README.md`
- PyPI `openrouter==1.3.43`（2026-10-11）: https://pypi.org/project/openrouter/1.3.43/ 。`METADATA`、`video_generation.py`、`sdk.py`、`components/videogenerationrequest.py`、`components/videogenerationresponse.py`、`components/videomodel.py`、`components/chatusage.py`、`components/frameimage.py`
- バックエンドの依存: `backend/requirements.txt`、`backend/requirements.lock.txt`

**[B] 公式ページ（検索抜粋）**

- https://openrouter.ai/docs/guides/overview/multimodal/video-generation 、https://openrouter.ai/docs/features/multimodal/video-generation
- https://openrouter.ai/docs/cookbook/video-generation/text-to-video 、https://openrouter.ai/docs/cookbook/video-generation/choose-video-model
- https://openrouter.ai/docs/api/api-reference/video-generation/poll-video-generation-status
- https://openrouter.ai/docs/guides/features/zdr 、https://openrouter.ai/blog/insights/zero-data-retention/
- https://openrouter.ai/docs/features/provider-routing 、https://openrouter.ai/docs/guides/routing/provider-selection.md
- https://openrouter.ai/docs/guides/features/structured-outputs
- https://openrouter.ai/docs/guides/administration/usage-accounting
- https://openrouter.ai/docs/api_reference/errors-and-debugging.md
- https://openrouter.ai/terms-of-service-enterprise
- モデルページ:
  - Claude: https://openrouter.ai/anthropic/claude-opus-5.5-20260921 、https://openrouter.ai/models/anthropic/claude-sonnet-5.5
  - Veo: https://openrouter.ai/google/veo-3.1 、https://openrouter.ai/google/veo-3.1-fast-20260320
  - Kling: https://openrouter.ai/kwaivgi/kling-video-o1 、https://openrouter.ai/kwaivgi/kling-v3.0-std-20260429
  - その他: https://openrouter.ai/minimax/hailuo-2.3-20260420 、https://openrouter.ai/alibaba/wan-2.7 、https://openrouter.ai/bytedance/seedance-2.0-mini
  - 比較ページ: https://openrouter.ai/compare/kwaivgi/kling-v3.0-std/runway/gen-4.5

**[C] 第三者・ミラー（参考）**

- https://vorplabs.com/models/openrouter-provider-controls
- https://conductatlas.com/platform/openrouter/openrouter-terms-of-service/provision/CA-P-064062/output-ownership-governed-by-model-terms/
- https://api.ifq.ai/docs/guides/overview/multimodal/video-generation （公式ドキュメントのミラー。ZDR 強制時に動画がルーティングされないという記述）
- https://praison.ai/docs/video/openrouter 、https://mer.vin/2026/09/which-ai-video-models-actually-take-a-first-and-last-frame/
- https://note.com/lucky_allium8251/n/n87f100b52a20 （上乗せなしという記述）

### 8.10 実装メモ（2026-10-12、`app/services/providers/openrouter.py`・`app/services/openrouter.py`・`app/services/llm.py`）

§8.1〜8.5 の推奨形からの変更点と、実装で決めたことをまとめる。どれも respx のモックでのみテストしており、実 API では未検証（`verified: false`）。

- **動画の送信（`POST /videos`）**: 冪等キーがないため、`ApiVideoProvider._submit_request` で送る。
  - 再試行するのは、ジョブが作られていないと言える失敗だけ（接続前のエラーと 408 / 425 / 429 / 503 / 529）。
  - それ以外（読み取り・書き込みのタイムアウト、途中で切れた接続、500 / 502 / 504 / 524 などの 5xx、ID を読み取れない 2xx）は
    `ProviderSubmitAmbiguous`（`code: provider_submit_ambiguous`、再試行不可）とし、見積もり額を課金済み（推定）として記録する。再送はしない。
  - 400 / 401 / 402 / 403 / 413 などの 4xx は再試行不可の拒否として扱う（402 はクレジット不足と表示）。
- **ジョブの再開**: 投入時に見積もり額を `clip.params.submitted_usd` に保存する。再起動後は、モデルがモデル一覧から消えた・料金を
  読み取れなくなった・9:16 に対応しなくなった場合でも、保存したジョブ ID のポーリングを続ける（`_prepare_resume`）。費用は
  `usage.cost`、なければ `submitted_usd`。再開できない場合（キーが消えたなど）は `submitted_usd` を課金済み（推定）として記録する。
- **モデル一覧**: キーがあるときだけ `GET /videos/models` を取得し、メモリと `WORK_DIR/cache/openrouter_video_models.json` に
  `OPENROUTER_MODELS_CACHE_TTL` 秒（既定 3600、最小 60）保持する。
  - 取得に失敗したとき、または 200 でも使えるモデルが 1 つもない（空・ID が不正・9:16 非対応だけ）ときは、前回の一覧（なければ
    組み込みの 7 モデル。2026-10-13 の実際の一覧の写し）を使い続け、キャッシュファイルは上書きせず、5 分後まで再取得しない。
  - 9:16 を明示的に含まないモデル、アップスケーラー、ID が不正または 90 文字を超えるモデルは表示しない。アスペクト比の記載が
    ないモデルは `supports_vertical: false` と注記付きで表示する。
  - 料金を読み取れないモデル（トークン単位の料金、使えない値など。§8.3）は `price_known: false`（`price_per_second_usd: null`）で表示し、選択できない（予算を確保できないため）。
  - 1080p の料金が別にあるときだけ `<id>@1080p` を追加する。
- **ダウンロード**: `/content?index=0`（キー付き）を先に、だめなら `unsigned_urls` を試す。どの URL もリダイレクト先も、API と同じ
  オリジンか、https の公開ホストだけを許可する（`localhost`・`.local`・グローバルでない IP。`2130706433` や `0x7f000001`、`127.1`
  のような数値表記の IP も含めて拒否）。DNS で内部アドレスに解決される名前は防げない。キーは API のオリジンにしか送らない。
- **費用**: 完了時の `usage.cost` を記録し、なければ表の単価 × 秒数（再開時は `submitted_usd`）。ダウンロードのやり直しで
  `usage` のない応答を読み直しても、報告済みの金額として扱う。**BYOK**（`is_byok: true`）のときは `usage.cost` が OpenRouter の
  手数料だけの可能性がある [B] ため、見積もり額を下回る分は見積もり額で記録する（注記「BYOK のため推定額」）。
- **Claude（`/chat/completions`）**: 毎回 `provider: {data_collection: "deny", zdr: true, require_parameters: true}` を送る。
  `user` には所有者 ID を `SECRET_KEY` で HMAC した仮名（`mvf-<32 桁>`）を入れる。
  - 再試行: 408 / 429 / 500 / 502 / 503 / 524 / 529 と接続エラーを最大 2 回（`Retry-After` / `retry-after-ms` に従う、最大 20 秒）。
    条件を満たす提供元がない 503 と、読み取りのタイムアウト（処理中で課金されうる）は再試行しない。
  - 費用は `usage.cost`、なければトークン数 × Anthropic の定価。BYOK のときは `cost_details.upstream_inference_cost` を足し、
    なければトークン数 × 定価を下限にする。
  - 拒否（`message.refusal` か `finish_reason: content_filter`）、`length`、`error`、JSON の解析失敗、HTTP エラーはテンプレートに
    切り替える（日本語の警告付き。応答があった場合は費用を記録）。
- **送らないもの**: 動画のリクエストに `provider`（`zdr` / `data_collection` を含む）、`size`、`callback_url`、`user` は送らない。
