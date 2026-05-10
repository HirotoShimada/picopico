import { useEffect } from 'react';

/** Run `cb` once after `ms` has elapsed. Resets on dep change via React's mount semantics. */
export function useDelay(cb: () => void, ms: number) {
  useEffect(() => {
    const t = window.setTimeout(cb, ms);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms]);
}
