"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import { libraryHoldsKind } from "@/lib/media-kind";
import {
  MAX_MEDIA_BYTES,
  acceptAttribute,
  fileFitsTypes,
  kindOfTypes,
  mediaNoun,
  megabytes,
  withArticle,
  type MediaTypes,
} from "@/lib/media-pick";
import { TooBigError, libraryFile, libraryTitle, limitLabel, type LibraryPick } from "@/lib/library-file";

/** The label under a choice that came from the Library. */
export const LIBRARY_SOURCE = "From your Library";

/**
 * Where focus goes after a person's own action changes what the screen draws: "replace" after a choice is taken (the
 * control that changes it, never the one that discards it, so one more Enter cannot throw it away) and "choose" after
 * it is removed (the way to choose again). The control that had focus is gone by then, and focus would drop to the page.
 */
export type FocusRequest = "replace" | "choose";

/**
 * Gives focus to `target` once it is on screen, when the pick asked for `want`, and ends the request. It never runs on
 * mount, because only a choice made or removed in this screen asks.
 */
export function useTakeFocus(
  pick: { focusRequest: FocusRequest | null; endFocusRequest: () => void },
  want: FocusRequest,
  target: RefObject<HTMLElement | null>,
) {
  const { focusRequest, endFocusRequest } = pick;
  useEffect(() => {
    if (focusRequest !== want || !target.current) return;
    target.current.focus();
    endFocusRequest();
  });
}

export interface MediaPickOptions<Prepared = undefined> {
  /**
   * What the input takes: full types ("image/png") or families ("image/*"). Omit it for any file; an input that
   * takes any file has no kind and so no Library.
   */
  types?: MediaTypes;
  /** The kind the Library is filtered to. Defaults to the one family `types` share. */
  kind?: string | null;
  /** The words for the thing being chosen. Defaults to the kind's noun ("picture"). */
  noun?: string;
  /** How the wrong-type message names what to choose ("a PNG, JPEG or WebP picture"). Defaults to the noun. */
  accepted?: string;
  /**
   * Largest file taken. A screen that keeps the file as it is has no limit unless it passes one; a screen that reads
   * the file into the request (`prepare`) is held to the shared media limit, since the file travels as text. The
   * Library download always has a limit, this one or the shared one: it reads into memory.
   */
  maxBytes?: number;
  /**
   * Work to do on the file before the screen keeps it (a reference input reads it into a data URI). While it runs
   * the choice is pending and not yet in the form; a throw says the file could not be read. Leave it out when the
   * screen keeps the file as it is.
   */
  prepare?: (file: File) => Promise<Prepared>;
  /**
   * The screen keeps the file (and what `prepare` made of it). Called only if nothing superseded the choice.
   * `from` says what the Library called an item picked from it.
   */
  commit: (file: File, prepared: Prepared, from: { title?: string; seconds?: number | null }) => void;
  /**
   * The file the screen currently holds, so a source label is dropped, and a pick still arriving is ended, when
   * another route (a paste, a recording, a Remove) replaced it. Leave it out when the screen holds a reference.
   */
  current?: File | null;
  /**
   * The reference the screen currently holds (what `prepare` made), with the same effect as `current` for a screen
   * that keeps a reference: `null` for none. Leave it out when the screen holds the file.
   */
  currentReference?: Prepared | null;
  /** Told whenever a read or download starts or ends, for a screen that must hold Run while a choice arrives. */
  onPendingChange?: (pending: boolean) => void;
}

/**
 * One media input's behaviour, shared by every screen that takes a file: choose a file, choose from the Library,
 * and (for the screens that have a drop target or listen for paste) take a file handed over. The screen draws
 * the controls; this holds the rules.
 *
 * One read or download may still change the screen at a time. Anything that supersedes it (another choice, Remove,
 * a reopened Library, leaving the page) aborts its requests, ends its pending state and leaves a late answer
 * unheard. A file that fails the type or size check does not cancel a pending valid choice.
 */
export function useMediaPick<Prepared = undefined>(options: MediaPickOptions<Prepared>) {
  const { types, current, currentReference } = options;
  const kind = options.kind !== undefined ? options.kind : kindOfTypes(types);
  const noun = options.noun ?? mediaNoun(kind);
  const accepted = options.accepted ?? withArticle(noun);
  // The size a file may be when it comes from the disk; none for a file the screen keeps as it is.
  const maxBytes = options.maxBytes ?? (options.prepare ? MAX_MEDIA_BYTES : undefined);
  const libraryAvailable = libraryHoldsKind(kind);

  const fileInput = useRef<HTMLInputElement>(null);
  const active = useRef<{ controller: AbortController } | null>(null);
  const onPendingChange = useRef(options.onPendingChange);
  useEffect(() => {
    onPendingChange.current = options.onPendingChange;
  });
  // Always the latest handlers: a pick that settles after a re-render must call the current screen's.
  const handlers = useRef(options);
  useEffect(() => {
    handlers.current = options;
  });
  useEffect(
    () => () => {
      active.current?.controller.abort();
      active.current = null;
      onPendingChange.current?.(false);
    },
    [],
  );

  const [source, setSource] = useState<{
    label: string;
    file: File | null;
    /** What the screen committed with the file; a source is only the screen's choice while this is what it holds. */
    committed: unknown;
    /** The Library item it came from, so a reopened Library can mark it. */
    assetId?: string;
    title?: string;
    seconds?: number | null;
  } | null>(null);
  // Where focus goes once the screen has drawn the new state: a choice taken (Replace) or removed (the way to choose).
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [libraryOpen, setLibraryOpen] = useState(false);
  // A read or download is under way: the choice is not in the form yet.
  const [pending, setPending] = useState(false);
  const [blockedSubmit, setBlockedSubmit] = useState(false);
  // The one place `pending` changes, so the screen is told in the same step. A screen that holds Run while a
  // choice arrives (Home's Make) needs the pending state of an input that sits outside any form.
  function setPendingState(next: boolean) {
    setPending(next);
    onPendingChange.current?.(next);
  }

  // Run must not go out while the choice is still arriving: it would send the form without it. The page owns Run,
  // so the pending input stops the form.
  useEffect(() => {
    const form = pending ? fileInput.current?.closest("form") : null;
    if (!form) return;
    const stop = (event: Event) => {
      event.preventDefault();
      event.stopPropagation();
      setBlockedSubmit(true);
    };
    form.addEventListener("submit", stop, true);
    return () => form.removeEventListener("submit", stop, true);
  }, [pending]);

  function supersede() {
    active.current?.controller.abort();
    const next = { controller: new AbortController() };
    active.current = next;
    setPendingState(false);
    setFocusRequest(null);
    return next;
  }

  // What the screen holds changed. If it is not what this hook committed (a paste, a recording, a Remove, a typed
  // link) while a choice is still arriving, that choice is stale: end it, so it cannot land over the newer one.
  const heldByScreen = currentReference !== undefined ? currentReference : current;
  const committed = useRef<unknown>(undefined);
  const lastHeld = useRef(heldByScreen);
  useEffect(() => {
    if (lastHeld.current === heldByScreen) return;
    lastHeld.current = heldByScreen;
    if (pending && heldByScreen !== committed.current) supersede();
  });

  function startPending() {
    setPendingState(true);
    setBlockedSubmit(false);
  }

  /**
   * Gives the file to the screen, unless something superseded this choice first (then it does nothing and returns
   * false). A screen that keeps the file at once never shows a pending state.
   */
  async function handOver(
    file: File,
    operation: { controller: AbortController },
    from: { title?: string; seconds?: number | null },
  ): Promise<{ committed: unknown } | null> {
    const { prepare, commit } = handlers.current;
    let prepared: Prepared = undefined as Prepared;
    if (prepare) {
      startPending();
      prepared = await prepare(file);
      if (active.current !== operation) return null;
    }
    // Recorded before the screen changes, so its own commit is not read as another route's change.
    const kept = currentReference !== undefined ? prepared : file;
    committed.current = kept;
    commit(file, prepared, from);
    return { committed: kept };
  }

  /**
   * Takes a file from the file dialog, a drop or a paste: checks it, then hands it to the screen. Focus follows the
   * choice to Replace, unless the person was elsewhere (a paste into the words box passes `moveFocus: false`).
   */
  async function take(file: File | undefined | null, { moveFocus = true }: { moveFocus?: boolean } = {}) {
    if (!file) return;
    setError(null);
    if (!fileFitsTypes(file, types)) {
      setError(`That file is not ${accepted}. Choose ${accepted} file.`);
      return;
    }
    if (maxBytes !== undefined && file.size > maxBytes) {
      setError(`That ${noun} is ${megabytes(file.size)}. Choose one under ${limitLabel(maxBytes)}.`);
      return;
    }
    const operation = supersede();
    try {
      const handed = await handOver(file, operation, {});
      if (!handed) return;
      setSource({ label: file.name, file, committed: handed.committed });
      setPendingState(false);
      if (moveFocus) setFocusRequest("replace");
    } catch {
      if (active.current !== operation) return;
      setPendingState(false);
      setError(`Could not read that ${noun}. Try another file.`);
    }
  }

  async function pickFromLibrary(pick: LibraryPick) {
    if (!kind) return;
    const operation = supersede();
    setError(null);
    setLibraryOpen(false);
    // The request carries the bytes, as it does for "Choose a file". A bare asset id would only work for the app
    // that made the item, and the Library lists everything the person made with any of their apps.
    startPending();
    try {
      const picked = await libraryFile(pick, {
        kind,
        noun,
        types,
        maxBytes: maxBytes ?? MAX_MEDIA_BYTES,
        signal: operation.controller.signal,
      });
      if (active.current !== operation) return;
      const title = libraryTitle(pick, noun);
      const handed = await handOver(picked, operation, { title, seconds: pick.duration_s });
      if (!handed) return;
      setSource({
        label: LIBRARY_SOURCE,
        file: picked,
        committed: handed.committed,
        assetId: pick.asset_id,
        title,
        seconds: pick.duration_s,
      });
      setPendingState(false);
      setFocusRequest("replace");
    } catch (failure) {
      if (active.current !== operation) return;
      setPendingState(false);
      setError(
        failure instanceof TooBigError
          ? failure.message
          : `Could not add that ${noun} from your Library. Try another, or choose a file.`,
      );
    }
  }

  const watching = currentReference !== undefined || current !== undefined;
  const held = source && (!watching || source.committed === heldByScreen) ? source : null;

  return {
    kind,
    noun,
    /** The size limit for a file from the disk, or undefined when there is none. */
    maxBytes,
    /** The `accept` attribute for the file input. */
    accept: acceptAttribute(types),
    types,
    fileInput,
    /** True when the Library can hold this input's kind: show "Choose from Library". */
    libraryAvailable,
    libraryOpen,
    pending,
    blockedSubmit,
    error,
    /** "From your Library" or the chosen file's name, while it is still the file the screen holds. */
    source: held ? held.label : null,
    /** The file the source label is about; in reference mode the media itself is a data URI, and this names it. */
    sourceFile: held ? held.file : null,
    /** What the Library calls the item picked from it; the file's own name is only what is sent. */
    sourceTitle: held ? (held.title ?? null) : null,
    /** The Library item the choice came from, while it is still what the screen holds. */
    sourceAssetId: held ? (held.assetId ?? null) : null,
    /** The length of the item picked from the Library, in seconds, when the Library knew it. */
    sourceSeconds: held?.seconds ?? null,
    take,
    pickFromLibrary,
    openFileDialog: () => fileInput.current?.click(),
    openLibrary: () => {
      supersede();
      setLibraryOpen(true);
    },
    setLibraryOpen,
    /** Where focus is wanted once the screen has drawn the new state; see `useTakeFocus`. */
    focusRequest,
    endFocusRequest: () => setFocusRequest(null),
    /**
     * Takes another in one step, the way the first was chosen: the Library again for an item from it (or one whose
     * source is not known), the file dialog for a file from disk or an input with no Library.
     */
    replaceChosen: () => {
      if (libraryAvailable && (!held || held.label === LIBRARY_SOURCE)) {
        supersede();
        setLibraryOpen(true);
      } else {
        fileInput.current?.click();
      }
    },
    /** Ends a pending read or download and forgets the source: Remove, or an edit that replaces the choice. */
    clear: () => {
      supersede();
      setSource(null);
      setError(null);
      setFocusRequest("choose");
    },
    /** Ends a pending read or download without touching the choice (a typed link replaces it). */
    replace: () => {
      supersede();
      setSource(null);
    },
    /** `onChange` for the hidden file input. */
    onFileInputChange: (event: { currentTarget: HTMLInputElement }) => {
      const input = event.currentTarget;
      void take(input.files?.[0]).finally(() => {
        input.value = "";
      });
    },
  };
}
