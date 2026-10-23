/**
 * Work on this page that Switch person or Sign out would lose (account switcher W2; DESIGN.md 4.2).
 * A component registers what it holds while that work is mounted and active. The switch confirm reads
 * the list when a person asks to leave. It is module state, not context: the account menu reads it from
 * its own tree, and each browser tab runs one page.
 */
import { useCallback, useEffect, useRef } from "react";

export type UnsavedKind =
  | "new-key"
  | "try-draft"
  | "try-waiting"
  | "try-picture"
  | "try-audio"
  | "try-video"
  | "home-key"
  | "form";

export interface UnsavedWork {
  kind: UnsavedKind;
  /** The title of a changed form. */
  label?: string;
  /** How many of this kind the page holds, such as the Try tabs with unsent words. */
  count: number;
}

export type UnsavedWorkRequest = {
  count?: number;
  /** Registers only while true. */
  active?: boolean;
} & ({ kind: "form"; label: string } | { kind: Exclude<UnsavedKind, "form">; label?: undefined });

/** The history entry a piece of unsaved work is shown on. A Back press that would leave it is undone to this entry. */
export interface HistoryEntry {
  state: unknown;
  url: string;
}

let registrations: Array<{ id: number; work: UnsavedWork; entry: HistoryEntry }> = [];
let nextId = 0;

function register(work: UnsavedWork): () => void {
  const id = ++nextId;
  const entry = { state: window.history.state, url: window.location.href };
  registrations = [...registrations, { id, work, entry }];
  return () => {
    registrations = registrations.filter((item) => item.id !== id);
  };
}

/** What is unsaved right now, in the order it was registered. Read it when a person asks to leave. */
export function unsavedWorkNow(): readonly UnsavedWork[] {
  return registrations.map((item) => item.work);
}

/** The entry of the newest unsaved secret, or null when no secret is on screen unsaved. */
export function unsavedSecretEntry(): HistoryEntry | null {
  const secrets = registrations.filter((item) => item.work.kind === "new-key");
  return secrets.length ? secrets[secrets.length - 1].entry : null;
}

/** Registers the work while the calling component is mounted and `active`. */
export function useUnsavedWork({ kind, label, count = 1, active = true }: UnsavedWorkRequest): void {
  useEffect(() => {
    if (!active) return;
    return register({ kind, label, count });
  }, [active, kind, label, count]);
}

/**
 * A new secret on screen that its person has not confirmed saved (the key the gateway shows once). It is unsaved work
 * like any other, and it also stops in-app navigation and closing the tab (components/shell/leave-guard.tsx). The
 * returned `release` ends the hold at once. Call it in the click that confirms the save: an effect would run after
 * that click's navigation has started, and the browser's closing prompt would ask once more.
 */
export function useUnsavedSecret(active: boolean): () => void {
  const drop = useRef<(() => void) | null>(null);
  const release = useCallback(() => {
    drop.current?.();
    drop.current = null;
  }, []);
  useEffect(() => {
    if (!active) return;
    drop.current = register({ kind: "new-key", count: 1 });
    return release;
  }, [active, release]);
  return release;
}

/** The copy for each kind, as DESIGN.md 4.2 words it. Each takes the count of its kind. */
const LINE: Record<Exclude<UnsavedKind, "form">, (count: number) => string> = {
  "new-key": () => "The new key you haven’t saved yet",
  "try-draft": (count) =>
    count === 1 ? "Your unsent message in Try" : `Your unsent messages in ${count} Try tabs`,
  "try-waiting": (count) =>
    count === 1 ? "The answer Try is waiting for" : `The answers Try is waiting for in ${count} Try tabs`,
  "try-picture": (count) =>
    count === 1 ? "The picture being made in Try" : `The pictures being made in ${count} Try tabs`,
  "try-audio": (count) =>
    count === 1 ? "The audio being made in Try" : `The audio being made in ${count} Try tabs`,
  "try-video": (count) =>
    count === 1 ? "The video being made in Try" : `The videos being made in ${count} Try tabs`,
  "home-key": () => "The key you pasted on Home",
};

/** The order of the kinds in the confirm. Forms come after them, one line per changed form. */
const KIND_ORDER = ["new-key", "try-draft", "try-waiting", "try-picture", "try-audio", "try-video", "home-key"] as const;

/** The lines the switch confirm lists: one per kind, with a count when there is more than one. */
export function unsavedWorkLines(work: readonly UnsavedWork[]): string[] {
  const lines: string[] = [];
  for (const kind of KIND_ORDER) {
    const count = work.reduce((sum, item) => (item.kind === kind ? sum + item.count : sum), 0);
    if (count > 0) lines.push(LINE[kind](count));
  }
  const forms = [...new Set(work.filter((item) => item.kind === "form").map((item) => item.label))];
  for (const label of forms) lines.push(`Changes in ${label}`);
  return lines;
}
