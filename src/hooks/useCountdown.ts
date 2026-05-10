import { useEffect, useRef, useState } from 'react';

/**
 * Countdown timer in milliseconds. Calls `onElapsed` exactly once when it
 * reaches zero. The callback always sees the latest closure (we keep it in a
 * ref) so callers can safely read fresh React state inside it.
 */
export function useCountdown(durationMs: number, onElapsed?: () => void) {
  const [remaining, setRemaining] = useState(durationMs);
  const calledRef = useRef(false);
  const startRef = useRef<number>(performance.now());
  const cbRef = useRef(onElapsed);
  cbRef.current = onElapsed;

  useEffect(() => {
    calledRef.current = false;
    startRef.current = performance.now();
    let raf = 0;
    const tick = () => {
      const elapsed = performance.now() - startRef.current;
      const left = Math.max(0, durationMs - elapsed);
      setRemaining(left);
      if (left <= 0) {
        if (!calledRef.current) {
          calledRef.current = true;
          cbRef.current?.();
        }
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [durationMs]);

  return remaining;
}
