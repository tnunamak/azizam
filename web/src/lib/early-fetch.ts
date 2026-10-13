/**
 * Requests the page starts before its JavaScript has loaded.
 *
 * The first screen needs the viewer, Home and the recent creations. Those requests used to start
 * only after about 1 MB of JavaScript had been downloaded, parsed and run, then one after the
 * other (viewer, then Home), so on a phone the data arrived about 1.4 s after the code.
 * `EARLY_SCRIPT` runs from the document head, fires them at once and keeps the promises; the
 * first fetch of the same URL takes the answer instead of asking again. An answer older than
 * `MAX_AGE_MS`, or a failed one, is ignored and the page fetches as it always did.
 */
export const EARLY_PATHS = {
  everywhere: ["/me/api/session"],
  home: ["/me/api/home", "/me/api/media?limit=12"],
} as const;

const MAX_AGE_MS = 30_000;

interface EarlyEntry {
  at: number;
  promise: Promise<Response>;
}
declare global {
  interface Window {
    __azEarly?: Record<string, EarlyEntry>;
  }
}

/** The inline script for the document head. Keep it plain ES5: it runs before anything else. */
export const EARLY_SCRIPT = `(function(){try{var e=window.__azEarly={},p=location.pathname,t=Date.now();function g(u,i){e[u]={at:t,promise:fetch(u,i)};}
g(${JSON.stringify(EARLY_PATHS.everywhere[0])},{credentials:"same-origin",cache:"no-store"});
if(p==="/"||p==="/index.html"){${EARLY_PATHS.home.map((u) => `g(${JSON.stringify(u)},{credentials:"same-origin"});`).join("")}}
}catch(x){}})();`;

/** Take the early answer for `url` if there is a fresh one; otherwise null. Each answer is taken once. */
export function takeEarly(url: string, method = "GET"): Promise<Response> | null {
  if (typeof window === "undefined" || method !== "GET") return null;
  const entry = window.__azEarly?.[url];
  if (!entry) return null;
  delete window.__azEarly![url];
  return Date.now() - entry.at > MAX_AGE_MS ? null : entry.promise;
}

/** `fetch`, except that a request the head script already started is awaited instead of repeated. */
export async function fetchEarly(url: string, init?: RequestInit): Promise<Response> {
  const early = takeEarly(url, init?.method ?? "GET");
  if (early) {
    try {
      return await early;
    } catch {
      /* fall through to a normal request */
    }
  }
  return fetch(url, init);
}
