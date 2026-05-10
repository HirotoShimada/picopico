import { useEffect, useMemo, useRef, useState } from 'react';
import type { MiniGameProps, PlayerId } from '../types';
import { useCountdown } from '../hooks/useCountdown';
import { useFreshInput } from '../hooks/useFreshInput';
import { ControlsGuide } from '../components/ControlsGuide';

type Phase = 'wait' | 'go' | 'done';

export function LightTapGame({ durationMs, input, onFinish }: MiniGameProps) {
  const [phase, setPhase] = useState<Phase>('wait');
  const [punished, setPunished] = useState<Record<PlayerId, boolean>>({ 1: false, 2: false });
  const [tapped, setTapped] = useState<Record<PlayerId, number | null>>({ 1: null, 2: null });
  const finishedRef = useRef(false);

  // Random delay before turning green: between 1.0s and durationMs - 1500ms.
  const goAt = useMemo(() => {
    const max = Math.max(1500, durationMs - 1500);
    return 1000 + Math.random() * (max - 1000);
  }, [durationMs]);
  const startRef = useRef<number>(performance.now());

  useEffect(() => {
    const t = window.setTimeout(() => setPhase((p) => (p === 'wait' ? 'go' : p)), goAt);
    return () => window.clearTimeout(t);
  }, [goAt]);

  // Total time budget — auto-finish.
  useCountdown(durationMs, () => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    finalize();
  });

  // Input handling.
  useFreshInput(input, (e) => {
    if (finishedRef.current) return;
    if (e.action !== 'confirm') return;
    if (punished[e.player] || tapped[e.player] != null) return;
    if (phase === 'wait') {
      setPunished((p) => ({ ...p, [e.player]: true }));
    } else if (phase === 'go') {
      const elapsed = performance.now() - startRef.current - goAt;
      setTapped((t) => ({ ...t, [e.player]: Math.max(0, elapsed) }));
    }
  });

  // Finish early when both players have answered or fouled.
  useEffect(() => {
    const settled = (p: PlayerId) => punished[p] || tapped[p] != null;
    if (settled(1) && settled(2) && !finishedRef.current) {
      finishedRef.current = true;
      // Slight delay so players see the final state.
      window.setTimeout(finalize, 350);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [punished, tapped]);

  function finalize() {
    setPhase('done');
    const winners: PlayerId[] = [];
    const labels: Partial<Record<PlayerId, string>> = {};
    const scores: { p: PlayerId; ms: number }[] = [];
    ([1, 2] as PlayerId[]).forEach((p) => {
      if (punished[p]) {
        labels[p] = 'フライング！';
      } else if (tapped[p] != null) {
        scores.push({ p, ms: tapped[p]! });
      } else {
        labels[p] = '間に合わず';
      }
    });
    if (scores.length === 0) {
      onFinish({ winners: [], labels });
      return;
    }
    const best = Math.min(...scores.map((s) => s.ms));
    scores.forEach(({ p, ms }) => {
      labels[p] = `${Math.round(ms)} ms`;
      // Tie tolerance: 30ms.
      if (ms - best <= 30) winners.push(p);
    });
    onFinish({ winners, labels });
  }

  const lampColor = phase === 'wait' ? 'bg-rose-500' : phase === 'go' ? 'bg-emerald-400' : 'bg-slate-400';
  const ringPulse = phase === 'go' ? 'lamp-go' : '';
  const bestTap = Math.min(
    ...([1, 2] as PlayerId[]).map((p) => tapped[p]).filter((value): value is number => value != null),
  );

  return (
    <div className="absolute inset-0 flex items-center justify-center">
      <div className="flex flex-col items-center gap-8">
        <div className={`text-4xl font-black ${phase === 'go' ? 'countdown-slam text-emerald-500 text-stroke' : 'text-slate-800'}`}>
          {phase === 'wait' && '赤のあいだは押すな！'}
          {phase === 'go' && 'いま押せ！！'}
          {phase === 'done' && '結果...'}
        </div>
        <div className={`relative flex h-64 w-64 items-center justify-center rounded-full border-[10px] border-slate-900 ${lampColor} ${ringPulse}`}>
          <div className="absolute inset-3 rounded-full bg-white/30 blur-sm" />
          <div className="relative font-display text-6xl text-white text-stroke">
            {phase === 'wait' ? 'まて' : phase === 'go' ? 'GO!' : 'STOP'}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4 text-center">
          {([1, 2] as PlayerId[]).map((p) => (
            <div
              key={p}
              className={`card min-w-[180px] px-5 py-3 text-white transition-all ${p === 1 ? 'bg-p1' : 'bg-p2'} ${
                tapped[p] != null && tapped[p] === bestTap ? (p === 1 ? 'glow-p1 scale-105' : 'glow-p2 scale-105') : ''
              }`}
            >
              <div className="text-xs font-extrabold tracking-widest">{p === 1 ? '1P' : '2P'}</div>
              <div className="text-2xl font-black">
                {punished[p] && 'X フライング'}
                {!punished[p] && tapped[p] != null && `${Math.round(tapped[p]!)} ms`}
                {!punished[p] && tapped[p] == null && '...'}
              </div>
            </div>
          ))}
        </div>
      </div>
      <ControlsGuide confirm="緑になったら押す" />
    </div>
  );
}
