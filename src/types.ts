export type PlayerId = 1 | 2;

export type InputAction = 'left' | 'right' | 'confirm';

export type ProfileFilterId = 'normal' | 'comic' | 'neon' | 'party' | 'retro' | 'pixel';

export type PlayerProfile = {
  name: string;
  imageDataUrl: string;
  filterId: ProfileFilterId;
};

export type PlayerProfiles = Record<PlayerId, PlayerProfile>;

export type PlayerInput = {
  player: PlayerId;
  action: InputAction;
  /** monotonically increasing event id, used to detect new presses inside React hooks */
  id: number;
};

export type MiniGameOutcome = {
  /** Players who succeeded this round (each gets 1 point). */
  winners: PlayerId[];
  /** Optional per-player labels for the result screen ("成功" / "失敗" / custom). */
  labels?: Partial<Record<PlayerId, string>>;
};

export type MiniGameProps = {
  /** Total time budget for this mini game in milliseconds (3000-8000). */
  durationMs: number;
  /** Stream of fresh key presses. The newest event has the largest id. */
  input: PlayerInput | null;
  /** Called exactly once when the game decides the round is over. */
  onFinish: (outcome: MiniGameOutcome) => void;
};

export type MiniGameDefinition = {
  id: string;
  name: string;
  /** Short instruction shown on the round-intro screen. */
  instruction: string;
  durationMs: number;
  /** React component that implements the game. */
  Component: React.ComponentType<MiniGameProps>;
};

export type Phase =
  | { kind: 'title' }
  | { kind: 'intro'; round: number; gameIndex: number }
  | { kind: 'playing'; round: number; gameIndex: number }
  | { kind: 'result'; round: number; outcome: MiniGameOutcome; gameName: string }
  | { kind: 'final' };

export type Scoreboard = Record<PlayerId, number>;
