import { useMemo, useRef, useState } from 'react';
import type { MiniGameProps, PlayerId } from '../types';
import { useCountdown } from '../hooks/useCountdown';
import { useFreshInput } from '../hooks/useFreshInput';
import { ControlsGuide } from '../components/ControlsGuide';

export function BiggerNumberGame({ durationMs, input, onFinish }: MiniGameProps) {
  const numbers = useMemo(() => {
    const a = Math.floor(Math.random() * 90) + 10;
    let b = Math.floor(Math.random() * 90) + 10;
    while (b === a) b = Math.floor(Math.random() * 90) + 10;
    return [a, b] as const;
  }, []);
  const correct: 'left' | 'right' = numbers[0] > numbers[1] ? 'left' : 'right';
  const [answers, setAnswers] = useState<Record<PlayerId, 'left' | 'right' | null>>({ 1: null, 2: null });
  const finishedRef = useRef(false);

  useCountdown(durationMs, () => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    finalize(answers);
  });

  useFreshInput(input, (e) => {
    if (finishedRef.current) return;
    if (e.action !== 'left' && e.action !== 'right') return;
    if (answers[e.player] != null) return;
    const next = { ...answers, [e.player]: e.action } as Record<PlayerId, 'left' | 'right' | null>;
    setAnswers(next);
    if (next[1] && next[2]) {
      finishedRef.current = true;
      window.setTimeout(() => finalize(next), 250);
    }
  });

  function finalize(state: Record<PlayerId, 'left' | 'right' | null>) {
    const winners: PlayerId[] = [];
    const labels: Partial<Record<PlayerId, string>> = {};
    ([1, 2] as PlayerId[]).forEach((p) => {
      if (state[p] === correct) {
        winners.push(p);
        labels[p] = '正解！';
      } else if (state[p] == null) {
        labels[p] = '時間切れ';
      } else {
        labels[p] = '不正解';
      }
    });
    onFinish({ winners, labels });
  }

  return (
    <div className="absolute inset-0 flex items-center justify-center">
      <div className="flex flex-col items-center gap-10">
        <div className="text-3xl font-black text-slate-800">大きいほうの数字を選べ！</div>
        <div className="flex items-center gap-10">
          <NumberPanel value={numbers[0]} side="left" selectedBy={([1, 2] as PlayerId[]).filter((p) => answers[p] === 'left')} />
          <div className="text-5xl font-black text-slate-700">VS</div>
          <NumberPanel value={numbers[1]} side="right" selectedBy={([1, 2] as PlayerId[]).filter((p) => answers[p] === 'right')} />
        </div>
        <div className="grid grid-cols-2 gap-4 text-center">
          {([1, 2] as PlayerId[]).map((p) => (
            <div key={p} className={`card px-5 py-3 ${p === 1 ? 'bg-p1' : 'bg-p2'} text-white min-w-[180px]`}>
              <div className="text-xs font-extrabold tracking-widest">{p === 1 ? '1P' : '2P'}</div>
              <div className="text-2xl font-black">
                {answers[p] == null && '...'}
                {answers[p] === 'left' && '← 左'}
                {answers[p] === 'right' && '右 →'}
              </div>
            </div>
          ))}
        </div>
      </div>
      <ControlsGuide left="左の数字" right="右の数字" />
    </div>
  );
}

function NumberPanel({ value, side, selectedBy }: { value: number; side: 'left' | 'right'; selectedBy: PlayerId[] }) {
  const tilt = side === 'left' ? '-rotate-3' : 'rotate-3';
  return (
    <div
      className={`card relative flex h-48 w-48 items-center justify-center bg-white text-7xl font-black text-slate-900 transition-transform ${tilt} ${
        selectedBy.length > 0 ? 'scale-105' : ''
      }`}
    >
      {selectedBy.length > 0 && (
        <div className="absolute -top-5 flex gap-1">
          {selectedBy.map((p) => (
            <span
              key={p}
              className={`rounded-full border-4 border-slate-900 px-3 py-0.5 text-sm font-black text-white shadow-[0_3px_0_#1a1f2c] ${
                p === 1 ? 'bg-p1' : 'bg-p2'
              }`}
            >
              {p}P
            </span>
          ))}
        </div>
      )}
      {value}
    </div>
  );
}
