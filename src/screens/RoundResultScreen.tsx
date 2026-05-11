import { useEffect } from 'react';
import type { MiniGameOutcome, PlayerId, PlayerInput, PlayerProfiles, Scoreboard } from '../types';
import { ConfettiBurst } from '../components/ConfettiBurst';
import { ProfileAvatar } from '../components/ProfileAvatar';
import { useFreshInput } from '../hooks/useFreshInput';
import type { SoundCue } from '../hooks/useSoundFx';

type Props = {
  round: number;
  totalRounds: number;
  gameName: string;
  outcome: MiniGameOutcome;
  scores: Scoreboard;
  profiles?: Partial<PlayerProfiles>;
  input: PlayerInput | null;
  onReveal?: (cue: SoundCue) => void;
  onContinue: () => void;
};

export function RoundResultScreen({ round, totalRounds, gameName, outcome, scores, profiles, input, onReveal, onContinue }: Props) {
  const hasWinner = outcome.winners.length > 0;

  // Auto-advance after 2.5s, or sooner if the player hits confirm.
  useEffect(() => {
    const t = window.setTimeout(onContinue, 2500);
    return () => window.clearTimeout(t);
  }, [onContinue]);
  useEffect(() => {
    onReveal?.(hasWinner ? 'win' : 'fail');
  }, [hasWinner, onReveal]);
  useFreshInput(input, (e) => {
    if (e.action === 'confirm') onContinue();
  });

  const banner =
    outcome.winners.length === 0
      ? 'どちらも失敗...'
      : outcome.winners.length === 2
        ? 'どちらも成功！'
        : `${profiles?.[outcome.winners[0]]?.name ?? `${outcome.winners[0]}P`} の勝ち！`;

  return (
    <div className={`absolute inset-0 flex flex-col items-center justify-center gap-8 ${hasWinner ? 'screen-shake' : ''}`}>
      <ConfettiBurst active={hasWinner} pieces={outcome.winners.length === 2 ? 96 : 72} />
      {hasWinner && <div className="pointer-events-none absolute inset-0 animate-flashbg bg-white/20" />}
      <div className="text-sm font-black tracking-widest text-slate-700">
        ROUND {round} / {totalRounds} ・ {gameName}
      </div>
      <div className={`font-display text-7xl text-stroke animate-popin ${hasWinner ? 'text-pop-pink' : 'text-slate-500'}`}>
        {banner}
      </div>

      <div className="grid grid-cols-2 gap-8">
        {([1, 2] as PlayerId[]).map((p) => {
          const won = outcome.winners.includes(p);
          return (
            <div
              key={p}
              className={`card relative flex w-72 flex-col items-center gap-2 px-6 py-5 text-white transition-transform ${
                p === 1 ? 'bg-p1' : 'bg-p2'
              } ${won ? `animate-popin scale-105 ${p === 1 ? 'glow-p1' : 'glow-p2'}` : 'opacity-80'}`}
            >
              {won && (
                <div className="score-float pointer-events-none absolute -right-3 -top-6 rounded-full border-4 border-slate-900 bg-pop-yellow px-4 py-1 text-3xl font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]">
                  +1
                </div>
              )}
              <ProfileAvatar profile={profiles?.[p]} player={p} size="lg" className="-mt-14 mb-1" />
              <div className="max-w-[220px] truncate text-xs font-extrabold tracking-widest">
                {profiles?.[p]?.name ?? (p === 1 ? 'PLAYER 1' : 'PLAYER 2')}
              </div>
              <div className="text-5xl font-black">{won ? '◎ 成功' : '× 失敗'}</div>
              {outcome.labels?.[p] && <div className="text-base font-bold opacity-90">{outcome.labels[p]}</div>}
              <div className="mt-2 text-sm font-bold">
                スコア: <span className="text-2xl font-black tabular-nums">{scores[p]}</span>
                {won && <span className="ml-1 text-pop-yellow">+1</span>}
              </div>
            </div>
          );
        })}
      </div>

      <div className="text-sm font-bold text-slate-600">↓ で次へ (自動で進みます)</div>
    </div>
  );
}
