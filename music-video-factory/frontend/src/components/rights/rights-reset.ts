"use client";

import * as React from "react";

import type { ProjectOut, RightsKey } from "@/lib/types";

/**
 * Rights checklist items the backend un-confirmed because the content they vouched for changed: a new
 * song resets `audio_rights` (`POST /audio` → `ProjectOut.warnings`), a change of the lyric text resets
 * `lyrics_rights` (`PUT /lyrics`, `POST /lyrics/upload`, `PUT /lyrics/lines` → `warnings`). The API says
 * so only in the response of that change, so the browser remembers it per project (localStorage, best
 * effort) until the item is confirmed again. It drives the 「要再確認」 badges and the inline notices.
 */
export interface RightsReset {
  key: RightsKey;
  /** The backend's explanation (Japanese). */
  message: string;
  /** ISO time the reset was seen. */
  at: string;
}

type Store = Record<string, Partial<Record<RightsKey, Omit<RightsReset, "key">>>>;

const STORAGE_KEY = "mvf.rightsReset";
const EMPTY: Store = {};
const NO_RESETS: RightsReset[] = [];

/** Checklist items whose confirmation a content change can withdraw, with their short names. */
export const RESETTABLE_RIGHTS: Partial<Record<RightsKey, string>> = {
  audio_rights: "音源の権利",
  lyrics_rights: "歌詞の権利",
};

let memory: Store | null = null;
const listeners = new Set<() => void>();

function parse(raw: string | null): Store {
  if (!raw) return EMPTY;
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value) ? (value as Store) : EMPTY;
  } catch {
    return EMPTY;
  }
}

function load(): Store {
  if (memory === null) {
    try {
      memory = parse(window.localStorage.getItem(STORAGE_KEY));
    } catch {
      memory = EMPTY; // storage blocked: remembered for this page only
    }
  }
  return memory;
}

function save(next: Store): void {
  memory = next;
  try {
    if (Object.keys(next).length > 0) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Not persisted (private mode / blocked storage); the in-memory copy still works.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key !== null && event.key !== STORAGE_KEY) return;
    memory = parse(event.newValue);
    listener();
  };
  listeners.add(listener);
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

function update(projectId: string, change: (entries: NonNullable<Store[string]>) => NonNullable<Store[string]>): void {
  const store = load();
  const entries = change({ ...(store[projectId] ?? {}) });
  const next = { ...store };
  if (Object.keys(entries).length > 0) next[projectId] = entries;
  else delete next[projectId];
  save(next);
}

/**
 * Remember the items a change un-confirmed: those confirmed in `before` and no longer in `after`.
 * The warning naming an item becomes its message. Returns the items and the warnings used as messages
 * (callers list the remaining warnings, e.g. lyric import notes, separately).
 */
export function recordRightsResets(
  projectId: string,
  before: ProjectOut["rights"] | null | undefined,
  after: ProjectOut["rights"] | null | undefined,
  warnings: readonly string[],
): { keys: RightsKey[]; messages: string[] } {
  const used: string[] = [];
  const found: RightsReset[] = [];
  for (const [key, name] of Object.entries(RESETTABLE_RIGHTS) as [RightsKey, string][]) {
    if (before?.[key] !== true || !after || after[key] === true) continue;
    const message = warnings.find((warning) => warning.includes(`「${name}」`));
    if (message) used.push(message);
    found.push({
      key,
      message: message ?? `内容が変わったため、権利確認の「${name}」が未確認に戻りました。確認し直すまで、生成と再レンダリングはできません。`,
      at: new Date().toISOString(),
    });
  }
  if (found.length > 0) {
    update(projectId, (entries) => {
      for (const reset of found) entries[reset.key] = { message: reset.message, at: reset.at };
      return entries;
    });
  }
  return { keys: found.map((reset) => reset.key), messages: used };
}

/** Forget resets (the items were confirmed again). */
export function clearRightsResets(projectId: string, keys: readonly RightsKey[]): void {
  const entries = load()[projectId];
  if (!entries || !keys.some((key) => key in entries)) return;
  update(projectId, (current) => {
    for (const key of keys) delete current[key];
    return current;
  });
}

/**
 * The project's remembered resets whose item is still unconfirmed (`project.rights`). Entries for items
 * confirmed in the meantime (here, in another tab or through the API) are dropped.
 */
export function useRightsResets(project: Pick<ProjectOut, "id" | "rights"> | null | undefined): RightsReset[] {
  const store = React.useSyncExternalStore(subscribe, load, () => EMPTY);
  const entries = project ? store[project.id] : undefined;
  const rights = project?.rights;
  const resets = React.useMemo(() => {
    if (!entries || !rights) return NO_RESETS;
    return (Object.entries(entries) as [RightsKey, Omit<RightsReset, "key">][])
      .filter(([key]) => rights[key] !== true)
      .map(([key, value]) => ({ key, ...value }));
  }, [entries, rights]);

  const projectId = project?.id;
  const confirmedAgain = React.useMemo(
    () => (entries && rights ? (Object.keys(entries) as RightsKey[]).filter((key) => rights[key] === true) : []),
    [entries, rights],
  );
  React.useEffect(() => {
    if (projectId && confirmedAgain.length > 0) clearRightsResets(projectId, confirmedAgain);
  }, [projectId, confirmedAgain]);
  return resets;
}

/** Short name of a resettable item (「音源の権利」), else the key. */
export function rightsResetName(key: RightsKey): string {
  return RESETTABLE_RIGHTS[key] ?? key;
}
