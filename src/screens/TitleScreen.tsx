import type { PlayerId, PlayerInput, PlayerProfiles } from '../types';
import { ProfileAvatar } from '../components/ProfileAvatar';
import { useFreshInput } from '../hooks/useFreshInput';

type Props = {
  input: PlayerInput | null;
  profiles: PlayerProfiles;
  onStart: () => void;
  onNetworkMode: () => void;
  onEditProfiles: () => void;
};

export function TitleScreen({ input, profiles, onStart, onNetworkMode, onEditProfiles }: Props) {
  useFreshInput(input, (e) => {
    if (e.action === 'confirm') onStart();
  });

  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-12">
      <div className="flex flex-col items-center gap-3">
        <div className="rounded-full border-4 border-slate-900 bg-pop-yellow px-5 py-1 text-sm font-black tracking-widest text-slate-900 shadow-[0_4px_0_#1a1f2c]">
          PARTY MINIGAMES
        </div>
        <h1 className="font-display text-[120px] leading-none tracking-tight text-pop-pink text-stroke animate-popin">
          ピコピコ
          <br />
          バトル!
        </h1>
        <p className="mt-2 text-lg font-bold text-slate-700">
          短いミニゲームが連続で出題！全10ラウンドの2人対戦パーティーゲーム
        </p>
      </div>

      <div className="flex items-center gap-5">
        <button type="button" onClick={onStart} className="btn-pop animate-wiggle">
          ローカル対戦
        </button>
        <button type="button" onClick={onNetworkMode} className="btn-pop btn-mint">
          ネット対戦
        </button>
      </div>
      <button
        type="button"
        onClick={onEditProfiles}
        className="rounded-full border-4 border-slate-900 bg-white px-5 py-2 text-sm font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]"
      >
        プロフィール変更
      </button>

      <div className="grid grid-cols-2 gap-6 text-center">
        <PlayerCard
          player={1}
          name="PLAYER 1"
          profile={profiles[1]}
          color="bg-p1"
          keys={[
            { k: 'A', d: '左' },
            { k: 'D', d: '右' },
            { k: 'Space', d: '決定' },
          ]}
        />
        <PlayerCard
          player={2}
          name="PLAYER 2"
          profile={profiles[2]}
          color="bg-p2"
          keys={[
            { k: '←', d: '左' },
            { k: '→', d: '右' },
            { k: 'Enter', d: '決定' },
          ]}
        />
      </div>
    </div>
  );
}

function PlayerCard({
  player,
  name,
  profile,
  color,
  keys,
}: {
  player: PlayerId;
  name: string;
  profile: PlayerProfiles[PlayerId];
  color: string;
  keys: { k: string; d: string }[];
}) {
  return (
    <div className={`card ${color} flex items-center gap-4 px-6 py-4 text-white`}>
      <ProfileAvatar profile={profile} player={player} size="md" />
      <div>
      <div className="text-sm font-extrabold tracking-widest">{profile.name || name}</div>
      <div className="mt-2 flex items-center gap-4">
        {keys.map((k) => (
          <div key={k.k} className="flex flex-col items-center">
            <kbd className="rounded-md border-2 border-slate-900 bg-white px-3 py-1 text-base font-black text-slate-900 shadow-[0_2px_0_#1a1f2c]">
              {k.k}
            </kbd>
            <span className="mt-1 text-xs font-bold">{k.d}</span>
          </div>
        ))}
      </div>
      </div>
    </div>
  );
}
