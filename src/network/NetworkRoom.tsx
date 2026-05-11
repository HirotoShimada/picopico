import { useEffect, useMemo, useState } from 'react';
import type { InputAction, PlayerId, PlayerInput, PlayerProfile, PlayerProfiles } from '../types';
import { Hud } from '../components/Hud';
import { ProfileAvatar } from '../components/ProfileAvatar';
import { RoundResultScreen } from '../screens/RoundResultScreen';
import { FinalResultScreen } from '../screens/FinalResultScreen';
import { useFreshInput } from '../hooks/useFreshInput';
import { useNetworkRoom } from './useNetworkRoom';
import type {
  BiggerNumberState,
  CatchItemState,
  ColorWordChoice,
  ColorWordState,
  LightTapState,
  MashBattleState,
  NetworkRoomState,
} from './types';

type Props = {
  input: PlayerInput | null;
  profile: PlayerProfile;
  onBack: () => void;
};

export function NetworkRoom({ input, profile, onBack }: Props) {
  const { status, role, roomCode, room, error, createRoom, joinRoom, startGame, sendInput } = useNetworkRoom(profile);
  const [joinCode, setJoinCode] = useState('');
  const [copied, setCopied] = useState(false);
  const serverNow = useServerNow(room);
  const canPlay = role === 1 || role === 2;

  useFreshInput(input, (event) => {
    if (room?.phase === 'playing' && canPlay) sendInput(event.action);
  });

  const shareUrl = useMemo(() => {
    if (!roomCode) return '';
    const url = new URL(window.location.href);
    url.searchParams.set('room', roomCode);
    return url.toString();
  }, [roomCode]);

  const copyShareUrl = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  const remainingMs = Math.max(0, (room?.phaseEndsAt ?? serverNow) - serverNow);

  return (
    <div className="absolute inset-0">
      {!room && (
        <NetworkLobby
          status={status}
          error={error}
          joinCode={joinCode}
          onJoinCodeChange={setJoinCode}
          onCreate={createRoom}
          onJoin={() => joinRoom(joinCode)}
          onBack={onBack}
        />
      )}

      {room && room.phase === 'lobby' && (
        <NetworkWaitingRoom
          room={room}
          role={role}
          shareUrl={shareUrl}
          copied={copied}
          error={error}
          onCopy={copyShareUrl}
          onStart={startGame}
          onBack={onBack}
        />
      )}

      {room && room.phase === 'intro' && room.game && (
        <NetworkIntro room={room} role={role} remainingMs={remainingMs} />
      )}

      {room && room.phase === 'playing' && room.game && (
        <div className="absolute inset-0">
          <Hud
            round={room.round}
            totalRounds={room.totalRounds}
            scores={room.scores}
            profiles={room.profiles}
            remainingMs={remainingMs}
            totalMs={room.game.durationMs}
          />
          <NetworkRoleBadge room={room} role={role} />
          <NetworkPlayArea room={room} serverNow={serverNow} />
          <NetworkControls disabled={!canPlay} roleLabel={roleLabel(role)} onInput={sendInput} />
        </div>
      )}

      {room && room.phase === 'result' && room.outcome && (
        <RoundResultScreen
          round={room.round}
          totalRounds={room.totalRounds}
          gameName={room.outcome.gameName}
          outcome={room.outcome}
          scores={room.scores}
          profiles={room.profiles}
          input={null}
          onContinue={() => undefined}
        />
      )}

      {room && room.phase === 'final' && (
        <FinalResultScreen scores={room.scores} profiles={room.profiles} input={null} onRestart={startGame} />
      )}
    </div>
  );
}

function NetworkLobby({
  status,
  error,
  joinCode,
  onJoinCodeChange,
  onCreate,
  onJoin,
  onBack,
}: {
  status: string;
  error: string | null;
  joinCode: string;
  onJoinCodeChange: (value: string) => void;
  onCreate: () => void;
  onJoin: () => void;
  onBack: () => void;
}) {
  const isConnected = status === 'open';
  const joinDisabled = !isConnected || !joinCode.trim();
  const disabledButtonClass = 'opacity-45 cursor-not-allowed hover:translate-y-0 hover:shadow-[0_6px_0_#1a1f2c]';

  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-8">
      <button
        type="button"
        onClick={onBack}
        className="absolute left-6 top-6 rounded-full border-4 border-slate-900 bg-white px-5 py-2 text-sm font-black shadow-[0_4px_0_#1a1f2c]"
      >
        タイトルへ
      </button>
      <div className="text-sm font-black tracking-widest text-slate-700">ONLINE BATTLE</div>
      <div className="font-display text-7xl text-pop-violet text-stroke">ネット対戦</div>
      <div className="card flex w-[760px] flex-col gap-5 bg-white/95 px-10 py-8">
        <div className="text-center text-lg font-black text-slate-700">
          ひとりが部屋を作り、もうひとりが同じ部屋コードで参加します
        </div>
        <div className="grid grid-cols-[1fr_auto] gap-4">
          <input
            value={joinCode}
            onChange={(event) => onJoinCodeChange(event.target.value.toUpperCase())}
            maxLength={8}
            placeholder="部屋コード"
            className="rounded-2xl border-4 border-slate-900 bg-white px-5 py-3 text-center text-4xl font-black tracking-widest text-slate-900 shadow-[0_4px_0_#1a1f2c] outline-none"
          />
          <button type="button" onClick={onJoin} disabled={joinDisabled} className={`btn-pop text-2xl ${joinDisabled ? disabledButtonClass : ''}`}>
            参加
          </button>
        </div>
        <button type="button" onClick={onCreate} disabled={!isConnected} className={`btn-pop mx-auto text-2xl ${!isConnected ? disabledButtonClass : ''}`}>
          {isConnected ? '新しい部屋をつくる' : 'サーバー接続中...'}
        </button>
        <div className="text-center text-sm font-bold text-slate-600">
          接続状態: {status === 'open' ? '接続OK' : status === 'connecting' ? '接続中...' : '切断'}
        </div>
        {!isConnected && (
          <div className="text-center text-xs font-bold text-slate-500">
            Render無料サーバーの起動中は、接続OKになるまで30秒ほどかかることがあります。
          </div>
        )}
        {error && <div className="rounded-2xl bg-rose-100 px-4 py-2 text-center text-sm font-black text-rose-700">{error}</div>}
      </div>
    </div>
  );
}

function NetworkWaitingRoom({
  room,
  role,
  shareUrl,
  copied,
  error,
  onCopy,
  onStart,
  onBack,
}: {
  room: NetworkRoomState;
  role: PlayerId | 'spectator';
  shareUrl: string;
  copied: boolean;
  error: string | null;
  onCopy: () => void;
  onStart: () => void;
  onBack: () => void;
}) {
  const ready = room.players[1] && room.players[2];
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-7">
      <button
        type="button"
        onClick={onBack}
        className="absolute left-6 top-6 rounded-full border-4 border-slate-900 bg-white px-5 py-2 text-sm font-black shadow-[0_4px_0_#1a1f2c]"
      >
        タイトルへ
      </button>
      <div className="text-sm font-black tracking-widest text-slate-700">ROOM</div>
      <div className="font-display text-[110px] leading-none text-pop-pink text-stroke">{room.roomCode}</div>
      <div className="grid grid-cols-2 gap-6">
        <PlayerReadyCard player={1} active={room.players[1]} self={role === 1} profile={room.profiles[1]} />
        <PlayerReadyCard player={2} active={room.players[2]} self={role === 2} profile={room.profiles[2]} />
      </div>
      <div className="card flex w-[780px] flex-col gap-3 bg-white/95 px-8 py-5 text-center">
        <div className="text-xl font-black text-slate-900">あなたは {roleLabel(role)}</div>
        <div className="break-all rounded-2xl border-4 border-slate-900 bg-slate-50 px-4 py-3 text-sm font-bold text-slate-700">
          {shareUrl}
        </div>
        <div className="flex justify-center gap-4">
          <button type="button" onClick={onCopy} className="btn-pop text-xl">
            {copied ? 'コピーしました' : '招待リンクをコピー'}
          </button>
          <button type="button" onClick={onStart} disabled={!ready} className={`btn-pop text-xl ${ready ? '' : 'opacity-45'}`}>
            対戦スタート
          </button>
        </div>
        {!ready && <div className="text-sm font-bold text-slate-600">2人そろうと開始できます</div>}
        {error && <div className="rounded-2xl bg-rose-100 px-4 py-2 text-sm font-black text-rose-700">{error}</div>}
      </div>
    </div>
  );
}

function PlayerReadyCard({
  player,
  active,
  self,
  profile,
}: {
  player: PlayerId;
  active: boolean;
  self: boolean;
  profile?: PlayerProfiles[PlayerId];
}) {
  return (
    <div
      className={`card flex w-64 flex-col items-center gap-2 px-6 py-5 text-white ${
        player === 1 ? 'bg-p1' : 'bg-p2'
      } ${self ? (player === 1 ? 'glow-p1' : 'glow-p2') : ''} ${active ? '' : 'opacity-45'}`}
    >
      <ProfileAvatar profile={profile} player={player} size="lg" />
      <div className="text-sm font-extrabold tracking-widest">{profile?.name ?? `PLAYER ${player}`}</div>
      <div className="text-4xl font-black">{active ? '参加中' : '空き'}</div>
      {self && <div className="rounded-full bg-pop-yellow px-3 py-1 text-xs font-black text-slate-900">あなた</div>}
    </div>
  );
}

function NetworkIntro({ room, role, remainingMs }: { room: NetworkRoomState; role: PlayerId | 'spectator'; remainingMs: number }) {
  const countdown = remainingMs > 1800 ? '3' : remainingMs > 1100 ? '2' : remainingMs > 400 ? '1' : 'GO!';
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-7">
      <NetworkRoleBadge room={room} role={role} />
      <div className="text-2xl font-black tracking-widest text-slate-700">
        ROUND {room.round} / {room.totalRounds}
      </div>
      <div className="card relative flex flex-col items-center gap-3 overflow-hidden bg-pop-yellow px-12 py-8">
        <div className="absolute inset-0 stripes opacity-45" />
        <div className="relative text-sm font-black tracking-widest text-slate-900">NEXT GAME</div>
        <div className="relative font-display text-6xl text-pop-violet text-stroke">{room.game?.name}</div>
        <div className="relative max-w-2xl text-center text-base font-bold text-slate-800">{room.game?.instruction}</div>
      </div>
      <div className="countdown-slam font-display text-[128px] leading-none text-pop-pink text-stroke">{countdown}</div>
    </div>
  );
}

function NetworkRoleBadge({ room, role }: { room: NetworkRoomState; role: PlayerId | 'spectator' }) {
  return (
    <div className="absolute left-5 top-24 z-30 flex items-center gap-2 rounded-full border-4 border-slate-900 bg-white/95 px-4 py-2 text-sm font-black shadow-[0_4px_0_#1a1f2c]">
      <span>部屋 {room.roomCode}</span>
      <span className={role === 1 ? 'text-p1' : role === 2 ? 'text-p2' : 'text-slate-500'}>{roleLabel(role)}</span>
    </div>
  );
}

function NetworkPlayArea({ room, serverNow }: { room: NetworkRoomState; serverNow: number }) {
  if (!room.game || !room.gameState) return null;
  return (
    <div className="absolute inset-0 flex items-center justify-center px-10 pb-28 pt-28">
      {room.game.id === 'light-tap' && <NetworkLightTap state={room.gameState as unknown as LightTapState} serverNow={serverNow} />}
      {room.game.id === 'bigger-number' && <NetworkBiggerNumber state={room.gameState as unknown as BiggerNumberState} />}
      {room.game.id === 'catch-item' && (
        <NetworkCatchItem
          state={room.gameState as unknown as CatchItemState}
          startedAt={room.roundStartedAt ?? serverNow}
          serverNow={serverNow}
        />
      )}
      {room.game.id === 'color-word' && <NetworkColorWord state={room.gameState as unknown as ColorWordState} />}
      {room.game.id === 'mash-battle' && <NetworkMashBattle state={room.gameState as unknown as MashBattleState} />}
    </div>
  );
}

function NetworkLightTap({ state, serverNow }: { state: LightTapState; serverNow: number }) {
  const go = serverNow >= state.goAt;
  const bestTap = Math.min(...([1, 2] as PlayerId[]).map((p) => state.tapped[p]).filter((value): value is number => value !== null));
  return (
    <div className="flex flex-col items-center gap-8">
      <div className={`text-4xl font-black ${go ? 'countdown-slam text-emerald-500 text-stroke' : 'text-slate-800'}`}>
        {go ? 'いま押せ！！' : '赤のあいだは押すな！'}
      </div>
      <div
        className={`relative flex h-64 w-64 items-center justify-center rounded-full border-[10px] border-slate-900 ${
          go ? 'lamp-go bg-emerald-400' : 'bg-rose-500'
        }`}
      >
        <div className="absolute inset-3 rounded-full bg-white/30 blur-sm" />
        <div className="relative font-display text-6xl text-white text-stroke">{go ? 'GO!' : 'まて'}</div>
      </div>
      <PlayerMiniResults>
        {([1, 2] as PlayerId[]).map((player) => (
          <PlayerMiniCard key={player} player={player} highlight={state.tapped[player] !== null && state.tapped[player] === bestTap}>
            {state.punished[player] && 'X フライング'}
            {!state.punished[player] && state.tapped[player] !== null && `${Math.round(state.tapped[player] ?? 0)} ms`}
            {!state.punished[player] && state.tapped[player] === null && '...'}
          </PlayerMiniCard>
        ))}
      </PlayerMiniResults>
    </div>
  );
}

function NetworkBiggerNumber({ state }: { state: BiggerNumberState }) {
  return (
    <div className="flex flex-col items-center gap-10">
      <div className="text-3xl font-black text-slate-800">大きいほうの数字を選べ！</div>
      <div className="flex items-center gap-10">
        <NetworkNumberPanel value={state.numbers[0]} side="left" selectedBy={selectedByAnswer(state.answers, 'left')} />
        <div className="text-5xl font-black text-slate-700">VS</div>
        <NetworkNumberPanel value={state.numbers[1]} side="right" selectedBy={selectedByAnswer(state.answers, 'right')} />
      </div>
      <AnswerCards answers={state.answers} format={(answer) => (answer === 'left' ? '← 左' : answer === 'right' ? '右 →' : '...')} />
    </div>
  );
}

function NetworkNumberPanel({ value, side, selectedBy }: { value: number; side: 'left' | 'right'; selectedBy: PlayerId[] }) {
  const tilt = side === 'left' ? '-rotate-3' : 'rotate-3';
  return (
    <div className={`card relative flex h-48 w-48 items-center justify-center bg-white text-7xl font-black text-slate-900 ${tilt}`}>
      <SelectedBadges selectedBy={selectedBy} />
      {value}
    </div>
  );
}

function NetworkCatchItem({ state, startedAt, serverNow }: { state: CatchItemState; startedAt: number; serverNow: number }) {
  const progress = Math.max(0, Math.min(1, (serverNow - startedAt) / state.landingDurationMs));
  return (
    <div className="relative h-[460px] w-[640px] overflow-hidden rounded-[28px] border-4 border-slate-900 bg-gradient-to-b from-sky-200 to-sky-50 shadow-[0_8px_0_#1a1f2c] stripes">
      <div className="absolute inset-0 grid" style={{ gridTemplateColumns: `repeat(${state.lanes}, minmax(0, 1fr))` }}>
        {Array.from({ length: state.lanes }).map((_, lane) => (
          <div key={lane} className="border-r border-dashed border-slate-400/60 last:border-r-0" />
        ))}
      </div>
      <div
        className="absolute text-6xl"
        style={{
          top: `${progress * 78}%`,
          left: `${(state.itemLane + 0.5) * (100 / state.lanes)}%`,
          transform: 'translate(-50%, 0)',
        }}
      >
        {state.emoji}
      </div>
      <div className="absolute inset-x-0 bottom-2 grid h-20" style={{ gridTemplateColumns: `repeat(${state.lanes}, minmax(0, 1fr))` }}>
        {Array.from({ length: state.lanes }).map((_, lane) => (
          <div key={lane} className="relative flex items-end justify-center">
            {state.positions[1] === lane && <NetworkAvatar player={1} side="left" />}
            {state.positions[2] === lane && <NetworkAvatar player={2} side="right" />}
          </div>
        ))}
      </div>
    </div>
  );
}

function NetworkAvatar({ player, side }: { player: PlayerId; side: 'left' | 'right' }) {
  const color = player === 1 ? 'bg-p1' : 'bg-p2';
  const offset = side === 'left' ? '-translate-x-3' : 'translate-x-3';
  return (
    <div
      className={`flex h-16 w-16 items-center justify-center rounded-2xl border-4 border-slate-900 ${color} text-xl font-black text-white shadow-[0_4px_0_#1a1f2c] ${offset}`}
    >
      {player === 1 ? '1P' : '2P'}
    </div>
  );
}

function NetworkColorWord({ state }: { state: ColorWordState }) {
  const choices: ColorWordChoice[] = [
    { id: 'red', label: 'あか', inkClass: 'red' },
    { id: 'blue', label: 'あお', inkClass: 'blue' },
    { id: 'yellow', label: 'きいろ', inkClass: 'yellow' },
  ];
  return (
    <div className="flex flex-col items-center gap-10">
      <div className="text-3xl font-black text-slate-800">この「言葉」のいろを選べ！</div>
      <div className={`card flex h-44 w-[440px] items-center justify-center bg-white text-7xl font-black ${textColor(state.ink.id)}`}>
        {state.correct.label}
      </div>
      <div className="grid grid-cols-3 gap-4">
        {choices.map((choice) => (
          <div key={choice.id} className="flex flex-col items-center gap-2">
            <div className={`card relative flex h-28 w-36 items-center justify-center ${colorBg(choice.id)} text-3xl font-black text-white`}>
              <SelectedBadges selectedBy={selectedByAnswer(state.answers, choice.id)} />
              {choice.label}
            </div>
            <div className="rounded-full border-2 border-slate-900 bg-white px-3 py-1 text-xs font-black shadow-[0_2px_0_#1a1f2c]">
              {choice.id === 'red' ? '左' : choice.id === 'blue' ? '決定' : '右'}
            </div>
          </div>
        ))}
      </div>
      <AnswerCards
        answers={state.answers}
        format={(answer) => choices.find((choice) => choice.id === answer)?.label ?? '...'}
      />
    </div>
  );
}

function NetworkMashBattle({ state }: { state: MashBattleState }) {
  const max = Math.max(state.counts[1], state.counts[2], 1);
  const leader: PlayerId | null = state.counts[1] === state.counts[2] ? null : state.counts[1] > state.counts[2] ? 1 : 2;
  const lead = Math.abs(state.counts[1] - state.counts[2]);
  return (
    <div className="flex flex-col items-center gap-8">
      <div className="text-3xl font-black text-slate-800 animate-wiggle">れんだバトル！！</div>
      <div className="h-10 rounded-full border-4 border-slate-900 bg-white px-6 py-1 text-xl font-black text-slate-900 shadow-[0_4px_0_#1a1f2c]">
        {leader ? `${leader}P リード +${lead}` : '同点！'}
      </div>
      <div className="grid w-[720px] grid-cols-2 gap-8">
        {([1, 2] as PlayerId[]).map((player) => (
          <div key={player} className="flex flex-col items-center gap-3">
            <div
              className={`card relative flex h-40 w-full items-center justify-center text-7xl font-black tabular-nums text-white ${
                player === 1 ? 'bg-p1' : 'bg-p2'
              } ${leader === player ? (player === 1 ? 'glow-p1' : 'glow-p2') : ''}`}
            >
              {state.counts[player]}
            </div>
            <div className="h-6 w-full overflow-hidden rounded-full border-4 border-slate-900 bg-white">
              <div className={`h-full rounded-full ${player === 1 ? 'bg-p1' : 'bg-p2'}`} style={{ width: `${(state.counts[player] / max) * 100}%` }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function NetworkControls({ disabled, roleLabel, onInput }: { disabled: boolean; roleLabel: string; onInput: (action: InputAction) => void }) {
  return (
    <div className="absolute inset-x-0 bottom-5 z-40 flex justify-center px-4">
      <div className={`card flex items-center gap-4 bg-white/95 px-5 py-3 ${disabled ? 'opacity-55' : ''}`}>
        <div className="mr-2 text-sm font-black text-slate-700">{roleLabel}</div>
        <ControlButton label="左" keys="A / ←" disabled={disabled} onClick={() => onInput('left')} />
        <ControlButton label="決定" keys="Space / Enter" disabled={disabled} onClick={() => onInput('confirm')} wide />
        <ControlButton label="右" keys="D / →" disabled={disabled} onClick={() => onInput('right')} />
      </div>
    </div>
  );
}

function ControlButton({
  label,
  keys,
  disabled,
  wide = false,
  onClick,
}: {
  label: string;
  keys: string;
  disabled: boolean;
  wide?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`rounded-2xl border-4 border-slate-900 bg-pop-yellow px-5 py-2 text-center font-black text-slate-900 shadow-[0_4px_0_#1a1f2c] active:translate-y-1 active:shadow-[0_2px_0_#1a1f2c] disabled:cursor-not-allowed ${
        wide ? 'min-w-[150px]' : 'min-w-[92px]'
      }`}
    >
      <div className="text-xl">{label}</div>
      <div className="text-[11px] text-slate-700">{keys}</div>
    </button>
  );
}

function PlayerMiniResults({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-2 gap-4 text-center">{children}</div>;
}

function PlayerMiniCard({ player, highlight, children }: { player: PlayerId; highlight?: boolean; children: React.ReactNode }) {
  return (
    <div
      className={`card min-w-[180px] px-5 py-3 text-white ${player === 1 ? 'bg-p1' : 'bg-p2'} ${
        highlight ? (player === 1 ? 'glow-p1 scale-105' : 'glow-p2 scale-105') : ''
      }`}
    >
      <div className="text-xs font-extrabold tracking-widest">{player === 1 ? '1P' : '2P'}</div>
      <div className="text-2xl font-black">{children}</div>
    </div>
  );
}

function AnswerCards<T extends string>({
  answers,
  format,
}: {
  answers: Record<PlayerId, T | null>;
  format: (answer: T | null) => string;
}) {
  return (
    <PlayerMiniResults>
      {([1, 2] as PlayerId[]).map((player) => (
        <PlayerMiniCard key={player} player={player}>
          {format(answers[player])}
        </PlayerMiniCard>
      ))}
    </PlayerMiniResults>
  );
}

function SelectedBadges({ selectedBy }: { selectedBy: PlayerId[] }) {
  if (selectedBy.length === 0) return null;
  return (
    <div className="absolute -top-5 flex gap-1">
      {selectedBy.map((player) => (
        <span
          key={player}
          className={`rounded-full border-4 border-slate-900 px-3 py-0.5 text-sm font-black text-white shadow-[0_3px_0_#1a1f2c] ${
            player === 1 ? 'bg-p1' : 'bg-p2'
          }`}
        >
          {player}P
        </span>
      ))}
    </div>
  );
}

function selectedByAnswer<T extends string>(answers: Record<PlayerId, T | null>, value: T) {
  return ([1, 2] as PlayerId[]).filter((player) => answers[player] === value);
}

function useServerNow(room: NetworkRoomState | null) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), 80);
    return () => window.clearInterval(timer);
  }, []);
  if (!room) return Date.now();
  return room.serverNow + (Date.now() - room.receivedAt) + tick * 0;
}

function roleLabel(role: PlayerId | 'spectator') {
  if (role === 1) return '1P';
  if (role === 2) return '2P';
  return '観戦';
}

function textColor(id: ColorWordChoice['id']) {
  switch (id) {
    case 'red':
      return 'text-rose-500';
    case 'blue':
      return 'text-sky-500';
    case 'yellow':
      return 'text-amber-400';
  }
}

function colorBg(id: ColorWordChoice['id']) {
  switch (id) {
    case 'red':
      return 'bg-rose-500';
    case 'blue':
      return 'bg-sky-500';
    case 'yellow':
      return 'bg-amber-400';
  }
}
