/*
 * What a person typed on Home, carried to Try in sessionStorage. It never goes in the URL, so
 * the words stay out of history, sync and access logs. Try takes it once, on the page it opens.
 */
const KEY = "azizam.home.handoff";
/** A hand-off older than this was not taken by the page it was meant for; it is dropped. */
const MAX_AGE_MS = 5 * 60_000;

export interface HomeHandoff {
  /** The kind of work (the same word the Try URL carries). */
  capability: string;
  model: string | null;
  prompt: string;
}

export function storeHomeHandoff(handoff: HomeHandoff): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ ...handoff, at: Date.now() }));
  } catch {
    // Storage can be refused (private windows). Try then opens empty and the person types again.
  }
}

/** The waiting hand-off for this kind of work, removed from storage as it is read. */
export function takeHomeHandoff(capability: string | null): HomeHandoff | null {
  let raw: string | null = null;
  try {
    raw = sessionStorage.getItem(KEY);
    sessionStorage.removeItem(KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as HomeHandoff & { at: number };
    if (Date.now() - value.at > MAX_AGE_MS || value.capability !== capability) return null;
    return { capability: value.capability, model: value.model ?? null, prompt: String(value.prompt ?? "") };
  } catch {
    return null;
  }
}

/*
 * What this tab last made from Home, so a make still running can show its words on its tile: a receipt
 * carries no prompt. It stays in sessionStorage, as the hand-off does, so the words leave with the tab.
 */
const MADE_KEY = "azizam.home.made";
const MADE_KEPT = 5;

export interface HomeMade {
  capability: string;
  prompt: string;
  /** When Make was pressed, in epoch seconds. */
  at: number;
}

export function rememberMade(made: Omit<HomeMade, "at">): void {
  try {
    const kept = readMade().slice(0, MADE_KEPT - 1);
    sessionStorage.setItem(MADE_KEY, JSON.stringify([{ ...made, at: Date.now() / 1000 }, ...kept]));
  } catch {
    // Storage can be refused; the running tile then shows its kind only.
  }
}

/** What this tab made from Home, newest first. */
export function readMade(): HomeMade[] {
  try {
    const value = JSON.parse(sessionStorage.getItem(MADE_KEY) ?? "[]");
    if (!Array.isArray(value)) return [];
    return value.filter(
      (item): item is HomeMade =>
        typeof item?.capability === "string" && typeof item?.prompt === "string" && typeof item?.at === "number",
    );
  } catch {
    return [];
  }
}
