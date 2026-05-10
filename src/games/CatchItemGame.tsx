import { useEffect, useMemo, useRef, useState } from 'react';
import type { MiniGameProps, PlayerId } from '../types';
import { useCountdown } from '../hooks/useCountdown';
import { useFreshInput } from '../hooks/useFreshInput';
import { ControlsGuide } from '../components/ControlsGuide';

const LANES = 5;
const ITEM_EMOJI = ['🍎', '🍩', '⭐', '🍕', '🎁'];

export function CatchItemGame({ durationMs, input, onFinish }: MiniGameProps) {
  // Each player chooses lane 0..LANES-1.
  const [pos, setPos] = useState<Record<PlayerId, number>>({ 1: 2, 2: 2 });
  // Item lane is decided once. Falls during the round.
  const itemLane = useMemo(() => Math.floor(Math.random() * LANES), []);
  const emoji = useMemo(() => ITEM_EMOJI[Math.floor(Math.random() * ITEM_EMOJI.length)], []);
  const [progress, setProgress] = useState(0); // 0 -> 1 (top -> bottom)
  const [landed, setLanded] = useState(false);
  const [caught, setCaught] = useState<Record<PlayerId, boolean> | null>(null);
  const finishedRef = useRef(false);
  const landingAtMs = Math.max(1200, durationMs - 650);

  const startRef = useRef<number>(performance.now());
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const t = (performance.now() - startRef.current) / landingAtMs;
      setProgress(Math.min(1, t));
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [landingAtMs]);

  useCountdown(landingAtMs, () => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    resolveLanding();
  });

  useFreshInput(input, (e) => {
    if (finishedRef.current) return;
    if (e.action !== 'left' && e.action !== 'right') return;
    setPos((p) => {
      const cur = p[e.player];
      const delta = e.action === 'left' ? -1 : 1;
      const next = Math.max(0, Math.min(LANES - 1, cur + delta));
      return { ...p, [e.player]: next };
    });
  });

  function resolveLanding() {
    const winners: PlayerId[] = [];
    const labels: Partial<Record<PlayerId, string>> = {};
    const caughtState: Record<PlayerId, boolean> = { 1: false, 2: false };
    ([1, 2] as PlayerId[]).forEach((p) => {
      if (pos[p] === itemLane) {
        winners.push(p);
        labels[p] = 'キャッチ！';
        caughtState[p] = true;
      } else {
        labels[p] = 'のがした...';
      }
    });
    setProgress(1);
    setLanded(true);
    setCaught(caughtState);
    window.setTimeout(() => onFinish({ winners, labels }), 520);
  }

  return (
    <div className="absolute inset-0 flex items-center justify-center">
      <div className="relative h-[460px] w-[640px] overflow-hidden rounded-[28px] border-4 border-slate-900 bg-gradient-to-b from-sky-200 to-sky-50 stripes shadow-[0_8px_0_#1a1f2c]">
        {/* lane guides */}
        <div className="absolute inset-0 grid" style={{ gridTemplateColumns: `repeat(${LANES}, minmax(0, 1fr))` }}>
          {Array.from({ length: LANES }).map((_, i) => (
            <div
              key={i}
              className={`border-r border-dashed border-slate-400/60 transition-colors last:border-r-0 ${
                landed && i === itemLane ? 'bg-pop-yellow/30' : ''
              }`}
            />
          ))}
        </div>

        {/* Falling item */}
        <div
          className="absolute text-6xl transition-transform duration-150"
          style={{
            top: `${progress * 78}%`,
            left: `${(itemLane + 0.5) * (100 / LANES)}%`,
            transform: landed ? 'translate(-50%, 0) scale(1.18)' : 'translate(-50%, 0) scale(1)',
          }}
        >
          {emoji}
        </div>
        {landed && (
          <div
            className="landing-spark absolute text-6xl text-pop-yellow text-stroke"
            style={{ left: `${(itemLane + 0.5) * (100 / LANES)}%`, bottom: '68px' }}
          >
            ★
          </div>
        )}

        {/* Players at the bottom */}
        <div className="absolute inset-x-0 bottom-2 grid h-20" style={{ gridTemplateColumns: `repeat(${LANES}, minmax(0, 1fr))` }}>
          {Array.from({ length: LANES }).map((_, lane) => (
            <div key={lane} className="relative flex items-end justify-center">
              {pos[1] === lane && <Avatar player={1} side="left" caught={caught?.[1] ?? null} />}
              {pos[2] === lane && <Avatar player={2} side="right" caught={caught?.[2] ?? null} />}
            </div>
          ))}
        </div>
      </div>
      <ControlsGuide left="左に移動" right="右に移動" />
    </div>
  );
}

function Avatar({ player, side, caught }: { player: PlayerId; side: 'left' | 'right'; caught: boolean | null }) {
  const color = player === 1 ? 'bg-p1' : 'bg-p2';
  const offset = side === 'left' ? '-translate-x-3' : 'translate-x-3';
  const resultClass = caught === true ? (player === 1 ? 'glow-p1 scale-110' : 'glow-p2 scale-110') : caught === false ? 'opacity-60 grayscale' : '';
  return (
    <div
      className={`flex h-16 w-16 items-center justify-center rounded-2xl border-4 border-slate-900 ${color} text-xl font-black text-white shadow-[0_4px_0_#1a1f2c] transition-all ${offset} ${resultClass}`}
    >
      {player === 1 ? '1P' : '2P'}
    </div>
  );
}
