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
