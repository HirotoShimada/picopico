import { useEffect, useRef, useState } from 'react';
import type { MiniGameDefinition, MiniGameOutcome, PlayerInput, PlayerProfiles, Scoreboard } from '../types';
import { Hud } from '../components/Hud';

type Props = {
  game: MiniGameDefinition;
  round: number;
  totalRounds: number;
  scores: Scoreboard;
  profiles?: Partial<PlayerProfiles>;
  input: PlayerInput | null;
  /** Called once when the round resolves (either by the game or by the safety timeout). */
  onRoundComplete: (outcome: MiniGameOutcome) => void;
};

/**
 * Wraps a mini-game with shared HUD chrome and a timer. Each round is mounted
 * with a fresh `key` upstream so internal state always resets cleanly.
 */
export function GameEngine({ game, round, totalRounds, scores, profiles, input, onRoundComplete }: Props) {
  const startRef = useRef(performance.now());
  const [remaining, setRemaining] = useState(game.durationMs);
  const finishedRef = useRef(false);

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const left = Math.max(0, game.durationMs - (performance.now() - startRef.current));
      setRemaining(left);
      if (left <= 0) {
        if (!finishedRef.current) {
          finishedRef.current = true;
          onRoundComplete({ winners: [], labels: { 1: '時間切れ', 2: '時間切れ' } });
        }
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleFinish(outcome: MiniGameOutcome) {
    if (finishedRef.current) return;
    finishedRef.current = true;
    onRoundComplete(outcome);
  }

  const { Component } = game;
  return (
    <div className="absolute inset-0">
      <Hud round={round} totalRounds={totalRounds} scores={scores} profiles={profiles} remainingMs={remaining} totalMs={game.durationMs} />
      <Component durationMs={game.durationMs} input={input} onFinish={handleFinish} />
    </div>
  );
}
