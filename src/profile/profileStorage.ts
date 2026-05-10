import type { PlayerId, PlayerProfile, PlayerProfiles, ProfileBackgroundId, ProfileEffectId, ProfileFilterId } from '../types';

const STORAGE_KEY = 'pico-battle-profiles-v1';

export const PROFILE_FILTERS: { id: ProfileFilterId; label: string; cssFilter: string; canvasFilter: string }[] = [
  { id: 'normal', label: 'そのまま', cssFilter: 'none', canvasFilter: 'none' },
  { id: 'comic', label: 'マンガ集中線', cssFilter: 'contrast(1.35) saturate(1.3)', canvasFilter: 'contrast(135%) saturate(130%)' },
  { id: 'neon', label: 'ネオン発光', cssFilter: 'contrast(1.2) saturate(1.8) hue-rotate(18deg)', canvasFilter: 'contrast(120%) saturate(180%) hue-rotate(18deg)' },
  { id: 'party', label: 'パーティー王冠', cssFilter: 'saturate(1.55) brightness(1.08)', canvasFilter: 'saturate(155%) brightness(108%)' },
  { id: 'retro', label: 'レトロ写真', cssFilter: 'sepia(0.75) contrast(1.15)', canvasFilter: 'sepia(75%) contrast(115%)' },
  { id: 'pixel', label: 'ピクセル風', cssFilter: 'contrast(1.25) saturate(1.15)', canvasFilter: 'contrast(125%) saturate(115%)' },
];

export const PROFILE_BACKGROUNDS: { id: ProfileBackgroundId; label: string }[] = [
  { id: 'plain', label: '元の背景' },
  { id: 'burst', label: 'ポップ放射' },
  { id: 'stage', label: 'ステージ' },
  { id: 'space', label: '宇宙' },
  { id: 'pixel', label: 'ドット背景' },
];

export const PROFILE_EFFECTS: { id: ProfileEffectId; label: string }[] = [
  { id: 'none', label: 'なし' },
  { id: 'sparkle', label: 'キラキラ' },
  { id: 'lightning', label: '稲妻' },
  { id: 'bubbles', label: 'シャボン' },
  { id: 'victory', label: '勝利スタンプ' },
];

export function createDefaultProfiles(): PlayerProfiles {
  return {
    1: createDefaultProfile(1),
    2: createDefaultProfile(2),
  };
}

export function createDefaultProfile(player: PlayerId): PlayerProfile {
  const name = player === 1 ? 'PLAYER 1' : 'PLAYER 2';
  return {
    name,
    imageDataUrl: createInitialAvatar(name, player),
    filterId: 'normal',
    backgroundId: 'burst',
    effectId: 'sparkle',
  };
}

export function loadProfiles(): PlayerProfiles {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return createDefaultProfiles();
    const parsed = JSON.parse(raw) as Partial<Record<PlayerId, Partial<PlayerProfile>>>;
    return {
      1: normalizeProfile(parsed[1], 1),
      2: normalizeProfile(parsed[2], 2),
    };
  } catch {
    return createDefaultProfiles();
  }
}

export function hasSavedProfiles() {
  try {
    return Boolean(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return false;
  }
}

export function saveProfiles(profiles: PlayerProfiles) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(profiles));
  } catch {
    // Profiles still work for this session even if localStorage is unavailable.
  }
}

export function normalizeProfile(profile: Partial<PlayerProfile> | null | undefined, player: PlayerId): PlayerProfile {
  const fallback = createDefaultProfile(player);
  const filterId = PROFILE_FILTERS.some((filter) => filter.id === profile?.filterId) ? profile!.filterId! : fallback.filterId;
  const backgroundId = PROFILE_BACKGROUNDS.some((background) => background.id === profile?.backgroundId) ? profile!.backgroundId! : fallback.backgroundId;
  const effectId = PROFILE_EFFECTS.some((effect) => effect.id === profile?.effectId) ? profile!.effectId! : fallback.effectId;
  return {
    name: sanitizeName(profile?.name) || fallback.name,
    imageDataUrl: typeof profile?.imageDataUrl === 'string' && profile.imageDataUrl.startsWith('data:image/') ? profile.imageDataUrl : fallback.imageDataUrl,
    filterId,
    backgroundId,
    effectId,
  };
}

export function sanitizeName(name: string | null | undefined) {
  return String(name ?? '')
    .trim()
    .slice(0, 14);
}

export function createInitialAvatar(
  name: string,
  player: PlayerId,
  filterId: ProfileFilterId = 'normal',
  backgroundId: ProfileBackgroundId = 'burst',
  effectId: ProfileEffectId = 'sparkle',
) {
  const canvas = document.createElement('canvas');
  canvas.width = 360;
  canvas.height = 360;
  const ctx = canvas.getContext('2d');
  if (!ctx) return '';

  drawProfileBackground(ctx, backgroundId, player);
  drawFilterOverlay(ctx, filterId, player);
  drawProfileEffect(ctx, effectId, player);
  ctx.fillStyle = '#ffffff';
  ctx.strokeStyle = '#1a1f2c';
  ctx.lineWidth = 10;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = '900 92px sans-serif';
  const initials = getInitials(name || `P${player}`);
  ctx.strokeText(initials, 180, 184);
  ctx.fillText(initials, 180, 184);
  return canvas.toDataURL('image/jpeg', 0.86);
}

export function drawProfileBackground(ctx: CanvasRenderingContext2D, backgroundId: ProfileBackgroundId, player: PlayerId) {
  const accent = player === 1 ? '#ff5577' : '#3aa3ff';
  const other = player === 1 ? '#3aa3ff' : '#ff5577';
  if (backgroundId === 'plain') {
    const gradient = ctx.createLinearGradient(0, 0, 360, 360);
    gradient.addColorStop(0, accent);
    gradient.addColorStop(1, '#ffd23f');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 360, 360);
    ctx.fillStyle = 'rgba(255,255,255,0.18)';
    for (let i = -260; i < 420; i += 38) {
      ctx.fillRect(i, 0, 18, 520);
    }
    return;
  }

  if (backgroundId === 'burst') {
    const gradient = ctx.createRadialGradient(180, 172, 14, 180, 172, 270);
    gradient.addColorStop(0, '#ffffff');
    gradient.addColorStop(0.34, '#ffd23f');
    gradient.addColorStop(1, accent);
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 360, 360);
    ctx.save();
    ctx.translate(180, 172);
    for (let i = 0; i < 24; i += 1) {
      ctx.rotate(Math.PI / 12);
      ctx.fillStyle = i % 2 === 0 ? 'rgba(255,255,255,0.34)' : 'rgba(26,31,44,0.1)';
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(420, -15);
      ctx.lineTo(420, 15);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  if (backgroundId === 'stage') {
    const gradient = ctx.createLinearGradient(0, 0, 0, 360);
    gradient.addColorStop(0, '#1a1f2c');
    gradient.addColorStop(0.58, '#5533a8');
    gradient.addColorStop(1, '#ffd23f');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 360, 360);
    ctx.fillStyle = 'rgba(255,255,255,0.24)';
    ctx.beginPath();
    ctx.moveTo(52, 0);
    ctx.lineTo(150, 360);
    ctx.lineTo(58, 360);
    ctx.closePath();
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(308, 0);
    ctx.lineTo(302, 360);
    ctx.lineTo(210, 360);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = 'rgba(26,31,44,0.18)';
    for (let y = 284; y < 360; y += 22) {
      ctx.fillRect(0, y, 360, 8);
    }
  }

  if (backgroundId === 'space') {
    const gradient = ctx.createLinearGradient(0, 0, 360, 360);
    gradient.addColorStop(0, '#10162c');
    gradient.addColorStop(0.52, '#3f2a88');
    gradient.addColorStop(1, '#061826');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 360, 360);
    ctx.fillStyle = '#ffffff';
    for (let i = 0; i < 34; i += 1) {
      const x = (i * 47) % 350;
      const y = (i * 83) % 340;
      const size = i % 5 === 0 ? 4 : 2;
      ctx.fillRect(x + 5, y + 8, size, size);
    }
    ctx.fillStyle = '#ffd23f';
    ctx.beginPath();
    ctx.arc(286, 78, 36, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.65)';
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.ellipse(286, 78, 58, 15, -0.42, 0, Math.PI * 2);
    ctx.stroke();
  }

  if (backgroundId === 'pixel') {
    ctx.fillStyle = '#fff7d6';
    ctx.fillRect(0, 0, 360, 360);
    const colors = ['#ffd23f', accent, other, '#5ee2c1', '#9b6dff'];
    for (let y = 0; y < 360; y += 24) {
      for (let x = 0; x < 360; x += 24) {
        ctx.fillStyle = colors[(x / 24 + y / 24) % colors.length];
        ctx.globalAlpha = (x + y) % 72 === 0 ? 0.78 : 0.36;
        ctx.fillRect(x, y, 18, 18);
      }
    }
    ctx.globalAlpha = 1;
  }
}

export function drawFilterOverlay(ctx: CanvasRenderingContext2D, filterId: ProfileFilterId, player: PlayerId) {
  const accent = player === 1 ? '#ff5577' : '#3aa3ff';
  if (filterId === 'comic') {
    ctx.save();
    ctx.strokeStyle = '#1a1f2c';
    ctx.globalAlpha = 0.35;
    ctx.lineWidth = 4;
    for (let i = 0; i < 28; i += 1) {
      const angle = (Math.PI * 2 * i) / 28;
      ctx.beginPath();
      ctx.moveTo(180 + Math.cos(angle) * 82, 180 + Math.sin(angle) * 82);
      ctx.lineTo(180 + Math.cos(angle) * 250, 180 + Math.sin(angle) * 250);
      ctx.stroke();
    }
    ctx.restore();
  }
  if (filterId === 'neon') {
    ctx.save();
    ctx.strokeStyle = '#5ee2c1';
    ctx.lineWidth = 16;
    ctx.shadowColor = '#9b6dff';
    ctx.shadowBlur = 24;
    ctx.strokeRect(24, 24, 312, 312);
    ctx.restore();
  }
  if (filterId === 'party') {
    ctx.save();
    ctx.fillStyle = '#ffd23f';
    ctx.strokeStyle = '#1a1f2c';
    ctx.lineWidth = 8;
    ctx.beginPath();
    ctx.moveTo(110, 84);
    ctx.lineTo(142, 34);
    ctx.lineTo(180, 80);
    ctx.lineTo(218, 34);
    ctx.lineTo(250, 84);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = accent;
    for (const [x, y] of [
      [64, 112],
      [300, 126],
      [82, 280],
      [286, 286],
    ]) {
      ctx.beginPath();
      ctx.arc(x, y, 12, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }
  if (filterId === 'retro') {
    ctx.save();
    ctx.fillStyle = 'rgba(255, 210, 63, 0.18)';
    ctx.fillRect(0, 0, 360, 360);
    ctx.strokeStyle = 'rgba(26, 31, 44, 0.24)';
    ctx.lineWidth = 18;
    ctx.strokeRect(18, 18, 324, 324);
    ctx.restore();
  }
  if (filterId === 'pixel') {
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = '#1a1f2c';
    for (let y = 0; y < 360; y += 18) {
      for (let x = 0; x < 360; x += 18) {
        if ((x + y) % 36 === 0) ctx.fillRect(x, y, 9, 9);
      }
    }
    ctx.restore();
  }
}

export function drawProfileEffect(ctx: CanvasRenderingContext2D, effectId: ProfileEffectId, player: PlayerId) {
  const accent = player === 1 ? '#ff5577' : '#3aa3ff';
  if (effectId === 'none') return;

  if (effectId === 'sparkle') {
    ctx.save();
    for (const [index, x, y, size] of [
      [0, 54, 70, 18],
      [1, 300, 96, 22],
      [2, 76, 286, 15],
      [3, 292, 270, 18],
      [4, 210, 42, 13],
    ]) {
      drawSpark(ctx, Number(x), Number(y), Number(size), index % 2 === 0 ? '#ffd23f' : '#ffffff');
    }
    ctx.restore();
  }

  if (effectId === 'lightning') {
    ctx.save();
    ctx.strokeStyle = '#ffd23f';
    ctx.lineWidth = 10;
    ctx.lineJoin = 'round';
    ctx.shadowColor = '#ffffff';
    ctx.shadowBlur = 14;
    drawBolt(ctx, [
      [42, 34],
      [92, 116],
      [66, 116],
      [128, 222],
    ]);
    drawBolt(ctx, [
      [318, 48],
      [268, 132],
      [298, 132],
      [232, 254],
    ]);
    ctx.restore();
  }

  if (effectId === 'bubbles') {
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.86)';
    ctx.lineWidth = 5;
    ctx.fillStyle = 'rgba(94,226,193,0.24)';
    for (const [x, y, radius] of [
      [54, 112, 20],
      [302, 126, 27],
      [82, 290, 18],
      [292, 278, 22],
      [236, 54, 14],
    ]) {
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }

  if (effectId === 'victory') {
    ctx.save();
    ctx.translate(252, 300);
    ctx.rotate(-0.16);
    ctx.fillStyle = '#ffffff';
    ctx.strokeStyle = accent;
    ctx.lineWidth = 8;
    ctx.font = '900 48px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.strokeText('WIN!', 0, 0);
    ctx.fillText('WIN!', 0, 0);
    ctx.restore();
  }
}

function drawSpark(ctx: CanvasRenderingContext2D, x: number, y: number, size: number, color: string) {
  ctx.fillStyle = color;
  ctx.strokeStyle = '#1a1f2c';
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.moveTo(x, y - size);
  ctx.lineTo(x + size * 0.32, y - size * 0.32);
  ctx.lineTo(x + size, y);
  ctx.lineTo(x + size * 0.32, y + size * 0.32);
  ctx.lineTo(x, y + size);
  ctx.lineTo(x - size * 0.32, y + size * 0.32);
  ctx.lineTo(x - size, y);
  ctx.lineTo(x - size * 0.32, y - size * 0.32);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
}

function drawBolt(ctx: CanvasRenderingContext2D, points: [number, number][]) {
  ctx.beginPath();
  points.forEach(([x, y], index) => {
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function getInitials(name: string) {
  const compact = name.replace(/\s/g, '');
  if (!compact) return 'P';
  const playerMatch = compact.match(/^PLAYER([12])$/i);
  if (playerMatch) return `P${playerMatch[1]}`;
  return compact.slice(0, 2).toUpperCase();
}
