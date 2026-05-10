import { useEffect, useState } from 'react';
import type { MiniGameDefinition, PlayerId, PlayerProfiles, Scoreboard } from '../types';
import { ProfileAvatar } from '../components/ProfileAvatar';
import type { SoundCue } from '../hooks/useSoundFx';

type Props = {
  round: number;
  totalRounds: number;
  game: MiniGameDefinition;
  scores: Scoreboard;
  profiles?: Partial<PlayerProfiles>;
  onCue?: (cue: SoundCue) => void;
  onReady: () => void;
};

export function RoundIntroScreen({ round, totalRounds, game, scores, profiles, onCue, onReady }: Props) {
  // Mini-countdown 3 -> 2 -> 1 -> START.
  const [step, setStep] = useState<3 | 2 | 1 | 0>(3);

  useEffect(() => {
    onCue?.(step === 0 ? 'go' : 'countdown');
    if (step === 0) {
      const t = window.setTimeout(onReady, 520);
      return () => window.clearTimeout(t);
    }
    const t = window.setTimeout(() => setStep((s) => (s > 0 ? ((s - 1) as 3 | 2 | 1 | 0) : s)), 700);
    return () => window.clearTimeout(t);
  }, [step, onCue, onReady]);

  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-6">
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-10">
        <div key={`ghost-${step}`} className="countdown-slam font-display text-[330px] leading-none text-slate-900">
          {step === 0 ? 'GO' : step}
        </div>
      </div>
      <div className="text-2xl font-black tracking-widest text-slate-700">
        ROUND {round} / {totalRounds}
      </div>
      <div className="card relative flex flex-col items-center gap-3 overflow-hidden bg-pop-yellow px-12 py-8">
        <div className="absolute inset-0 stripes opacity-45" />
        <div className="text-sm font-black tracking-widest text-slate-900">NEXT GAME</div>
        <div className="relative font-display text-6xl text-pop-violet text-stroke">{game.name}</div>
        <div className="relative max-w-2xl text-center text-base font-bold text-slate-800">{game.instruction}</div>
      </div>

      <div className="flex items-center gap-6">
        <ScorePill player={1} name={profiles?.[1]?.name ?? '1P'} color="bg-p1" score={scores[1]} profile={profiles?.[1]} />
        <div className="text-2xl font-black text-slate-700">スコア</div>
        <ScorePill player={2} name={profiles?.[2]?.name ?? '2P'} color="bg-p2" score={scores[2]} profile={profiles?.[2]} />
      </div>

      <div className="relative mt-1 flex h-40 w-56 items-center justify-center">
        <div className="impact-ring absolute h-32 w-32 rounded-full border-[10px] border-pop-mint" />
        <div className="impact-ring absolute h-44 w-44 rounded-full border-[6px] border-pop-yellow [animation-delay:120ms]" />
        <div
          key={step}
          className={`countdown-slam font-display leading-none text-stroke ${
            step === 0 ? 'text-[118px] text-emerald-400' : 'text-[140px] text-pop-pink'
          }`}
        >
          {step === 0 ? 'GO!' : step}
        </div>
      </div>
    </div>
  );
}

function ScorePill({
  player,
  name,
  color,
  score,
  profile,
}: {
  player: PlayerId;
  name: string;
  color: string;
  score: number;
  profile?: PlayerProfiles[PlayerId];
}) {
  return (
    <div className={`card ${color} flex items-center gap-3 px-5 py-2 text-white`}>
      <ProfileAvatar profile={profile} player={player} size="sm" />
      <div>
        <div className="max-w-[120px] truncate text-xs font-extrabold tracking-widest">{name}</div>
        <div className="text-3xl font-black tabular-nums leading-none">{score}</div>
      </div>
    </div>
  );
}
