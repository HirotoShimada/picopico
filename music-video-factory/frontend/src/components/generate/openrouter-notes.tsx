import { ListChecks, Scale, TriangleAlert, VolumeX, Waypoints } from "lucide-react";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { modelsSourceLabel, openRouterVendor } from "@/lib/llm";
import type { ProviderInfo, ProviderModelInfo } from "@/lib/types";

function Note({ icon: Icon, children }: { icon: React.ComponentType<{ className?: string }>; children: React.ReactNode }) {
  return (
    <li className="flex items-start gap-2">
      <Icon className="mt-0.5 size-3.5 shrink-0 text-subtle-foreground" aria-hidden />
      <span>{children}</span>
    </li>
  );
}

/**
 * What choosing OpenRouter means (shown while it is selected): the audio is not generated, the prompts
 * pass through OpenRouter to the model's company, the underlying model's terms apply, and where the
 * model list comes from. Nothing of it has been verified with a real key.
 */
export function OpenRouterNotes({ provider, model }: { provider: ProviderInfo; model: ProviderModelInfo | undefined }) {
  const vendor = openRouterVendor(model?.id);
  const source = modelsSourceLabel(provider);
  return (
    <section
      aria-label="OpenRouter での生成について"
      className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised/40 px-3 py-2.5 text-xs leading-5 text-muted-foreground"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">OpenRouter での生成について</span>
        <Badge tone="warning" title="実際の API キーでの生成テストはまだ行っていません（モックした応答でのみテスト済み）">
          <TriangleAlert aria-hidden />
          未検証
        </Badge>
      </div>
      <ul className="flex flex-col gap-1.5">
        <Note icon={VolumeX}>
          音声の生成はオフ（generate_audio: false）で依頼します。動画には楽曲の音声だけを使います（オフにできるかはモデルごとに未検証です）。
        </Note>
        <Note icon={Waypoints}>
          プロンプト（英語のシーン説明）は OpenRouter を経由して、モデルの提供元{vendor ? `（${vendor}）` : ""}に送られます。楽曲と歌詞は送りません。
        </Note>
        <Note icon={Scale}>
          生成した映像の権利・商用利用には、OpenRouter の規約に加えて、実際に使うモデル{vendor ? `（提供元: ${vendor}）` : ""}の規約が適用されます。公開する前に両方を確認してください。
        </Note>
        {/* The backend note says where the list came from (fetched, cached or built-in) and what was left out. */}
        {provider.models_note || source ? <Note icon={ListChecks}>{provider.models_note || `モデル一覧: ${source}`}</Note> : null}
      </ul>
    </section>
  );
}
