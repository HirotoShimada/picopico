import { useEffect, useRef, useState } from 'react';
import type { InputAction, PlayerId, PlayerInput } from '../types';

const KEY_MAP: Record<string, { player: PlayerId; action: InputAction }> = {
  // 1P
  KeyA: { player: 1, action: 'left' },
  KeyD: { player: 1, action: 'right' },
  Space: { player: 1, action: 'confirm' },
  // 2P
  ArrowLeft: { player: 2, action: 'left' },
  ArrowRight: { player: 2, action: 'right' },
  Enter: { player: 2, action: 'confirm' },
};

/**
 * Global keyboard listener. Returns the most recent key event so consumers
 * can react to it via useEffect on `input?.id`.
 *
 * Disabled rapid-fire from key auto-repeat: ignores `event.repeat`.
 */
export function useInput(): PlayerInput | null {
  const [input, setInput] = useState<PlayerInput | null>(null);
  const idRef = useRef(0);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.repeat) return;
      const mapped = KEY_MAP[event.code];
      if (!mapped) return;
      // Stop the browser from scrolling on Space/Arrow keys etc.
      event.preventDefault();
      idRef.current += 1;
      setInput({ player: mapped.player, action: mapped.action, id: idRef.current });
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return input;
}
