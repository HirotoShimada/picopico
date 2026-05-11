import { useCallback, useEffect, useState } from 'react';
import type { PlayerProfiles } from './types';
import { useInput } from './hooks/useInput';
import { useSoundFx } from './hooks/useSoundFx';
import { hasSavedProfiles, loadProfiles, saveProfiles } from './profile/profileStorage';
import { ProfileSetupScreen } from './screens/ProfileSetupScreen';
import { TitleScreen } from './screens/TitleScreen';
import { SoundToggle } from './components/SoundToggle';
import { NetworkRoom } from './network/NetworkRoom';

const STAGE_WIDTH = 1280;
const STAGE_HEIGHT = 760;

export default function App() {
  const input = useInput();
  const sound = useSoundFx();

  const [profiles, setProfiles] = useState<PlayerProfiles>(() => loadProfiles());
  const [profileReady, setProfileReady] = useState(() => hasSavedProfiles());
  const [mode, setMode] = useState<'title' | 'network'>(() =>
    new URLSearchParams(window.location.search).has('room') ? 'network' : 'title',
  );

  const openNetworkMode = useCallback(() => {
    sound.play('start');
    setMode('network');
  }, [sound]);

  const closeNetworkMode = useCallback(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete('room');
    window.history.replaceState({}, '', url);
    setMode('title');
  }, []);

  const completeProfiles = useCallback((nextProfiles: PlayerProfiles) => {
    setProfiles(nextProfiles);
    saveProfiles(nextProfiles);
    setProfileReady(true);
  }, []);

  const editProfiles = useCallback(() => {
    setMode('title');
    setProfileReady(false);
  }, []);

  return (
    <div className="relative h-full w-full overflow-hidden">
      <Stage>
        <SoundToggle enabled={sound.enabled} onToggle={sound.toggle} />
        {!profileReady && <ProfileSetupScreen initialProfiles={profiles} onComplete={completeProfiles} />}
        {profileReady && mode === 'title' && (
          <TitleScreen input={input} profiles={profiles} onNetworkMode={openNetworkMode} onEditProfiles={editProfiles} />
        )}
        {profileReady && mode === 'network' && <NetworkRoom input={input} profile={profiles[1]} onBack={closeNetworkMode} />}
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
