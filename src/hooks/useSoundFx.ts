import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export type SoundCue = 'start' | 'countdown' | 'go' | 'win' | 'fail' | 'score';

type ToneType = OscillatorType;
type WebkitAudioWindow = Window & typeof globalThis & { webkitAudioContext?: typeof AudioContext };

const STORAGE_KEY = 'pico-battle-sound';

export function useSoundFx() {
  const [enabled, setEnabled] = useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) !== 'off';
    } catch {
      return true;
    }
  });
  const audioRef = useRef<AudioContext | null>(null);

  const ensureContext = useCallback(() => {
    const AudioContextClass = window.AudioContext ?? (window as WebkitAudioWindow).webkitAudioContext;
    if (!AudioContextClass) return null;
    if (!audioRef.current) audioRef.current = new AudioContextClass();
    if (audioRef.current.state === 'suspended') {
      void audioRef.current.resume().catch(() => undefined);
    }
    return audioRef.current;
  }, []);

  const playTone = useCallback(
    (ctx: AudioContext, frequency: number, start: number, duration: number, type: ToneType, volume = 0.08) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(frequency, start);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(volume, start + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(start);
      osc.stop(start + duration + 0.03);
    },
    [],
  );

  const play = useCallback(
    (cue: SoundCue) => {
      if (!enabled) return;
      const ctx = ensureContext();
      if (!ctx) return;
      const now = ctx.currentTime;

      switch (cue) {
        case 'start':
          playTone(ctx, 392, now, 0.08, 'square', 0.06);
          playTone(ctx, 587, now + 0.08, 0.1, 'square', 0.06);
          break;
        case 'countdown':
          playTone(ctx, 440, now, 0.08, 'square', 0.05);
          break;
        case 'go':
          playTone(ctx, 523, now, 0.08, 'square', 0.06);
          playTone(ctx, 784, now + 0.08, 0.16, 'triangle', 0.07);
          break;
        case 'win':
          playTone(ctx, 523, now, 0.08, 'triangle', 0.06);
          playTone(ctx, 659, now + 0.08, 0.08, 'triangle', 0.06);
          playTone(ctx, 784, now + 0.16, 0.2, 'triangle', 0.08);
          break;
        case 'fail':
          playTone(ctx, 220, now, 0.12, 'sawtooth', 0.04);
          playTone(ctx, 165, now + 0.1, 0.18, 'sawtooth', 0.035);
          break;
        case 'score':
          playTone(ctx, 880, now, 0.07, 'triangle', 0.05);
          break;
      }
    },
    [enabled, ensureContext, playTone],
  );

  const toggle = useCallback(() => {
    setEnabled((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(STORAGE_KEY, next ? 'on' : 'off');
      } catch {
        // Ignore storage failures; the toggle still works for this session.
      }
      if (next) {
        const ctx = ensureContext();
        if (ctx) playTone(ctx, 740, ctx.currentTime, 0.08, 'triangle', 0.05);
      }
      return next;
    });
  }, [ensureContext, playTone]);

  useEffect(() => {
    if (!enabled) return;
    const unlock = () => {
      const ctx = ensureContext();
      if (!ctx) return;
      playTone(ctx, 440, ctx.currentTime, 0.01, 'sine', 0.0001);
    };
    window.addEventListener('pointerdown', unlock, { once: true });
    window.addEventListener('keydown', unlock, { once: true });
    return () => {
      window.removeEventListener('pointerdown', unlock);
      window.removeEventListener('keydown', unlock);
    };
  }, [enabled, ensureContext, playTone]);

  return useMemo(() => ({ enabled, play, toggle }), [enabled, play, toggle]);
}
