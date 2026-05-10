import { useEffect, useRef } from 'react';
import type { PlayerInput } from '../types';

/**
 * Calls `handler` only for genuinely new key events that arrived AFTER the
 * component mounted. The mount-time `input` (which could still be the press
 * that triggered the previous screen transition) is silently consumed.
 */
export function useFreshInput(input: PlayerInput | null, handler: (e: PlayerInput) => void) {
  const baselineRef = useRef<number | null>(null);
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    // Initialize baseline from whatever id the input had at mount.
    if (baselineRef.current === null) {
      baselineRef.current = input?.id ?? 0;
      return;
    }
    if (!input) return;
    if (input.id <= baselineRef.current) return;
    baselineRef.current = input.id;
    handlerRef.current(input);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input?.id]);
}
