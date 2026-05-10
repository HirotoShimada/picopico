import { useEffect, useRef, useState } from 'react';
import type { PlayerId, PlayerProfiles, ProfileBackgroundId, ProfileEffectId, ProfileFilterId } from '../types';
import { ProfileAvatar } from '../components/ProfileAvatar';
import {
  PROFILE_BACKGROUNDS,
  PROFILE_EFFECTS,
  PROFILE_FILTERS,
  createInitialAvatar,
  drawFilterOverlay,
  drawProfileBackground,
  drawProfileEffect,
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
  const activeBackground = PROFILE_BACKGROUNDS.find((background) => background.id === activeProfile.backgroundId) ?? PROFILE_BACKGROUNDS[0];
  const activeEffect = PROFILE_EFFECTS.find((effect) => effect.id === activeProfile.effectId) ?? PROFILE_EFFECTS[0];

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

    drawProfileBackground(ctx, activeProfile.backgroundId, activePlayer);
    drawCameraPortrait(ctx, video, { cropX, cropY, cropSize }, activeProfile.filterId, activeFilter.canvasFilter, activeProfile.backgroundId !== 'plain');
    ctx.filter = 'none';
    drawFilterOverlay(ctx, activeProfile.filterId, activePlayer);
    drawProfileEffect(ctx, activeProfile.effectId, activePlayer);
    updateProfile(activePlayer, { imageDataUrl: canvas.toDataURL('image/jpeg', 0.84) });
  };

  const useInitialAvatar = () => {
    updateProfile(activePlayer, {
      imageDataUrl: createInitialAvatar(activeProfile.name, activePlayer, activeProfile.filterId, activeProfile.backgroundId, activeProfile.effectId),
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

        <div className="card grid grid-cols-[1fr_340px] gap-6 bg-white/95 px-7 py-6">
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

            <div className="relative h-[340px] overflow-hidden rounded-[28px] border-4 border-slate-900 bg-slate-900 shadow-[0_8px_0_#1a1f2c]">
              <BackgroundPreviewLayer backgroundId={activeProfile.backgroundId} player={activePlayer} />
              {cameraState === 'ready' ? (
                <video
                  ref={videoRef}
                  autoPlay
                  muted
                  playsInline
                  className={
                    activeProfile.backgroundId === 'plain'
                      ? 'relative z-10 h-full w-full scale-x-[-1] object-cover'
                      : 'absolute left-1/2 top-1/2 z-10 h-[88%] w-[76%] -translate-x-1/2 -translate-y-1/2 scale-x-[-1] rounded-[46%] border-[6px] border-white object-cover shadow-[0_0_0_4px_#1a1f2c]'
                  }
                  style={{ filter: activeFilter.cssFilter }}
                />
              ) : (
                <div className="relative z-10 flex h-full flex-col items-center justify-center gap-4 bg-slate-950/55 text-white">
                  <div className="text-6xl font-black">CAMERA</div>
                  <div className="text-sm font-bold text-white/75">カメラを起動して顔写真を撮影</div>
                </div>
              )}
              <FilterPreviewOverlay filterId={activeProfile.filterId} />
              <EffectPreviewOverlay effectId={activeProfile.effectId} player={activePlayer} />
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

          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-4">
              <ProfileAvatar profile={activeProfile} player={activePlayer} size="xl" className="h-32 w-32" />
              <div className="min-w-0">
                <div className="text-xs font-black tracking-widest text-slate-500">PLAYER IMAGE</div>
                <div className="max-w-[190px] truncate text-3xl font-black text-slate-900">{activeProfile.name}</div>
                <div className="mt-2 rounded-xl border-[3px] border-slate-900 bg-pop-yellow px-3 py-1 text-xs font-black text-slate-900 shadow-[0_3px_0_#1a1f2c]">
                  {activeFilter.label} / {activeBackground.label} / {activeEffect.label}
                </div>
              </div>
            </div>
            <OptionGroup title="顔フィルター" options={PROFILE_FILTERS} value={activeProfile.filterId} onChange={(filterId) => updateProfile(activePlayer, { filterId })} />
            <OptionGroup
              title="背景加工"
              options={PROFILE_BACKGROUNDS}
              value={activeProfile.backgroundId}
              onChange={(backgroundId) => updateProfile(activePlayer, { backgroundId })}
            />
            <OptionGroup title="エフェクト" options={PROFILE_EFFECTS} value={activeProfile.effectId} onChange={(effectId) => updateProfile(activePlayer, { effectId })} />
          </div>
        </div>
      </div>
    </div>
  );
}

type ProfileOption<T extends string> = {
  id: T;
  label: string;
};

function OptionGroup<T extends string>({
  title,
  options,
  value,
  onChange,
}: {
  title: string;
  options: ProfileOption<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="rounded-2xl border-[3px] border-slate-900 bg-white px-3 py-2 shadow-[0_3px_0_#1a1f2c]">
      <div className="mb-2 text-xs font-black tracking-widest text-slate-500">{title}</div>
      <div className="grid grid-cols-3 gap-2">
        {options.map((option) => (
          <button
            key={option.id}
            type="button"
            onClick={() => onChange(option.id)}
            className={`min-h-10 rounded-xl border-[3px] px-2 text-[11px] font-black leading-tight shadow-[0_2px_0_#1a1f2c] transition-transform hover:-translate-y-0.5 ${
              value === option.id ? 'border-slate-900 bg-pop-mint text-slate-900' : 'border-slate-900 bg-slate-50 text-slate-700'
            }`}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function BackgroundPreviewLayer({ backgroundId, player }: { backgroundId: ProfileBackgroundId; player: PlayerId }) {
  const accent = player === 1 ? '#ff5577' : '#3aa3ff';
  const other = player === 1 ? '#3aa3ff' : '#ff5577';
  if (backgroundId === 'plain') return null;
  if (backgroundId === 'burst') {
    return (
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background: `repeating-conic-gradient(from -8deg at 50% 50%, rgba(255,255,255,0.4) 0deg 10deg, rgba(26,31,44,0.12) 10deg 20deg), radial-gradient(circle at 50% 46%, #fff 0 8%, #ffd23f 28%, ${accent} 78%)`,
        }}
      />
    );
  }
  if (backgroundId === 'stage') {
    return (
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'linear-gradient(105deg, transparent 0 14%, rgba(255,255,255,0.25) 14% 30%, transparent 30% 70%, rgba(255,255,255,0.24) 70% 86%, transparent 86%), linear-gradient(180deg, #1a1f2c 0%, #5533a8 62%, #ffd23f 100%)',
        }}
      />
    );
  }
  if (backgroundId === 'space') {
    return (
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(circle at 78% 20%, #ffd23f 0 8%, transparent 9%), radial-gradient(circle at 18% 26%, #fff 0 1.2%, transparent 1.8%), radial-gradient(circle at 44% 14%, #fff 0 1%, transparent 1.5%), radial-gradient(circle at 70% 72%, #fff 0 1%, transparent 1.5%), linear-gradient(135deg, #10162c, #3f2a88 55%, #061826)',
        }}
      />
    );
  }
  return (
    <div
      className="pointer-events-none absolute inset-0"
      style={{
        background: `linear-gradient(90deg, rgba(255,255,255,0.45) 12px, transparent 12px), linear-gradient(rgba(255,255,255,0.45) 12px, transparent 12px), linear-gradient(135deg, #fff7d6, ${accent}, ${other}, #5ee2c1)`,
        backgroundSize: '24px 24px, 24px 24px, 100% 100%',
      }}
    />
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

function EffectPreviewOverlay({ effectId, player }: { effectId: ProfileEffectId; player: PlayerId }) {
  const accentClass = player === 1 ? 'text-p1' : 'text-p2';
  if (effectId === 'none') return null;
  if (effectId === 'sparkle') {
    return (
      <div className="pointer-events-none absolute inset-0 z-20">
        {[
          [38, 34, 28],
          [292, 58, 34],
          [64, 270, 22],
          [292, 264, 28],
        ].map(([left, top, size]) => (
          <span
            key={`${left}-${top}`}
            className="absolute rotate-45 border-[3px] border-slate-900 bg-pop-yellow shadow-[0_0_16px_rgba(255,255,255,0.9)]"
            style={{ left, top, width: size, height: size }}
          />
        ))}
      </div>
    );
  }
  if (effectId === 'lightning') {
    return (
      <div className="pointer-events-none absolute inset-0 z-20">
        {[
          [34, 24, -14],
          [286, 40, 16],
        ].map(([left, top, rotate]) => (
          <span
            key={`${left}-${top}`}
            className="absolute h-24 w-12 bg-pop-yellow shadow-[0_0_14px_rgba(255,255,255,0.9)]"
            style={{
              left,
              top,
              transform: `rotate(${rotate}deg)`,
              clipPath: 'polygon(42% 0, 100% 0, 62% 40%, 88% 40%, 26% 100%, 42% 54%, 10% 54%)',
            }}
          />
        ))}
      </div>
    );
  }
  if (effectId === 'bubbles') {
    return (
      <div className="pointer-events-none absolute inset-0 z-20">
        {[24, 34, 42, 28, 20].map((size, index) => (
          <span
            key={size}
            className="absolute rounded-full border-4 border-white/85 bg-pop-mint/25"
            style={{
              width: size,
              height: size,
              left: [40, 282, 72, 260, 218][index],
              top: [96, 116, 270, 250, 46][index],
            }}
          />
        ))}
      </div>
    );
  }
  return (
    <div className={`pointer-events-none absolute bottom-7 right-7 z-20 rotate-[-10deg] text-5xl font-black ${accentClass} text-stroke`}>
      WIN!
    </div>
  );
}

type CameraCrop = {
  cropX: number;
  cropY: number;
  cropSize: number;
};

function drawCameraPortrait(
  ctx: CanvasRenderingContext2D,
  video: HTMLVideoElement,
  crop: CameraCrop,
  filterId: ProfileFilterId,
  canvasFilter: string,
  cutOutBackground: boolean,
) {
  ctx.save();
  if (cutOutBackground) {
    ctx.beginPath();
    ctx.ellipse(180, 178, 132, 154, 0, 0, Math.PI * 2);
    ctx.clip();
  }

  if (filterId === 'pixel') drawPixelVideo(ctx, video, crop, canvasFilter);
  else drawMirroredVideo(ctx, video, crop, canvasFilter);

  ctx.restore();

  if (cutOutBackground) {
    ctx.save();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 12;
    ctx.beginPath();
    ctx.ellipse(180, 178, 132, 154, 0, 0, Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = '#1a1f2c';
    ctx.lineWidth = 5;
    ctx.stroke();
    ctx.restore();
  }
}

function drawMirroredVideo(ctx: CanvasRenderingContext2D, video: HTMLVideoElement, crop: CameraCrop, canvasFilter: string) {
  ctx.save();
  ctx.filter = canvasFilter;
  ctx.translate(360, 0);
  ctx.scale(-1, 1);
  ctx.drawImage(video, crop.cropX, crop.cropY, crop.cropSize, crop.cropSize, 0, 0, 360, 360);
  ctx.restore();
}

function drawPixelVideo(ctx: CanvasRenderingContext2D, video: HTMLVideoElement, crop: CameraCrop, canvasFilter: string) {
  const small = document.createElement('canvas');
  small.width = 90;
  small.height = 90;
  const smallCtx = small.getContext('2d');
  if (!smallCtx) return;
  smallCtx.filter = canvasFilter;
  smallCtx.translate(90, 0);
  smallCtx.scale(-1, 1);
  smallCtx.drawImage(video, crop.cropX, crop.cropY, crop.cropSize, crop.cropSize, 0, 0, 90, 90);
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(small, 0, 0, 360, 360);
  ctx.restore();
}

function stopCamera(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}
