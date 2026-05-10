import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { InputAction, PlayerProfile } from '../types';
import type { NetworkClientMessage, NetworkRole, NetworkRoomState, NetworkServerMessage } from './types';

type ConnectionStatus = 'connecting' | 'open' | 'closed';

function getWebSocketUrl() {
  const configuredUrl = import.meta.env.VITE_WS_URL;
  if (typeof configuredUrl === 'string' && configuredUrl.trim()) return configuredUrl.trim();
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}

export function useNetworkRoom(profile: PlayerProfile) {
  const socketRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>('connecting');
  const [role, setRole] = useState<NetworkRole>('spectator');
  const [roomCode, setRoomCode] = useState('');
  const [room, setRoom] = useState<NetworkRoomState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const socket = new WebSocket(getWebSocketUrl());
    socketRef.current = socket;
    setStatus('connecting');

    socket.addEventListener('open', () => {
      setStatus('open');
      const roomFromUrl = new URLSearchParams(window.location.search).get('room');
      if (roomFromUrl) send(socket, { type: 'join', roomCode: roomFromUrl, profile });
    });
    socket.addEventListener('close', () => setStatus('closed'));
    socket.addEventListener('error', () => setError('サーバーに接続できませんでした'));
    socket.addEventListener('message', (event) => {
      const message = parseMessage(event.data);
      if (!message) return;
      if (message.type === 'joined') {
        setRole(message.role);
        setRoomCode(message.roomCode);
        updateUrlRoom(message.roomCode);
      }
      if (message.type === 'state') {
        setRoom({ ...message.state, receivedAt: Date.now() });
      }
      if (message.type === 'error') setError(message.message);
    });

    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [profile]);

  const sendMessage = useCallback((message: NetworkClientMessage) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setError('まだサーバーに接続していません');
      return;
    }
    send(socket, message);
  }, []);

  const createRoom = useCallback(() => {
    setError(null);
    sendMessage({ type: 'create', profile });
  }, [profile, sendMessage]);

  const joinRoom = useCallback(
    (nextRoomCode: string) => {
      setError(null);
      sendMessage({ type: 'join', roomCode: nextRoomCode, profile });
    },
    [profile, sendMessage],
  );

  const startGame = useCallback(() => {
    setError(null);
    sendMessage({ type: 'start' });
  }, [sendMessage]);

  const sendInput = useCallback(
    (action: InputAction) => {
      sendMessage({ type: 'input', action });
    },
    [sendMessage],
  );

  return useMemo(
    () => ({ status, role, roomCode, room, error, createRoom, joinRoom, startGame, sendInput }),
    [createRoom, error, joinRoom, role, room, roomCode, sendInput, startGame, status],
  );
}

function send(socket: WebSocket, message: NetworkClientMessage) {
  socket.send(JSON.stringify(message));
}

function parseMessage(data: unknown): NetworkServerMessage | null {
  try {
    return JSON.parse(String(data)) as NetworkServerMessage;
  } catch {
    return null;
  }
}

function updateUrlRoom(roomCode: string) {
  const url = new URL(window.location.href);
  url.searchParams.set('room', roomCode);
  window.history.replaceState({}, '', url);
}
