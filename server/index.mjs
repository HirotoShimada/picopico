import { createReadStream, existsSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocket, WebSocketServer } from 'ws';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const PORT = Number(process.env.PORT ?? 4173);
const DIST_DIR = resolve(__dirname, '..', 'dist');
const TOTAL_ROUNDS = 10;
const PLAYER_IDS = [1, 2];

const MINI_GAMES = [
  {
    id: 'light-tap',
    name: '光ったら押せ',
    instruction: 'ランプが緑になった瞬間に決定キー！ 早押しはフライング',
    durationMs: 5000,
  },
  {
    id: 'bigger-number',
    name: '大きい数字を選べ',
    instruction: '左右のうち大きい方の数字の方向キーを押せ',
    durationMs: 4000,
  },
  {
    id: 'catch-item',
    name: 'おちものをキャッチ',
    instruction: '左右で動いて、落ちてくるアイテムの真下に立とう',
    durationMs: 6000,
  },
  {
    id: 'color-word',
    name: '正しい色を選べ',
    instruction: '言葉の意味どおりの色を選ぼう。文字色にだまされるな！',
    durationMs: 4000,
  },
  {
    id: 'mash-battle',
    name: '連打バトル',
    instruction: '制限時間中に決定キーを連打！多く押した方が勝ち',
    durationMs: 5000,
  },
];

const COLOR_CHOICES = [
  { id: 'red', label: 'あか', inkClass: 'red' },
  { id: 'blue', label: 'あお', inkClass: 'blue' },
  { id: 'yellow', label: 'きいろ', inkClass: 'yellow' },
];

const ITEM_EMOJI = ['🍎', '🍩', '⭐', '🍕', '🎁'];
const MIME_TYPES = new Map([
  ['.html', 'text/html; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.css', 'text/css; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.svg', 'image/svg+xml'],
  ['.png', 'image/png'],
  ['.jpg', 'image/jpeg'],
  ['.jpeg', 'image/jpeg'],
  ['.ico', 'image/x-icon'],
]);

const rooms = new Map();
const heartbeatInterval = setInterval(() => {
  wss.clients.forEach((socket) => {
    if (socket.isAlive === false) {
      socket.terminate();
      return;
    }
    socket.isAlive = false;
    socket.ping();
  });
}, 30_000);

const server = createServer((request, response) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host ?? 'localhost'}`);
  if (url.pathname === '/healthz') {
    response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(
      JSON.stringify({
        ok: true,
        rooms: rooms.size,
        clients: wss.clients.size,
        uptimeSec: Math.round(process.uptime()),
      }),
    );
    return;
  }
  let filePath = resolve(DIST_DIR, `.${decodeURIComponent(url.pathname)}`);
  if (!filePath.startsWith(DIST_DIR)) {
    response.writeHead(403);
    response.end('Forbidden');
    return;
  }
  if (!existsSync(filePath) || url.pathname === '/') {
    filePath = join(DIST_DIR, 'index.html');
  }
  const contentType = MIME_TYPES.get(extname(filePath)) ?? 'application/octet-stream';
  response.writeHead(200, { 'Content-Type': contentType });
  createReadStream(filePath).pipe(response);
});

const wss = new WebSocketServer({ server, path: '/ws' });

wss.on('connection', (socket) => {
  socket.isAlive = true;
  socket.clientId = crypto.randomUUID();
  socket.roomCode = null;
  socket.role = 'spectator';
  send(socket, { type: 'hello', clientId: socket.clientId });

  socket.on('message', (raw) => {
    let message;
    try {
      message = JSON.parse(String(raw));
    } catch {
      send(socket, { type: 'error', message: 'メッセージを読めませんでした' });
      return;
    }

    if (message.type === 'create') {
      joinRoom(socket, createRoomCode(), message.profile);
      return;
    }
    if (message.type === 'join') {
      const roomCode = normalizeRoomCode(message.roomCode);
      if (!roomCode) {
        send(socket, { type: 'error', message: '部屋コードを入力してください' });
        return;
      }
      joinRoom(socket, roomCode, message.profile);
      return;
    }
    if (message.type === 'start') {
      const room = getSocketRoom(socket);
      if (!room) return;
      if (!PLAYER_IDS.includes(socket.role)) {
        send(socket, { type: 'error', message: '観戦中は開始できません' });
        return;
      }
      if (!room.players[1] || !room.players[2]) {
        send(socket, { type: 'error', message: '2人そろうと開始できます' });
        return;
      }
      startGame(room);
      return;
    }
    if (message.type === 'input') {
      const room = getSocketRoom(socket);
      if (!room || !PLAYER_IDS.includes(socket.role)) return;
      handleInput(room, socket.role, message.action);
    }
  });

  socket.on('close', () => leaveRoom(socket));
  socket.on('pong', () => {
    socket.isAlive = true;
  });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`ピコピコバトル network server ready: http://localhost:${PORT}`);
});

process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

function shutdown() {
  clearInterval(heartbeatInterval);
  wss.clients.forEach((socket) => {
    send(socket, { type: 'error', message: 'サーバーを再起動しています。少し待って再読み込みしてください。' });
    socket.close(1001, 'server shutting down');
  });
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 8000).unref();
}

function createRoomCode() {
  const letters = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let code = '';
  do {
    code = Array.from({ length: 4 }, () => letters[Math.floor(Math.random() * letters.length)]).join('');
  } while (rooms.has(code));
  return code;
}

function normalizeRoomCode(value) {
  return String(value ?? '')
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '')
    .slice(0, 8);
}

function sanitizeProfile(profile, role) {
  const fallbackName = role === 1 ? 'PLAYER 1' : 'PLAYER 2';
  const name = String(profile?.name ?? fallbackName).trim().slice(0, 14) || fallbackName;
  const filterId = ['normal', 'comic', 'neon', 'party', 'retro', 'pixel'].includes(profile?.filterId)
    ? profile.filterId
    : 'normal';
  const imageDataUrl =
    typeof profile?.imageDataUrl === 'string' &&
    profile.imageDataUrl.startsWith('data:image/') &&
    profile.imageDataUrl.length < 260_000
      ? profile.imageDataUrl
      : '';
  return { name, filterId, imageDataUrl };
}

function createRoom(roomCode) {
  return {
    roomCode,
    clients: new Set(),
    players: { 1: null, 2: null },
    profiles: { 1: null, 2: null },
    scores: { 1: 0, 2: 0 },
    phase: 'lobby',
    round: 0,
    gameIndex: 0,
    queue: [],
    gameState: null,
    outcome: null,
    phaseEndsAt: null,
    roundStartedAt: null,
    timer: null,
  };
}

function joinRoom(socket, roomCode, profile) {
  leaveRoom(socket);
  const room = rooms.get(roomCode) ?? createRoom(roomCode);
  rooms.set(roomCode, room);

  const role = room.players[1] ? (room.players[2] ? 'spectator' : 2) : 1;
  socket.roomCode = roomCode;
  socket.role = role;
  room.clients.add(socket);
  if (PLAYER_IDS.includes(role)) {
    room.players[role] = socket.clientId;
    room.profiles[role] = sanitizeProfile(profile, role);
  }

  send(socket, { type: 'joined', roomCode, role });
  broadcast(room);
}

function leaveRoom(socket) {
  if (!socket.roomCode) return;
  const room = rooms.get(socket.roomCode);
  if (!room) return;
  room.clients.delete(socket);
  if (PLAYER_IDS.includes(socket.role) && room.players[socket.role] === socket.clientId) {
    room.players[socket.role] = null;
    room.profiles[socket.role] = null;
  }
  socket.roomCode = null;
  socket.role = 'spectator';
  if (room.clients.size === 0) {
    clearRoomTimer(room);
    rooms.delete(room.roomCode);
    return;
  }
  broadcast(room);
}

function getSocketRoom(socket) {
  if (!socket.roomCode) return null;
  return rooms.get(socket.roomCode) ?? null;
}

function startGame(room) {
  clearRoomTimer(room);
  room.scores = { 1: 0, 2: 0 };
  room.round = 1;
  room.gameIndex = 0;
  room.queue = buildRoundQueue(TOTAL_ROUNDS);
  room.outcome = null;
  startIntro(room);
}

function startIntro(room) {
  clearRoomTimer(room);
  room.phase = 'intro';
  room.gameState = null;
  room.outcome = null;
  room.phaseEndsAt = Date.now() + 2600;
  room.roundStartedAt = null;
  broadcast(room);
  setRoomTimer(room, () => startRound(room), 2600);
}

function startRound(room) {
  clearRoomTimer(room);
  const game = room.queue[room.gameIndex];
  const now = Date.now();
  room.phase = 'playing';
  room.outcome = null;
  room.roundStartedAt = now;
  room.gameState = createGameState(game, now);
  room.phaseEndsAt = now + getRoundDuration(game, room.gameState);
  broadcast(room);
  setRoomTimer(room, () => finishRound(room), getRoundDuration(game, room.gameState));
}

function finishRound(room) {
  if (room.phase !== 'playing') return;
  clearRoomTimer(room);
  const game = room.queue[room.gameIndex];
  const outcome = resolveOutcome(game, room.gameState);
  outcome.winners.forEach((player) => {
    room.scores[player] += 1;
  });
  room.phase = 'result';
  room.outcome = { ...outcome, gameName: game.name };
  room.phaseEndsAt = Date.now() + 2500;
  broadcast(room);
  setRoomTimer(room, () => advanceFromResult(room), 2500);
}

function advanceFromResult(room) {
  clearRoomTimer(room);
  if (room.round >= TOTAL_ROUNDS) {
    room.phase = 'final';
    room.phaseEndsAt = null;
    room.gameState = null;
    room.outcome = null;
    broadcast(room);
    return;
  }
  room.round += 1;
  room.gameIndex += 1;
  startIntro(room);
}

function buildRoundQueue(rounds) {
  const queue = [];
  let lastId = null;
  while (queue.length < rounds) {
    const choices = MINI_GAMES.filter((game) => game.id !== lastId);
    const next = choices[Math.floor(Math.random() * choices.length)];
    queue.push(next);
    lastId = next.id;
  }
  return queue;
}

function createGameState(game, now) {
  if (game.id === 'light-tap') {
    const max = Math.max(1500, game.durationMs - 1500);
    return {
      goAt: now + 1000 + Math.random() * (max - 1000),
      punished: { 1: false, 2: false },
      tapped: { 1: null, 2: null },
    };
  }
  if (game.id === 'bigger-number') {
    const left = Math.floor(Math.random() * 90) + 10;
    let right = Math.floor(Math.random() * 90) + 10;
    while (right === left) right = Math.floor(Math.random() * 90) + 10;
    return { numbers: [left, right], answers: { 1: null, 2: null } };
  }
  if (game.id === 'catch-item') {
    return {
      lanes: 5,
      itemLane: Math.floor(Math.random() * 5),
      emoji: ITEM_EMOJI[Math.floor(Math.random() * ITEM_EMOJI.length)],
      landingDurationMs: Math.max(1200, game.durationMs - 650),
      positions: { 1: 2, 2: 2 },
    };
  }
  if (game.id === 'color-word') {
    const correct = COLOR_CHOICES[Math.floor(Math.random() * COLOR_CHOICES.length)];
    const ink =
      Math.random() < 0.3
        ? correct
        : COLOR_CHOICES.filter((choice) => choice.id !== correct.id)[Math.floor(Math.random() * 2)];
    return { correct, ink, answers: { 1: null, 2: null } };
  }
  if (game.id === 'mash-battle') {
    return { counts: { 1: 0, 2: 0 } };
  }
  return {};
}

function getRoundDuration(game, gameState) {
  if (game.id === 'catch-item') return gameState.landingDurationMs;
  return game.durationMs;
}

function handleInput(room, player, action) {
  if (room.phase !== 'playing') return;
  const game = room.queue[room.gameIndex];
  const state = room.gameState;
  if (!state) return;

  if (game.id === 'light-tap') {
    if (action !== 'confirm') return;
    if (state.punished[player] || state.tapped[player] !== null) return;
    const now = Date.now();
    if (now < state.goAt) state.punished[player] = true;
    else state.tapped[player] = Math.max(0, now - state.goAt);
    broadcast(room);
    if (PLAYER_IDS.every((p) => state.punished[p] || state.tapped[p] !== null)) {
      setRoomTimer(room, () => finishRound(room), 350);
    }
    return;
  }

  if (game.id === 'bigger-number') {
    if (action !== 'left' && action !== 'right') return;
    if (state.answers[player]) return;
    state.answers[player] = action;
    broadcast(room);
    if (PLAYER_IDS.every((p) => state.answers[p])) setRoomTimer(room, () => finishRound(room), 250);
    return;
  }

  if (game.id === 'catch-item') {
    if (action !== 'left' && action !== 'right') return;
    const delta = action === 'left' ? -1 : 1;
    state.positions[player] = Math.max(0, Math.min(state.lanes - 1, state.positions[player] + delta));
    broadcast(room);
    return;
  }

  if (game.id === 'color-word') {
    if (state.answers[player]) return;
    const choice = action === 'left' ? 'red' : action === 'confirm' ? 'blue' : action === 'right' ? 'yellow' : null;
    if (!choice) return;
    state.answers[player] = choice;
    broadcast(room);
    if (PLAYER_IDS.every((p) => state.answers[p])) setRoomTimer(room, () => finishRound(room), 250);
    return;
  }

  if (game.id === 'mash-battle') {
    if (action !== 'confirm') return;
    state.counts[player] += 1;
    broadcast(room);
  }
}

function resolveOutcome(game, state) {
  if (game.id === 'light-tap') {
    const winners = [];
    const labels = {};
    const scores = [];
    PLAYER_IDS.forEach((player) => {
      if (state.punished[player]) labels[player] = 'フライング！';
      else if (state.tapped[player] !== null) scores.push({ player, ms: state.tapped[player] });
      else labels[player] = '間に合わず';
    });
    if (scores.length === 0) return { winners, labels };
    const best = Math.min(...scores.map((score) => score.ms));
    scores.forEach(({ player, ms }) => {
      labels[player] = `${Math.round(ms)} ms`;
      if (ms - best <= 30) winners.push(player);
    });
    return { winners, labels };
  }

  if (game.id === 'bigger-number') {
    const correct = state.numbers[0] > state.numbers[1] ? 'left' : 'right';
    return resolveAnswers(state.answers, (answer) => answer === correct);
  }

  if (game.id === 'catch-item') {
    const winners = [];
    const labels = {};
    PLAYER_IDS.forEach((player) => {
      if (state.positions[player] === state.itemLane) {
        winners.push(player);
        labels[player] = 'キャッチ！';
      } else {
        labels[player] = 'のがした...';
      }
    });
    return { winners, labels };
  }

  if (game.id === 'color-word') {
    return resolveAnswers(state.answers, (answer) => answer === state.correct.id);
  }

  if (game.id === 'mash-battle') {
    const labels = { 1: `${state.counts[1]} 回`, 2: `${state.counts[2]} 回` };
    if (state.counts[1] > state.counts[2]) return { winners: [1], labels };
    if (state.counts[2] > state.counts[1]) return { winners: [2], labels };
    return { winners: state.counts[1] > 0 ? [1, 2] : [], labels };
  }

  return { winners: [], labels: {} };
}

function resolveAnswers(answers, isCorrect) {
  const winners = [];
  const labels = {};
  PLAYER_IDS.forEach((player) => {
    if (isCorrect(answers[player])) {
      winners.push(player);
      labels[player] = '正解！';
    } else if (!answers[player]) {
      labels[player] = '時間切れ';
    } else {
      labels[player] = '不正解';
    }
  });
  return { winners, labels };
}

function roomSnapshot(room) {
  const game = room.queue[room.gameIndex] ?? null;
  return {
    roomCode: room.roomCode,
    phase: room.phase,
    round: room.round,
    totalRounds: TOTAL_ROUNDS,
    scores: room.scores,
    players: { 1: Boolean(room.players[1]), 2: Boolean(room.players[2]) },
    profiles: {
      1: room.profiles[1],
      2: room.profiles[2],
    },
    game,
    gameState: room.gameState,
    outcome: room.outcome,
    phaseEndsAt: room.phaseEndsAt,
    roundStartedAt: room.roundStartedAt,
    serverNow: Date.now(),
  };
}

function setRoomTimer(room, callback, delayMs) {
  clearRoomTimer(room);
  room.timer = setTimeout(() => {
    room.timer = null;
    callback();
  }, Math.max(0, delayMs));
}

function clearRoomTimer(room) {
  if (!room.timer) return;
  clearTimeout(room.timer);
  room.timer = null;
}

function broadcast(room) {
  const message = JSON.stringify({ type: 'state', state: roomSnapshot(room) });
  room.clients.forEach((socket) => {
    if (socket.readyState === WebSocket.OPEN) socket.send(message);
  });
}

function send(socket, payload) {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}
