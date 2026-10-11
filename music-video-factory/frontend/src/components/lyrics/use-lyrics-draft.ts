"use client";

import * as React from "react";
import { useSWRConfig } from "swr";

import { recordRightsResets } from "@/components/rights/rights-reset";
import { apiPaths, updateLyricLines } from "@/lib/api";
import type { LyricLine, LyricsOut, ProjectOut } from "@/lib/types";

import { fillMissingEnds, linesEqual } from "./timing";

const HISTORY_LIMIT = 100;

interface DraftState {
  /** Server lines last adopted (identity, to detect revalidation). */
  server: LyricLine[] | undefined;
  /** Saved lines the draft is compared against. */
  baseline: LyricLine[];
  draft: LyricLine[];
  past: LyricLine[][];
  future: LyricLine[][];
}

type DraftAction =
  | { type: "sync"; lines: LyricLine[] | undefined }
  | { type: "edit"; lines: LyricLine[] }
  | { type: "replace"; lines: LyricLine[]; undoable: boolean }
  | { type: "reset"; lines: LyricLine[] }
  | { type: "undo" }
  | { type: "redo" }
  | { type: "discard" };

function reducer(state: DraftState, action: DraftAction): DraftState {
  switch (action.type) {
    case "sync": {
      const lines = action.lines ?? [];
      const dirty = !linesEqual(state.draft, state.baseline);
      // Keep unsaved edits; otherwise follow the server.
      if (dirty) return { ...state, server: action.lines, baseline: lines };
      // Our own save echoed back through the SWR cache: keep the undo history.
      if (linesEqual(lines, state.draft)) return { ...state, server: action.lines, baseline: lines, draft: lines };
      return { server: action.lines, baseline: lines, draft: lines, past: [], future: [] };
    }
    case "edit": {
      if (linesEqual(action.lines, state.draft)) return state;
      return {
        ...state,
        draft: action.lines,
        past: [...state.past, state.draft].slice(-HISTORY_LIMIT),
        future: [],
      };
    }
    case "replace":
      // New saved state (after save / auto-timing): becomes the baseline and the draft.
      // Auto-timing is undoable (undo + save restores the previous timing).
      return {
        ...state,
        baseline: action.lines,
        draft: action.lines,
        past: action.undoable ? [...state.past, state.draft].slice(-HISTORY_LIMIT) : state.past,
        future: action.undoable ? [] : state.future,
      };
    case "reset":
      // A new line list (re-parsed lyrics): history refers to other line ids, so it is cleared.
      return { ...state, baseline: action.lines, draft: action.lines, past: [], future: [] };
    case "undo": {
      const previous = state.past[state.past.length - 1];
      if (!previous) return state;
      return { ...state, draft: previous, past: state.past.slice(0, -1), future: [state.draft, ...state.future] };
    }
    case "redo": {
      const [next, ...rest] = state.future;
      if (!next) return state;
      return { ...state, draft: next, past: [...state.past, state.draft], future: rest };
    }
    case "discard":
      return { ...state, draft: state.baseline, past: [...state.past, state.draft].slice(-HISTORY_LIMIT), future: [] };
  }
}

export interface LyricsDraft {
  lines: LyricLine[];
  dirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  saving: boolean;
  edit: (lines: LyricLine[]) => void;
  undo: () => void;
  redo: () => void;
  discard: () => void;
  /** Persist the draft (`PUT /lyrics/lines`); empty ends are filled with their derived value. */
  save: () => Promise<LyricLine[]>;
  /** Replace everything with freshly parsed (already saved) lines; clears undo history. */
  adopt: (lines: LyricLine[]) => void;
  /** Persist a server-produced set of lines (e.g. auto-timing) as an undoable step. */
  commit: (lines: LyricLine[]) => Promise<LyricLine[]>;
}

/**
 * Local editing state for the lyric timing table, kept in sync with `GET /lyrics`. `rights` is the
 * project's checklist: saving changed line texts un-confirms `lyrics_rights` (remembered for the notices).
 */
export function useLyricsDraft(
  projectId: string,
  serverLines: LyricLine[] | undefined,
  songDuration: number | null,
  rights: ProjectOut["rights"] | undefined,
): LyricsDraft {
  const { mutate } = useSWRConfig();
  const [state, dispatch] = React.useReducer(reducer, serverLines, (lines) => ({
    server: lines,
    baseline: lines ?? [],
    draft: lines ?? [],
    past: [],
    future: [],
  }));
  const [saving, setSaving] = React.useState(false);

  if (serverLines !== state.server) dispatch({ type: "sync", lines: serverLines });

  const persist = React.useCallback(
    async (lines: LyricLine[], undoable: boolean) => {
      setSaving(true);
      try {
        const result = await updateLyricLines(projectId, fillMissingEnds(lines, songDuration));
        dispatch({ type: "replace", lines: result.lines, undoable });
        await mutate<LyricsOut>(apiPaths.lyrics(projectId), (current) => (current ? { ...current, lines: result.lines } : current), {
          revalidate: false,
        });
        if (result.warnings?.length) {
          const project = await mutate<ProjectOut>(apiPaths.project(projectId)).catch(() => undefined);
          recordRightsResets(projectId, rights, project?.rights, result.warnings);
        } else {
          void mutate(apiPaths.project(projectId));
        }
        return result.lines;
      } finally {
        setSaving(false);
      }
    },
    [mutate, projectId, songDuration, rights],
  );

  const draft = state.draft;
  return {
    lines: draft,
    dirty: !linesEqual(draft, state.baseline),
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
    saving,
    edit: React.useCallback((lines: LyricLine[]) => dispatch({ type: "edit", lines }), []),
    undo: React.useCallback(() => dispatch({ type: "undo" }), []),
    redo: React.useCallback(() => dispatch({ type: "redo" }), []),
    discard: React.useCallback(() => dispatch({ type: "discard" }), []),
    adopt: React.useCallback((lines: LyricLine[]) => dispatch({ type: "reset", lines }), []),
    save: React.useCallback(() => persist(draft, false), [persist, draft]),
    commit: React.useCallback((lines: LyricLine[]) => persist(lines, true), [persist]),
  };
}
