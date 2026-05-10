import { useEffect } from 'react';
import type { PlayerId, PlayerInput, PlayerProfiles, Scoreboard } from '../types';
import { ConfettiBurst } from '../components/ConfettiBurst';
import { ProfileAvatar } from '../components/ProfileAvatar';
import { useFreshInput } from '../hooks/useFreshInput';
import type { SoundCue } from '../hooks/useSoundFx';

type Props = {
  scores: Scoreboard;
  profiles?: Partial<PlayerProfiles>;
  input: PlayerInput | null;
  onReveal?: (cue: SoundCue) => void;
  onRestart: () => void;
};

export function FinalResultScreen({ scores, profiles, input, onReveal, onRestart }: Props) {
  useFreshInput(input, (e) => {
    if (e.action === 'confirm') onRestart();
  });

  useEffect(() => {
    onReveal?.(scores[1] === scores[2] ? 'go' : 'win');
  }, [onReveal, scores[1], scores[2]]);

  let winnerLine: string;
  if (scores[1] > scores[2]) winnerLine = `${profiles?.[1]?.name ?? 'PLAYER 1'} の勝利！`;
  else if (scores[2] > scores[1]) winnerLine = `${profiles?.[2]?.name ?? 'PLAYER 2'} の勝利！`;
  else winnerLine = 'まさかの引き分け！';
  const hasWinner = scores[1] !== scores[2];

  return (
    <div className={`absolute inset-0 flex flex-col items-center justify-center gap-10 ${hasWinner ? 'screen-shake' : ''}`}>
      <ConfettiBurst active={hasWinner} pieces={120} />
      <div className="text-sm font-black tracking-widest text-slate-700">FINAL RESULT</div>
      <div className="font-display text-8xl text-pop-violet text-stroke animate-popin">{winnerLine}</div>

      <div className="grid grid-cols-2 gap-8">
        {([1, 2] as PlayerId[]).map((p) => {
          const isWinner =
            (p === 1 && scores[1] > scores[2]) || (p === 2 && scores[2] > scores[1]);
          return (
            <div
              key={p}
              className={`card relative flex w-80 flex-col items-center gap-2 px-6 py-7 text-white ${
                p === 1 ? 'bg-p1' : 'bg-p2'
              } ${isWinner ? 'animate-wiggle' : ''}`}
            >
              {isWinner && (
                <div className="absolute -top-5 rounded-full border-4 border-slate-900 bg-pop-yellow px-4 py-1 text-sm font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]">
                  WINNER
                </div>
              )}
              <ProfileAvatar profile={profiles?.[p]} player={p} size="xl" className="-mt-14 mb-1" />
              <div className="max-w-[250px] truncate text-xs font-extrabold tracking-widest">
                {profiles?.[p]?.name ?? (p === 1 ? 'PLAYER 1' : 'PLAYER 2')}
              </div>
              <div className="text-8xl font-black tabular-nums leading-none">{scores[p]}</div>
              <div className="text-sm font-bold opacity-90">点</div>
            </div>
          );
        })}
      </div>

      <button onClick={onRestart} className="btn-pop">
        もう一度あそぶ (Space / Enter)
      </button>
    </div>
  );
}
