// @vitest-environment jsdom
import { runInThisContext } from "node:vm";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EARLY_PATHS, EARLY_SCRIPT, fetchEarly, takeEarly } from "@/lib/early-fetch";

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

/** Run the head script in the page's global scope, as a browser runs an inline script. */
function runScript() {
  runInThisContext(EARLY_SCRIPT);
}

function runHeadScript(pathname: string) {
  const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(async () => ok({ early: true }));
  vi.stubGlobal("fetch", fetchMock);
  window.history.pushState({}, "", pathname);
  runScript();
  return fetchMock;
}

beforeEach(() => {
  delete window.__azEarly;
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.history.pushState({}, "", "/");
});

describe("the head script", () => {
  it("starts the viewer, Home and the recent creations at once on Home", () => {
    const fetchMock = runHeadScript("/");
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([...EARLY_PATHS.everywhere, ...EARLY_PATHS.home]);
    expect(Object.keys(window.__azEarly!)).toHaveLength(3);
  });
  it("starts only the viewer on any other page", () => {
    const fetchMock = runHeadScript("/library/");
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual(["/me/api/session"]);
  });
  it("never throws, whatever the browser lacks", () => {
    vi.stubGlobal("fetch", undefined);
    expect(() => runScript()).not.toThrow();
  });
});

describe("taking an early answer", () => {
  it("serves it once, then asks the network", async () => {
    runHeadScript("/");
    const network = vi.fn(async () => ok({ early: false }));
    vi.stubGlobal("fetch", network);
    expect(await (await fetchEarly("/me/api/home", { credentials: "same-origin" })).json()).toEqual({ early: true });
    expect(await (await fetchEarly("/me/api/home", { credentials: "same-origin" })).json()).toEqual({ early: false });
    expect(network).toHaveBeenCalledTimes(1);
  });
  it("ignores an answer older than 30 s", async () => {
    vi.useFakeTimers();
    runHeadScript("/");
    vi.advanceTimersByTime(31_000);
    expect(takeEarly("/me/api/home")).toBeNull();
  });
  it("falls back to a normal request when the early one failed", async () => {
    window.__azEarly = { "/me/api/home": { at: Date.now(), promise: Promise.reject(new TypeError("Failed to fetch")) } };
    const network = vi.fn(async () => ok({ early: false }));
    vi.stubGlobal("fetch", network);
    expect(await (await fetchEarly("/me/api/home")).json()).toEqual({ early: false });
  });
  it("never serves a write", () => {
    runHeadScript("/");
    expect(takeEarly("/me/api/home", "POST")).toBeNull();
  });
});
