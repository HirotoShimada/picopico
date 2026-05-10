import type { MiniGameDefinition } from '../types';
import { LightTapGame } from './LightTapGame';
import { BiggerNumberGame } from './BiggerNumberGame';
import { CatchItemGame } from './CatchItemGame';
import { ColorWordGame } from './ColorWordGame';
import { MashBattleGame } from './MashBattleGame';

export const MINI_GAMES: MiniGameDefinition[] = [
  {
    id: 'light-tap',
    name: '光ったら押せ',
    instruction: 'ランプが緑になった瞬間に決定キー！ 早押しはフライング',
    durationMs: 5000,
    Component: LightTapGame,
  },
  {
    id: 'bigger-number',
    name: '大きい数字を選べ',
    instruction: '左右のうち大きい方の数字の方向キーを押せ',
    durationMs: 4000,
    Component: BiggerNumberGame,
  },
  {
    id: 'catch-item',
    name: 'おちものをキャッチ',
    instruction: '左右で動いて、落ちてくるアイテムの真下に立とう',
    durationMs: 6000,
    Component: CatchItemGame,
  },
  {
    id: 'color-word',
    name: '正しい色を選べ',
    instruction: '言葉の意味どおりの色を選ぼう。文字色にだまされるな！',
    durationMs: 4000,
    Component: ColorWordGame,
  },
  {
    id: 'mash-battle',
    name: '連打バトル',
    instruction: '制限時間中に決定キーを連打！多く押した方が勝ち',
    durationMs: 5000,
    Component: MashBattleGame,
  },
];

/** Build a length-N round queue, shuffled, with no immediate repeats. */
export function buildRoundQueue(rounds: number): MiniGameDefinition[] {
  const queue: MiniGameDefinition[] = [];
  let lastId: string | null = null;
  while (queue.length < rounds) {
    const choices = MINI_GAMES.filter((g) => g.id !== lastId);
    const next = choices[Math.floor(Math.random() * choices.length)];
    queue.push(next);
    lastId = next.id;
  }
  return queue;
}
