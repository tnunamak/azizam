"use client";

import { useEffect, useRef, useState } from "react";
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
import { TooBigError, libraryFile, limitLabel, type LibraryPick } from "@/lib/library-file";

/** The label under a choice that came from the Library. */
export const LIBRARY_SOURCE = "From your Library";

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
  /** Largest file taken. Defaults to the shared media limit. */
  maxBytes?: number;
  /**
   * Work to do on the file before the screen keeps it (a reference input reads it into a data URI). While it runs
   * the choice is pending and not yet in the form; a throw says the file could not be read. Leave it out when the
   * screen keeps the file as it is.
   */
  prepare?: (file: File) => Promise<Prepared>;
  /** The screen keeps the file (and what `prepare` made of it). Called only if nothing superseded the choice. */
  commit: (file: File, prepared: Prepared) => void;
  /**
   * The file the screen currently holds, so a source label is dropped when another route (a paste, a recording)
   * replaced it. Leave it out when the screen holds a reference rather than the file.
   */
  current?: File | null;
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
  const { types, current } = options;
  const kind = options.kind !== undefined ? options.kind : kindOfTypes(types);
  const noun = options.noun ?? mediaNoun(kind);
  const accepted = options.accepted ?? withArticle(noun);
  const maxBytes = options.maxBytes ?? MAX_MEDIA_BYTES;
  const libraryAvailable = libraryHoldsKind(kind);

  const fileInput = useRef<HTMLInputElement>(null);
  const active = useRef<{ controller: AbortController } | null>(null);
  // Always the latest handlers: a pick that settles after a re-render must call the current screen's.
  const handlers = useRef(options);
  useEffect(() => {
    handlers.current = options;
  });
  useEffect(
    () => () => {
      active.current?.controller.abort();
      active.current = null;
    },
    [],
  );

  const [source, setSource] = useState<{ label: string; file: File | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [libraryOpen, setLibraryOpen] = useState(false);
  // A read or download is under way: the choice is not in the form yet.
  const [pending, setPending] = useState(false);
  const [blockedSubmit, setBlockedSubmit] = useState(false);
  const onPendingChange = useRef(options.onPendingChange);
  useEffect(() => {
    onPendingChange.current = options.onPendingChange;
  });
  useEffect(() => {
    onPendingChange.current?.(pending);
  }, [pending]);

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
    setPending(false);
    return next;
  }

  function startPending() {
    setPending(true);
    setBlockedSubmit(false);
  }

  /**
   * Gives the file to the screen, unless something superseded this choice first (then it does nothing and returns
   * false). A screen that keeps the file at once never shows a pending state.
   */
  async function handOver(file: File, operation: { controller: AbortController }): Promise<boolean> {
    const { prepare, commit } = handlers.current;
    let prepared: Prepared = undefined as Prepared;
    if (prepare) {
      startPending();
      prepared = await prepare(file);
      if (active.current !== operation) return false;
    }
    commit(file, prepared);
    return true;
  }

  /** Takes a file from the file dialog, a drop or a paste: checks it, then hands it to the screen. */
  async function take(file: File | undefined | null) {
    if (!file) return;
    setError(null);
    if (!fileFitsTypes(file, types)) {
      setError(`That file is not ${accepted}. Choose ${accepted} file.`);
      return;
    }
    if (file.size > maxBytes) {
      setError(`That ${noun} is ${megabytes(file.size)}. Choose one under ${limitLabel(maxBytes)}.`);
      return;
    }
    const operation = supersede();
    try {
      if (!(await handOver(file, operation))) return;
      setSource({ label: file.name, file });
      setPending(false);
    } catch {
      if (active.current !== operation) return;
      setPending(false);
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
        maxBytes,
        signal: operation.controller.signal,
      });
      if (active.current !== operation) return;
      if (!(await handOver(picked, operation))) return;
      setSource({ label: LIBRARY_SOURCE, file: picked });
      setPending(false);
    } catch (failure) {
      if (active.current !== operation) return;
      setPending(false);
      setError(
        failure instanceof TooBigError
          ? failure.message
          : `Could not add that ${noun} from your Library. Try another, or choose a file.`,
      );
    }
  }

  return {
    kind,
    noun,
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
    source: source && (current === undefined || source.file === current) ? source.label : null,
    take,
    pickFromLibrary,
    openFileDialog: () => fileInput.current?.click(),
    openLibrary: () => {
      supersede();
      setLibraryOpen(true);
    },
    setLibraryOpen,
    /** Ends a pending read or download and forgets the source: Remove, or an edit that replaces the choice. */
    clear: () => {
      supersede();
      setSource(null);
      setError(null);
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
