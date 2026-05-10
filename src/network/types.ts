import type { InputAction, MiniGameOutcome, PlayerId, PlayerProfile, Scoreboard } from '../types';

export type NetworkRole = PlayerId | 'spectator';

export type NetworkPhase = 'lobby' | 'intro' | 'playing' | 'result' | 'final';

export type NetworkGame = {
  id: 'light-tap' | 'bigger-number' | 'catch-item' | 'color-word' | 'mash-battle';
  name: string;
  instruction: string;
  durationMs: number;
};

export type NetworkRoomState = {
  roomCode: string;
  phase: NetworkPhase;
  round: number;
  totalRounds: number;
  scores: Scoreboard;
  players: Record<PlayerId, boolean>;
  profiles: Partial<Record<PlayerId, PlayerProfile>>;
  game: NetworkGame | null;
  gameState: Record<string, unknown> | null;
  outcome: (MiniGameOutcome & { gameName: string }) | null;
  phaseEndsAt: number | null;
  roundStartedAt: number | null;
  serverNow: number;
  receivedAt: number;
};

export type NetworkClientMessage =
  | { type: 'create'; profile: PlayerProfile }
  | { type: 'join'; roomCode: string; profile: PlayerProfile }
  | { type: 'start' }
  | { type: 'input'; action: InputAction };

export type NetworkServerMessage =
  | { type: 'hello'; clientId: string }
  | { type: 'joined'; roomCode: string; role: NetworkRole }
  | { type: 'state'; state: Omit<NetworkRoomState, 'receivedAt'> }
  | { type: 'error'; message: string };

export type LightTapState = {
  goAt: number;
  punished: Record<PlayerId, boolean>;
  tapped: Record<PlayerId, number | null>;
};

export type BiggerNumberState = {
  numbers: [number, number];
  answers: Record<PlayerId, 'left' | 'right' | null>;
};

export type CatchItemState = {
  lanes: number;
  itemLane: number;
  emoji: string;
  landingDurationMs: number;
  positions: Record<PlayerId, number>;
};

export type ColorWordChoice = {
  id: 'red' | 'blue' | 'yellow';
  label: string;
  inkClass: 'red' | 'blue' | 'yellow';
};

export type ColorWordState = {
  correct: ColorWordChoice;
  ink: ColorWordChoice;
  answers: Record<PlayerId, ColorWordChoice['id'] | null>;
};

export type MashBattleState = {
  counts: Record<PlayerId, number>;
};
