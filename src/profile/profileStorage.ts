import type { PlayerId, PlayerProfile, PlayerProfiles, ProfileFilterId } from '../types';

const STORAGE_KEY = 'pico-battle-profiles-v1';

export const PROFILE_FILTERS: { id: ProfileFilterId; label: string; cssFilter: string; canvasFilter: string }[] = [
  { id: 'normal', label: 'そのまま', cssFilter: 'none', canvasFilter: 'none' },
  { id: 'comic', label: 'マンガ集中線', cssFilter: 'contrast(1.35) saturate(1.3)', canvasFilter: 'contrast(135%) saturate(130%)' },
  { id: 'neon', label: 'ネオン発光', cssFilter: 'contrast(1.2) saturate(1.8) hue-rotate(18deg)', canvasFilter: 'contrast(120%) saturate(180%) hue-rotate(18deg)' },
  { id: 'party', label: 'パーティー王冠', cssFilter: 'saturate(1.55) brightness(1.08)', canvasFilter: 'saturate(155%) brightness(108%)' },
  { id: 'retro', label: 'レトロ写真', cssFilter: 'sepia(0.75) contrast(1.15)', canvasFilter: 'sepia(75%) contrast(115%)' },
  { id: 'pixel', label: 'ピクセル風', cssFilter: 'contrast(1.25) saturate(1.15)', canvasFilter: 'contrast(125%) saturate(115%)' },
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
  return {
    name: sanitizeName(profile?.name) || fallback.name,
    imageDataUrl: typeof profile?.imageDataUrl === 'string' && profile.imageDataUrl.startsWith('data:image/') ? profile.imageDataUrl : fallback.imageDataUrl,
    filterId,
  };
}

export function sanitizeName(name: string | null | undefined) {
  return String(name ?? '')
    .trim()
    .slice(0, 14);
}

export function createInitialAvatar(name: string, player: PlayerId, filterId: ProfileFilterId = 'normal') {
  const canvas = document.createElement('canvas');
  canvas.width = 360;
  canvas.height = 360;
  const ctx = canvas.getContext('2d');
  if (!ctx) return '';

  const gradient = ctx.createLinearGradient(0, 0, 360, 360);
  gradient.addColorStop(0, player === 1 ? '#ff5577' : '#3aa3ff');
  gradient.addColorStop(1, '#ffd23f');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 360, 360);

  ctx.fillStyle = 'rgba(255,255,255,0.18)';
  for (let i = -260; i < 420; i += 38) {
    ctx.fillRect(i, 0, 18, 520);
  }

  drawFilterOverlay(ctx, filterId, player);
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

function getInitials(name: string) {
  const compact = name.replace(/\s/g, '');
  if (!compact) return 'P';
  const playerMatch = compact.match(/^PLAYER([12])$/i);
  if (playerMatch) return `P${playerMatch[1]}`;
  return compact.slice(0, 2).toUpperCase();
}
