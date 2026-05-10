import { useEffect, useRef, useState } from 'react';
import type { PlayerId, PlayerProfiles, ProfileFilterId } from '../types';
import { ProfileAvatar } from '../components/ProfileAvatar';
import {
  PROFILE_FILTERS,
  createInitialAvatar,
  drawFilterOverlay,
  normalizeProfile,
  sanitizeName,
} from '../profile/profileStorage';

type Props = {
  initialProfiles: PlayerProfiles;
  onComplete: (profiles: PlayerProfiles) => void;
};

export function ProfileSetupScreen({ initialProfiles, onComplete }: Props) {
  const [profiles, setProfiles] = useState<PlayerProfiles>(initialProfiles);
  const [activePlayer, setActivePlayer] = useState<PlayerId>(1);
  const [cameraState, setCameraState] = useState<'idle' | 'starting' | 'ready' | 'blocked'>('idle');
  const [error, setError] = useState<string | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const activeProfile = profiles[activePlayer];
  const activeFilter = PROFILE_FILTERS.find((filter) => filter.id === activeProfile.filterId) ?? PROFILE_FILTERS[0];

  useEffect(() => {
    return () => stopCamera(streamRef.current);
  }, []);

  useEffect(() => {
    if (!videoRef.current || !streamRef.current) return;
    videoRef.current.srcObject = streamRef.current;
  }, [cameraState, activePlayer]);

  const updateProfile = (player: PlayerId, patch: Partial<PlayerProfiles[PlayerId]>) => {
    setProfiles((current) => ({
      ...current,
      [player]: normalizeProfile({ ...current[player], ...patch }, player),
    }));
  };

  const startCamera = async () => {
    setError(null);
    setCameraState('starting');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'user', width: { ideal: 720 }, height: { ideal: 720 } },
        audio: false,
      });
      stopCamera(streamRef.current);
      streamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;
      setCameraState('ready');
    } catch {
      setCameraState('blocked');
      setError('カメラを使えませんでした。ブラウザの許可を確認するか、仮アイコンで進めます。');
    }
  };

  const capture = () => {
    const video = videoRef.current;
    if (!video || cameraState !== 'ready') return;
    const canvas = document.createElement('canvas');
    canvas.width = 360;
    canvas.height = 360;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const sourceWidth = video.videoWidth || 720;
    const sourceHeight = video.videoHeight || 720;
    const cropSize = Math.min(sourceWidth, sourceHeight);
    const cropX = (sourceWidth - cropSize) / 2;
    const cropY = (sourceHeight - cropSize) / 2;

    if (activeProfile.filterId === 'pixel') {
      const small = document.createElement('canvas');
      small.width = 90;
      small.height = 90;
      const smallCtx = small.getContext('2d');
      if (!smallCtx) return;
      smallCtx.filter = activeFilter.canvasFilter;
      smallCtx.drawImage(video, cropX, cropY, cropSize, cropSize, 0, 0, 90, 90);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(small, 0, 0, 360, 360);
    } else {
      ctx.filter = activeFilter.canvasFilter;
      ctx.drawImage(video, cropX, cropY, cropSize, cropSize, 0, 0, 360, 360);
    }
    ctx.filter = 'none';
    drawFilterOverlay(ctx, activeProfile.filterId, activePlayer);
    updateProfile(activePlayer, { imageDataUrl: canvas.toDataURL('image/jpeg', 0.84) });
  };

  const useInitialAvatar = () => {
    updateProfile(activePlayer, {
      imageDataUrl: createInitialAvatar(activeProfile.name, activePlayer, activeProfile.filterId),
    });
  };

  const complete = () => {
    stopCamera(streamRef.current);
    streamRef.current = null;
    onComplete({
      1: normalizeProfile(profiles[1], 1),
      2: normalizeProfile(profiles[2], 2),
    });
  };

  return (
    <div className="absolute inset-0 flex items-center justify-center p-7">
      <div className="grid w-[1130px] grid-cols-[330px_1fr] gap-6">
        <div className="flex flex-col gap-4 pt-3">
          <div className="pb-1">
            <div className="text-sm font-black tracking-widest text-slate-700">PROFILE</div>
            <div className="font-display text-5xl leading-[1.05] text-pop-pink text-stroke">プレイヤー登録</div>
          </div>
          {([1, 2] as PlayerId[]).map((player) => (
            <button
              key={player}
              type="button"
              onClick={() => setActivePlayer(player)}
              className={`card flex items-center gap-4 px-4 py-4 text-left text-white ${
                player === 1 ? 'bg-p1' : 'bg-p2'
              } ${activePlayer === player ? (player === 1 ? 'glow-p1' : 'glow-p2') : ''}`}
            >
              <ProfileAvatar profile={profiles[player]} player={player} size="md" />
              <div>
                <div className="text-xs font-extrabold tracking-widest">{player === 1 ? 'あなた / 1P' : 'ローカル2P'}</div>
                <div className="max-w-[190px] truncate text-2xl font-black">{profiles[player].name}</div>
              </div>
            </button>
          ))}
          <div className="card bg-white/95 px-5 py-4 text-sm font-bold text-slate-700">
            ネット対戦では「あなた / 1P」のプロフィールを相手に送ります。ローカル対戦では2Pもここで使います。
          </div>
          <button type="button" onClick={complete} className="btn-pop text-2xl">
            これでOK
          </button>
        </div>

        <div className="card grid grid-cols-[1fr_300px] gap-6 bg-white/95 px-7 py-6">
          <div className="flex flex-col gap-5">
            <div className="grid grid-cols-[1fr_auto] items-end gap-4">
              <label className="flex flex-col gap-2">
                <span className="text-sm font-black tracking-widest text-slate-600">
                  {activePlayer === 1 ? 'あなたの名前' : 'ローカル2Pの名前'}
                </span>
                <input
                  value={activeProfile.name}
                  onChange={(event) => updateProfile(activePlayer, { name: sanitizeName(event.target.value) })}
                  maxLength={14}
                  className="rounded-2xl border-4 border-slate-900 bg-white px-4 py-3 text-3xl font-black text-slate-900 shadow-[0_4px_0_#1a1f2c] outline-none"
                />
              </label>
              <button type="button" onClick={useInitialAvatar} className="rounded-full border-4 border-slate-900 bg-pop-yellow px-4 py-2 text-sm font-black shadow-[0_4px_0_#1a1f2c]">
                仮アイコン
              </button>
            </div>

            <div className="grid grid-cols-3 gap-3">
              {PROFILE_FILTERS.map((filter) => (
                <button
                  key={filter.id}
                  type="button"
                  onClick={() => updateProfile(activePlayer, { filterId: filter.id })}
                  className={`rounded-2xl border-4 px-3 py-2 text-sm font-black shadow-[0_3px_0_#1a1f2c] ${
                    activeProfile.filterId === filter.id ? 'border-slate-900 bg-pop-mint text-slate-900' : 'border-slate-900 bg-white text-slate-700'
                  }`}
                >
                  {filter.label}
                </button>
              ))}
            </div>

            <div className="relative h-[360px] overflow-hidden rounded-[28px] border-4 border-slate-900 bg-slate-900 shadow-[0_8px_0_#1a1f2c]">
              {cameraState === 'ready' ? (
                <video
                  ref={videoRef}
                  autoPlay
                  muted
                  playsInline
                  className="h-full w-full scale-x-[-1] object-cover"
                  style={{ filter: activeFilter.cssFilter }}
                />
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-4 bg-gradient-to-br from-slate-800 to-slate-950 text-white">
                  <div className="text-6xl font-black">CAMERA</div>
                  <div className="text-sm font-bold text-white/75">カメラを起動して顔写真を撮影</div>
                </div>
              )}
              <FilterPreviewOverlay filterId={activeProfile.filterId} />
            </div>

            <div className="flex items-center gap-4">
              <button type="button" onClick={startCamera} className="btn-pop text-xl">
                {cameraState === 'ready' ? 'カメラ再起動' : cameraState === 'starting' ? '起動中...' : 'カメラ起動'}
              </button>
              <button type="button" onClick={capture} disabled={cameraState !== 'ready'} className={`btn-pop text-xl ${cameraState === 'ready' ? 'btn-mint' : 'opacity-45'}`}>
                撮影する
              </button>
            </div>
            {error && <div className="rounded-2xl bg-rose-100 px-4 py-2 text-sm font-black text-rose-700">{error}</div>}
          </div>

          <div className="flex flex-col items-center justify-center gap-5">
            <ProfileAvatar profile={activeProfile} player={activePlayer} size="xl" className="h-48 w-48" />
            <div className="text-center">
              <div className="text-xs font-black tracking-widest text-slate-500">PLAYER IMAGE</div>
              <div className="max-w-[280px] truncate text-4xl font-black text-slate-900">{activeProfile.name}</div>
            </div>
            <div className="rounded-2xl border-4 border-slate-900 bg-pop-yellow px-4 py-2 text-center text-sm font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]">
              {activeFilter.label}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function FilterPreviewOverlay({ filterId }: { filterId: ProfileFilterId }) {
  if (filterId === 'normal' || filterId === 'retro' || filterId === 'pixel') return null;
  if (filterId === 'comic') {
    return (
      <div className="pointer-events-none absolute inset-0 opacity-30">
        {Array.from({ length: 18 }).map((_, index) => (
          <span
            key={index}
            className="absolute left-1/2 top-1/2 h-[3px] w-[280px] origin-left bg-slate-950"
            style={{ transform: `rotate(${index * 20}deg) translateX(74px)` }}
          />
        ))}
      </div>
    );
  }
  if (filterId === 'neon') {
    return <div className="pointer-events-none absolute inset-5 rounded-[24px] border-[12px] border-pop-mint shadow-[0_0_34px_#9b6dff]" />;
  }
  return (
    <div className="pointer-events-none absolute left-1/2 top-5 -translate-x-1/2 text-7xl text-pop-yellow text-stroke">
      ▲
    </div>
  );
}

function stopCamera(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}
