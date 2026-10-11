import { RIGHTS_ITEMS } from "@/lib/constants";
import type { RightsKey } from "@/lib/types";

/** Why each §9 checklist item matters (from docs/RIGHTS_AND_SECURITY.md §6). */
const EXPLANATIONS: Record<RightsKey, string> = {
  audio_rights:
    "原盤権・著作権を持たない音源で動画を作って投稿すると権利侵害になり、プラットフォームで削除やミュートの対象になります。レーベルや権利者から利用許諾を得ているか確認してください。",
  lyrics_rights:
    "歌詞を字幕として動画に焼き込むことは、歌詞の複製・公衆送信にあたります。作詞者や音楽出版社の許諾が必要です。",
  no_real_person_likeness:
    "肖像権・パブリシティ権の侵害やディープフェイクを避けるため、アーティスト本人や著名人に似せた人物は生成しません。多くの動画生成モデルの利用規約でも禁止されています。",
  model_terms_checked:
    "商用利用の可否、クレジット表記、生成物の権利の帰属は、プロバイダーや契約プランによって異なります。設定画面のプロバイダー情報もあわせて確認してください。",
  platform_terms:
    "Instagram などの投稿先には、音楽の利用ポリシー、AI 生成コンテンツの表示（AI ラベル）、ブランドコンテンツのルールがあります。",
};

export interface RightsItem {
  key: RightsKey;
  label: string;
  explanation: string;
}

export const RIGHTS_CHECKLIST: readonly RightsItem[] = RIGHTS_ITEMS.map((item) => ({
  ...item,
  explanation: EXPLANATIONS[item.key],
}));

export const LIKENESS_POLICY: readonly string[] = [
  "生成する人物は架空の人物だけです。アーティスト本人・著名人・一般の実在人物に似せた映像は作りません。",
  "プロンプトからはアーティスト名や「本人」「そっくり」「celebrity」「lookalike」などの実在人物を示す表現を自動で取り除きます。",
  "すべてのプロンプトに「fictional characters only, no real or identifiable people, no celebrities」と、文字の混入を防ぐ「no text, no captions, no watermark」を付けます。",
  "モック生成の映像には「MOCK」の表示が入り、本物の生成結果と区別できます。",
  "アップロードした素材に写っている人物の権利は、アップロードした方の責任になります（自動では判定しません）。",
  "生成された動画に実在人物・ロゴ・不適切な内容が含まれていないかは、公開前に必ず人の目で確認してください。",
];
