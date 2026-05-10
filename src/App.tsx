import { useCallback, useEffect, useMemo, useState } from 'react';
import type { MiniGameDefinition, MiniGameOutcome, Phase, PlayerId, PlayerProfiles, Scoreboard } from './types';
import { useInput } from './hooks/useInput';
import { useSoundFx } from './hooks/useSoundFx';
import { hasSavedProfiles, loadProfiles, saveProfiles } from './profile/profileStorage';
import { buildRoundQueue } from './games/registry';
import { ProfileSetupScreen } from './screens/ProfileSetupScreen';
import { TitleScreen } from './screens/TitleScreen';
import { RoundIntroScreen } from './screens/RoundIntroScreen';
import { RoundResultScreen } from './screens/RoundResultScreen';
import { FinalResultScreen } from './screens/FinalResultScreen';
import { GameEngine } from './engine/GameEngine';
import { SoundToggle } from './components/SoundToggle';
import { NetworkRoom } from './network/NetworkRoom';

const TOTAL_ROUNDS = 10;
const STAGE_WIDTH = 1280;
const STAGE_HEIGHT = 760;

const EMPTY_SCORES: Scoreboard = { 1: 0, 2: 0 };

export default function App() {
  const input = useInput();
  const sound = useSoundFx();

  const [profiles, setProfiles] = useState<PlayerProfiles>(() => loadProfiles());
  const [profileReady, setProfileReady] = useState(() => hasSavedProfiles());
  const [phase, setPhase] = useState<Phase>({ kind: 'title' });
  const [mode, setMode] = useState<'local' | 'network'>(() =>
    new URLSearchParams(window.location.search).has('room') ? 'network' : 'local',
  );
  const [scores, setScores] = useState<Scoreboard>(EMPTY_SCORES);
  const [queue, setQueue] = useState<MiniGameDefinition[]>([]);
  /** Bumped on each fresh game-engine mount so internal state resets cleanly. */
  const [engineKey, setEngineKey] = useState(0);

  const startNewGame = useCallback(() => {
    sound.play('start');
    setQueue(buildRoundQueue(TOTAL_ROUNDS));
    setScores(EMPTY_SCORES);
    setPhase({ kind: 'intro', round: 1, gameIndex: 0 });
  }, [sound]);

  const openNetworkMode = useCallback(() => {
    setMode('network');
    setPhase({ kind: 'title' });
  }, []);

  const closeNetworkMode = useCallback(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete('room');
    window.history.replaceState({}, '', url);
    setMode('local');
    setPhase({ kind: 'title' });
  }, []);

  const completeProfiles = useCallback((nextProfiles: PlayerProfiles) => {
    setProfiles(nextProfiles);
    saveProfiles(nextProfiles);
    setProfileReady(true);
  }, []);

  const editProfiles = useCallback(() => setProfileReady(false), []);

  const startRound = useCallback(() => {
    setPhase((p) => (p.kind === 'intro' ? { kind: 'playing', round: p.round, gameIndex: p.gameIndex } : p));
    setEngineKey((k) => k + 1);
  }, []);

  const handleRoundComplete = useCallback(
    (outcome: MiniGameOutcome) => {
      setPhase((p) => {
        if (p.kind !== 'playing') return p;
        const game = queue[p.gameIndex];
        // Apply scores.
        setScores((s) => {
          const next: Scoreboard = { ...s };
          outcome.winners.forEach((w: PlayerId) => {
            next[w] = next[w] + 1;
          });
          return next;
        });
        return { kind: 'result', round: p.round, outcome, gameName: game.name };
      });
    },
    [queue],
  );

  const advanceFromResult = useCallback(() => {
    setPhase((p) => {
      if (p.kind !== 'result') return p;
      if (p.round >= TOTAL_ROUNDS) return { kind: 'final' };
      const nextRound = p.round + 1;
      return { kind: 'intro', round: nextRound, gameIndex: nextRound - 1 };
    });
  }, []);

  const restart = useCallback(() => setPhase({ kind: 'title' }), []);

  const currentGame = useMemo(() => {
    if (phase.kind === 'intro' || phase.kind === 'playing') return queue[phase.gameIndex];
    return null;
  }, [phase, queue]);

  return (
    <div className="relative h-full w-full overflow-hidden">
      <Stage>
        <SoundToggle enabled={sound.enabled} onToggle={sound.toggle} />
        {!profileReady && <ProfileSetupScreen initialProfiles={profiles} onComplete={completeProfiles} />}
        {profileReady && mode === 'network' && <NetworkRoom input={input} profile={profiles[1]} onBack={closeNetworkMode} />}
        {profileReady && mode === 'local' && phase.kind === 'title' && (
          <TitleScreen
            input={input}
            profiles={profiles}
            onStart={startNewGame}
            onNetworkMode={openNetworkMode}
            onEditProfiles={editProfiles}
          />
        )}
        {profileReady && mode === 'local' && phase.kind === 'intro' && currentGame && (
          <RoundIntroScreen
            round={phase.round}
            totalRounds={TOTAL_ROUNDS}
            game={currentGame}
            scores={scores}
            profiles={profiles}
            onCue={sound.play}
            onReady={startRound}
          />
        )}
        {profileReady && mode === 'local' && phase.kind === 'playing' && currentGame && (
          <GameEngine
            key={engineKey}
            game={currentGame}
            round={phase.round}
            totalRounds={TOTAL_ROUNDS}
            scores={scores}
            profiles={profiles}
            input={input}
            onRoundComplete={handleRoundComplete}
          />
        )}
        {profileReady && mode === 'local' && phase.kind === 'result' && (
          <RoundResultScreen
            round={phase.round}
            totalRounds={TOTAL_ROUNDS}
            gameName={phase.gameName}
            outcome={phase.outcome}
            scores={scores}
            profiles={profiles}
            input={input}
            onReveal={sound.play}
            onContinue={advanceFromResult}
          />
        )}
        {profileReady && mode === 'local' && phase.kind === 'final' && (
          <FinalResultScreen scores={scores} profiles={profiles} input={input} onRestart={restart} onReveal={sound.play} />
        )}
      </Stage>
    </div>
  );
}

/** Fixed 16:9-ish stage that scales the game to the viewport. */
function Stage({ children }: { children: React.ReactNode }) {
  const [scale, setScale] = useState(1);

  useEffect(() => {
    const fitToViewport = () => {
      const padding = 24;
      const availableWidth = Math.max(320, window.innerWidth - padding);
      const availableHeight = Math.max(240, window.innerHeight - padding);
      setScale(Math.min(1, availableWidth / STAGE_WIDTH, availableHeight / STAGE_HEIGHT));
    };
    fitToViewport();
    window.addEventListener('resize', fitToViewport);
    return () => window.removeEventListener('resize', fitToViewport);
  }, []);

  return (
    <div className="absolute inset-0 flex items-center justify-center overflow-hidden p-3">
      <div style={{ width: STAGE_WIDTH * scale, height: STAGE_HEIGHT * scale }}>
        <div
          className="relative h-[760px] w-[1280px] overflow-hidden rounded-[36px] border-4 border-slate-900 bg-white/40 shadow-[0_12px_0_#1a1f2c] backdrop-blur-sm"
          style={{ transform: `scale(${scale})`, transformOrigin: 'top left' }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}
