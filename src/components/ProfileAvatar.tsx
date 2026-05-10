import type { PlayerId, PlayerProfile } from '../types';

type Props = {
  profile?: PlayerProfile | null;
  player: PlayerId;
  size?: 'sm' | 'md' | 'lg' | 'xl';
  className?: string;
};

const SIZE_CLASS = {
  sm: 'h-10 w-10 border-2',
  md: 'h-16 w-16 border-4',
  lg: 'h-24 w-24 border-4',
  xl: 'h-32 w-32 border-4',
};

export function ProfileAvatar({ profile, player, size = 'md', className = '' }: Props) {
  const color = player === 1 ? 'bg-p1' : 'bg-p2';
  return (
    <div
      className={`${SIZE_CLASS[size]} ${color} relative overflow-hidden rounded-full border-slate-900 shadow-[0_4px_0_#1a1f2c] ${className}`}
    >
      {profile?.imageDataUrl ? (
        <img src={profile.imageDataUrl} alt={`${profile.name} のプレイヤー画像`} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-xl font-black text-white">{player}P</div>
      )}
    </div>
  );
}
