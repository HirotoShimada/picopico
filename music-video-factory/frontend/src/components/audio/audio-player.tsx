"use client";

import * as React from "react";

/**
 * One `<audio>` element for the whole project workspace, shared by the
 * waveform, the lyric timing table and the lyric preview. Playback time is
 * published through a tiny external store so per-frame consumers (playhead,
 * preview) do not re-render the whole page.
 */

export interface AudioPlayerState {
  /** True once metadata is loaded and the element can seek. */
  ready: boolean;
  playing: boolean;
  /** Element duration in seconds (0 until known). */
  duration: number;
  /** Japanese message when the source failed to load. */
  error: string | null;
  rate: number;
  hasSource: boolean;
}

export interface PlayRangeOptions {
  /** Restart at `start` when `end` is reached instead of pausing. */
  loop?: boolean;
}

type Listener = () => void;

const INITIAL_STATE: AudioPlayerState = {
  ready: false,
  playing: false,
  duration: 0,
  error: null,
  rate: 1,
  hasSource: false,
};

export class AudioPlayerStore {
  private element: HTMLAudioElement | null = null;
  private state: AudioPlayerState = INITIAL_STATE;
  private time = 0;
  private readonly stateListeners = new Set<Listener>();
  private readonly timeListeners = new Set<Listener>();
  private frame: number | null = null;
  private rangeEnd: number | null = null;
  private loopStart: number | null = null;
  private pendingSeek: number | null = null;
  private pendingPlay = false;
  private detachEvents: (() => void) | null = null;

  // --- subscriptions (useSyncExternalStore) -------------------------------------------

  readonly subscribeState = (listener: Listener): (() => void) => {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  };

  readonly subscribeTime = (listener: Listener): (() => void) => {
    this.timeListeners.add(listener);
    return () => this.timeListeners.delete(listener);
  };

  readonly getState = (): AudioPlayerState => this.state;
  readonly getTime = (): number => this.time;
  readonly getServerState = (): AudioPlayerState => INITIAL_STATE;
  readonly getServerTime = (): number => 0;

  private setState(patch: Partial<AudioPlayerState>): void {
    const next = { ...this.state, ...patch };
    const changed = (Object.keys(patch) as (keyof AudioPlayerState)[]).some((key) => next[key] !== this.state[key]);
    if (!changed) return;
    this.state = next;
    for (const listener of this.stateListeners) listener();
  }

  private emitTime(): void {
    for (const listener of this.timeListeners) listener();
  }

  private readElementTime(): void {
    if (this.element) this.time = this.element.currentTime;
  }

  // --- element lifecycle -----------------------------------------------------------------

  attach(element: HTMLAudioElement | null): void {
    this.detachEvents?.();
    this.detachEvents = null;
    this.element = element;
    if (!element) {
      this.stopLoop();
      this.setState({ ready: false, playing: false });
      return;
    }

    const onMetadata = () => {
      this.setState({ ready: true, duration: Number.isFinite(element.duration) ? element.duration : 0, error: null });
      if (this.pendingSeek !== null) {
        element.currentTime = this.pendingSeek;
        this.time = this.pendingSeek;
        this.pendingSeek = null;
        this.emitTime();
      }
      if (this.pendingPlay) {
        this.pendingPlay = false;
        void this.play();
      }
    };
    const onPlay = () => {
      this.setState({ playing: true });
      this.startLoop();
    };
    const onPause = () => {
      this.setState({ playing: false });
      this.stopLoop();
      this.readElementTime();
      this.emitTime();
    };
    const onTime = () => {
      if (this.frame !== null) return; // the animation loop already publishes the time
      this.readElementTime();
      this.emitTime();
    };
    const onError = () => {
      this.stopLoop();
      this.setState({ ready: false, playing: false, error: "音源を読み込めませんでした。" });
    };
    const onEmptied = () => this.setState({ ready: false, playing: false });
    const onRate = () => this.setState({ rate: element.playbackRate });
    const onDuration = () => {
      if (Number.isFinite(element.duration)) this.setState({ duration: element.duration });
    };

    const events: [keyof HTMLMediaElementEventMap, () => void][] = [
      ["loadedmetadata", onMetadata],
      ["play", onPlay],
      ["pause", onPause],
      ["ended", onPause],
      ["seeked", onTime],
      ["timeupdate", onTime],
      ["error", onError],
      ["emptied", onEmptied],
      ["ratechange", onRate],
      ["durationchange", onDuration],
    ];
    for (const [name, handler] of events) element.addEventListener(name, handler);
    this.detachEvents = () => {
      for (const [name, handler] of events) element.removeEventListener(name, handler);
    };

    element.playbackRate = this.state.rate;
    if (element.readyState >= HTMLMediaElement.HAVE_METADATA) onMetadata();
  }

  /** Called when the `src` attribute changes. `resumeAt` restores the position (URL refresh). */
  sourceChanged(hasSource: boolean, resumeAt: number | null): void {
    this.stopLoop();
    this.rangeEnd = null;
    this.loopStart = null;
    this.pendingSeek = resumeAt;
    this.pendingPlay = false;
    if (resumeAt === null) {
      this.time = 0;
      this.emitTime();
    }
    this.setState({ hasSource, ready: false, playing: false, error: null, duration: 0 });
  }

  // --- animation loop while playing -----------------------------------------------------------

  private readonly tick = () => {
    const element = this.element;
    if (!element) {
      this.frame = null;
      return;
    }
    this.time = element.currentTime;
    if (this.rangeEnd !== null && this.time >= this.rangeEnd) {
      if (this.loopStart !== null) {
        element.currentTime = this.loopStart;
        this.time = this.loopStart;
      } else {
        const end = this.rangeEnd;
        this.rangeEnd = null;
        element.pause();
        element.currentTime = end;
        this.time = end;
      }
    }
    this.emitTime();
    this.frame = window.requestAnimationFrame(this.tick);
  };

  private startLoop(): void {
    if (this.frame === null && typeof window !== "undefined") this.frame = window.requestAnimationFrame(this.tick);
  }

  private stopLoop(): void {
    if (this.frame !== null) {
      window.cancelAnimationFrame(this.frame);
      this.frame = null;
    }
  }

  // --- controls ---------------------------------------------------------------------------------

  readonly play = async (): Promise<void> => {
    const element = this.element;
    if (!element || !this.state.hasSource) return;
    if (!this.state.ready) {
      this.pendingPlay = true;
      return;
    }
    try {
      await element.play();
    } catch {
      // AbortError (interrupted by pause()/load()) or NotAllowedError (autoplay policy):
      // playback is always user-initiated here, so the user can simply press play again.
    }
  };

  readonly pause = (): void => {
    this.pendingPlay = false;
    this.element?.pause();
  };

  readonly toggle = (): void => {
    if (this.state.playing) this.pause();
    else void this.play();
  };

  private seekInternal(seconds: number): void {
    const duration = this.state.duration || this.element?.duration || 0;
    const target = Math.max(0, Number.isFinite(duration) && duration > 0 ? Math.min(seconds, duration) : seconds);
    this.time = target;
    if (this.element && this.state.ready) this.element.currentTime = target;
    else this.pendingSeek = target;
    this.emitTime();
  }

  /** Jump to `seconds` (cancels a range playback). */
  readonly seek = (seconds: number): void => {
    this.rangeEnd = null;
    this.loopStart = null;
    this.seekInternal(seconds);
  };

  readonly skip = (deltaSeconds: number): void => {
    this.seek(this.time + deltaSeconds);
  };

  /** Play from `start` and stop (or loop) at `end`. */
  readonly playRange = (start: number, end: number, options: PlayRangeOptions = {}): void => {
    this.seekInternal(start);
    this.rangeEnd = end;
    this.loopStart = options.loop ? start : null;
    void this.play();
  };

  /** Play from `seconds` without an end. */
  readonly playFrom = (seconds: number): void => {
    this.seek(seconds);
    void this.play();
  };

  readonly setRate = (rate: number): void => {
    this.setState({ rate });
    if (this.element) this.element.playbackRate = rate;
  };

  readonly clearError = (): void => this.setState({ error: null });

}

const AudioPlayerContext = React.createContext<AudioPlayerStore | null>(null);

export function useAudioPlayer(): AudioPlayerStore {
  const store = React.useContext(AudioPlayerContext);
  if (!store) throw new Error("useAudioPlayer must be used inside <AudioPlayerProvider>");
  return store;
}

/** Playback state (re-renders on play/pause/ready changes, not per frame). */
export function useAudioState(): AudioPlayerState {
  const store = useAudioPlayer();
  return React.useSyncExternalStore(store.subscribeState, store.getState, store.getServerState);
}

/** Current playback time; re-renders every animation frame while playing. */
export function useAudioTime(): number {
  const store = useAudioPlayer();
  return React.useSyncExternalStore(store.subscribeTime, store.getTime, store.getServerTime);
}

/**
 * A primitive derived from the playback time (e.g. the active lyric index).
 * Re-renders only when the derived value changes.
 */
export function useAudioTimeSelector<T extends string | number | boolean | null>(select: (time: number) => T): T {
  const store = useAudioPlayer();
  return React.useSyncExternalStore(
    store.subscribeTime,
    () => select(store.getTime()),
    () => select(0),
  );
}

/** Calls `listener(time)` on every time update without re-rendering (imperative DOM updates). */
export function useAudioTimeListener(listener: (time: number) => void): void {
  const store = useAudioPlayer();
  const onTime = React.useEffectEvent(listener);
  React.useEffect(() => {
    onTime(store.getTime());
    return store.subscribeTime(() => onTime(store.getTime()));
  }, [store]);
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON" || tag === "A") return true;
  if (tag === "INPUT") {
    const type = (target as HTMLInputElement).type;
    return type !== "checkbox" && type !== "radio" && type !== "range";
  }
  const role = target.getAttribute("role");
  return role === "slider" || role === "spinbutton" || role === "textbox" || role === "menuitem" || role === "tab";
}

export interface AudioHotkeyOptions {
  /** Extra single-key shortcuts (lower-case `event.key`). */
  extra?: Record<string, () => void>;
}

/**
 * Keyboard shortcuts while the calling component is visible:
 * Space = play/pause, ←/→ = ∓5 s (with Shift: ∓1 s), plus `extra` keys.
 * Ignored while typing in a field or when a button/slider has focus.
 */
export function useAudioHotkeys({ extra }: AudioHotkeyOptions = {}): void {
  const store = useAudioPlayer();
  const onKeyDown = React.useEffectEvent((event: KeyboardEvent) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    if (isTypingTarget(event.target)) return;
    if (document.querySelector("[role=dialog],[role=alertdialog]")) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if (key === " ") {
      event.preventDefault();
      store.toggle();
    } else if (key === "ArrowLeft" || key === "ArrowRight") {
      event.preventDefault();
      const step = event.shiftKey ? 1 : 5;
      store.skip(key === "ArrowLeft" ? -step : step);
    } else if (extra && key in extra) {
      event.preventDefault();
      extra[key]();
    }
  });
  React.useEffect(() => {
    const handler = (event: KeyboardEvent) => onKeyDown(event);
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}

export interface AudioPlayerProviderProps {
  /** Signed media URL of the song (`project.audio.url`). */
  src: string | null;
  /** Identity of the audio file (asset id). A new key loads the new file from 0:00. */
  sourceKey: string | null;
  /** Fetch a fresh signed URL (e.g. revalidate the project) after the source failed to load. */
  onSourceError?: () => void;
  children: React.ReactNode;
}

interface LoadedSource {
  key: string | null;
  src: string | null;
}

/** Automatic URL refreshes per file before the error is left to the user ("再読み込み"). */
const MAX_AUTO_RELOADS = 1;

const ReloadContext = React.createContext<() => void>(() => {});

/** Re-fetches a signed URL for the song after a load error. */
export function useAudioReload(): () => void {
  return React.useContext(ReloadContext);
}

/**
 * Provides the shared player. Signed URLs are re-issued on every project fetch;
 * the element keeps its current URL (and position) until the file changes or
 * the URL stops working, in which case the newest URL is loaded at the same position.
 */
export function AudioPlayerProvider({ src, sourceKey, onSourceError, children }: AudioPlayerProviderProps) {
  const [store] = React.useState(() => new AudioPlayerStore());
  const [loaded, setLoaded] = React.useState<LoadedSource>({ key: sourceKey, src });
  const [refreshWanted, setRefreshWanted] = React.useState(false);
  const state = React.useSyncExternalStore(store.subscribeState, store.getState, store.getServerState);

  const urlFailed = (state.error !== null || refreshWanted) && !!src && src !== loaded.src;
  if (loaded.key !== sourceKey || (!loaded.src && src) || urlFailed) {
    // New file, first URL, or the loaded URL failed (likely expired) and a fresh one is available.
    setLoaded({ key: sourceKey, src });
    if (refreshWanted) setRefreshWanted(false);
  }

  const loadedKey = loaded.key;
  const loadedSrc = loaded.src;
  const previous = React.useRef<LoadedSource>({ key: null, src: null });
  // Layout effect: runs before the element can dispatch media events for the new `src`.
  React.useLayoutEffect(() => {
    const before = previous.current;
    previous.current = { key: loadedKey, src: loadedSrc };
    if (before.key === loadedKey && before.src === loadedSrc) return;
    const sameFile = before.key === loadedKey && before.src !== null;
    store.sourceChanged(!!loadedSrc, sameFile ? store.getTime() : null);
  }, [store, loadedKey, loadedSrc]);

  const autoReloads = React.useRef<{ key: string | null; count: number }>({ key: null, count: 0 });
  const requestUrl = React.useEffectEvent(() => onSourceError?.());
  React.useEffect(() => {
    if (state.ready) autoReloads.current = { key: loadedKey, count: 0 };
  }, [state.ready, loadedKey]);
  React.useEffect(() => {
    if (!state.error || !loadedSrc) return;
    const attempts = autoReloads.current.key === loadedKey ? autoReloads.current.count : 0;
    if (attempts >= MAX_AUTO_RELOADS) return;
    autoReloads.current = { key: loadedKey, count: attempts + 1 };
    requestUrl();
  }, [state.error, loadedSrc, loadedKey]);

  const reload = React.useCallback(() => {
    setRefreshWanted(true);
    onSourceError?.();
  }, [onSourceError]);

  const attachRef = React.useCallback(
    (element: HTMLAudioElement | null) => {
      store.attach(element);
      return () => store.attach(null);
    },
    [store],
  );

  // Pause when the page is hidden (Activity keeps navigated-away pages mounted) or unmounted.
  React.useLayoutEffect(() => () => store.pause(), [store]);

  return (
    <AudioPlayerContext value={store}>
      <ReloadContext value={reload}>{children}</ReloadContext>
      <audio ref={attachRef} src={loadedSrc ?? undefined} preload="auto" className="hidden" aria-hidden />
    </AudioPlayerContext>
  );
}
