import { useEffect, useRef, useState } from 'react';
import type { PlayerId, PlayerProfiles, Scoreboard } from '../types';
import { ProfileAvatar } from './ProfileAvatar';

type Props = {
  round: number;
  totalRounds: number;
  scores: Scoreboard;
  profiles?: Partial<PlayerProfiles>;
  remainingMs: number;
  totalMs: number;
};

export function Hud({ round, totalRounds, scores, profiles, remainingMs, totalMs }: Props) {
  const pct = Math.max(0, Math.min(1, remainingMs / totalMs));
  const seconds = (remainingMs / 1000).toFixed(1);
  const urgent = remainingMs <= 1200;
  return (
    <div className="absolute inset-x-0 top-0 z-20 flex items-start justify-between gap-6 p-4">
      <ScoreChip player={1} score={scores[1]} profile={profiles?.[1]} />
      <div className="flex-1">
        <div className="text-center text-sm font-extrabold text-slate-700">
          ROUND {round} / {totalRounds}
        </div>
        <div className="mx-auto mt-1 h-5 w-full max-w-xl overflow-hidden rounded-full border-4 border-slate-900 bg-white">
          <div
            className={`h-full rounded-full bg-gradient-to-r transition-[width] duration-100 ${
              urgent ? 'from-red-500 via-pop-yellow to-red-500' : 'from-pop-yellow via-pop-pink to-pop-violet'
            }`}
            style={{ width: `${pct * 100}%` }}
          />
        </div>
        <div className={`mt-1 text-center text-2xl font-black tabular-nums ${urgent ? 'animate-wiggle text-red-600' : 'text-slate-900'}`}>
          {seconds}s
        </div>
      </div>
      <ScoreChip player={2} score={scores[2]} profile={profiles?.[2]} />
    </div>
  );
}

function ScoreChip({ player, score, profile }: { player: PlayerId; score: number; profile?: PlayerProfiles[PlayerId] }) {
  const color = player === 1 ? 'bg-p1' : 'bg-p2';
  const previousScore = useRef(score);
  const [delta, setDelta] = useState(0);
  const [pulseKey, setPulseKey] = useState(0);

  useEffect(() => {
    const change = score - previousScore.current;
    previousScore.current = score;
    if (change <= 0) return;
    setDelta(change);
    setPulseKey((key) => key + 1);
    const t = window.setTimeout(() => setDelta(0), 900);
    return () => window.clearTimeout(t);
  }, [score]);

  return (
    <div className={`card ${color} relative flex min-w-[190px] items-center gap-3 overflow-visible px-4 py-2 text-white`}>
      {delta > 0 && (
        <div
          key={`float-${pulseKey}`}
          className="score-float pointer-events-none absolute -right-2 -top-2 rounded-full border-4 border-slate-900 bg-pop-yellow px-3 py-0.5 text-2xl font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]"
        >
          +{delta}
        </div>
      )}
      <ProfileAvatar profile={profile} player={player} size="sm" />
      <div className="min-w-0">
        <div className="max-w-[115px] truncate text-xs font-extrabold tracking-widest">
          {profile?.name || (player === 1 ? 'PLAYER 1' : 'PLAYER 2')}
        </div>
        <div key={pulseKey} className="score-pop text-4xl font-black tabular-nums leading-none">
          {score}
        </div>
      </div>
    </div>
  );
}
