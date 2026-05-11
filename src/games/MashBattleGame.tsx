import { useRef, useState } from 'react';
import type { MiniGameProps, PlayerId } from '../types';
import { useCountdown } from '../hooks/useCountdown';
import { useFreshInput } from '../hooks/useFreshInput';
import { ControlsGuide } from '../components/ControlsGuide';

export function MashBattleGame({ durationMs, input, onFinish }: MiniGameProps) {
  const [counts, setCounts] = useState<Record<PlayerId, number>>({ 1: 0, 2: 0 });
  const [bump, setBump] = useState<Record<PlayerId, number>>({ 1: 0, 2: 0 });
  const finishedRef = useRef(false);

  useCountdown(durationMs, () => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    finalize(counts);
  });

  useFreshInput(input, (e) => {
    if (finishedRef.current) return;
    if (e.action !== 'confirm') return;
    setCounts((c) => ({ ...c, [e.player]: c[e.player] + 1 }));
    setBump((b) => ({ ...b, [e.player]: b[e.player] + 1 }));
  });

  function finalize(state: Record<PlayerId, number>) {
    const winners: PlayerId[] = [];
    const labels: Partial<Record<PlayerId, string>> = {
      1: `${state[1]} 回`,
      2: `${state[2]} 回`,
    };
    if (state[1] > state[2]) winners.push(1);
    else if (state[2] > state[1]) winners.push(2);
    else if (state[1] > 0) {
      winners.push(1, 2);
    }
    onFinish({ winners, labels });
  }

  const max = Math.max(counts[1], counts[2], 1);
  const leader: PlayerId | null = counts[1] === counts[2] ? null : counts[1] > counts[2] ? 1 : 2;
  const lead = Math.abs(counts[1] - counts[2]);

  return (
    <div className="absolute inset-0 flex items-center justify-center">
      <div className="flex flex-col items-center gap-8">
        <div className="text-3xl font-black text-slate-800 animate-wiggle">れんだバトル！！</div>
        <div className="h-10 rounded-full border-4 border-slate-900 bg-white px-6 py-1 text-xl font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]">
          {leader ? `${leader}P リード +${lead}` : '同点！'}
        </div>
        <div className="grid w-[720px] grid-cols-2 gap-8">
          {([1, 2] as PlayerId[]).map((p) => (
            <div key={p} className="flex flex-col items-center gap-3">
              <div
                className={`card relative flex h-40 w-full items-center justify-center text-7xl font-black tabular-nums text-white transition-all ${
                  p === 1 ? 'bg-p1' : 'bg-p2'
                } ${leader === p ? (p === 1 ? 'glow-p1' : 'glow-p2') : ''}`}
                style={{ transform: `scale(${1 + (bump[p] % 2) * 0.04})` }}
              >
                {leader === p && (
                  <div className="absolute -top-5 rounded-full border-4 border-slate-900 bg-pop-yellow px-4 py-1 text-sm font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]">
                    LEAD
                  </div>
                )}
                {counts[p]}
              </div>
              <div className="h-6 w-full overflow-hidden rounded-full border-4 border-slate-900 bg-white">
                <div
                  className={`h-full rounded-full ${p === 1 ? 'bg-p1' : 'bg-p2'}`}
                  style={{ width: `${(counts[p] / max) * 100}%` }}
                />
              </div>
              <div className="text-sm font-bold text-slate-700">↓ を連打！</div>
            </div>
          ))}
        </div>
      </div>
      <ControlsGuide confirm="連打する" />
    </div>
  );
}
