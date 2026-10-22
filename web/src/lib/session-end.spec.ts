import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as sessionEnd from "@/lib/session-end";
import { requestReauth } from "@/lib/reauth";
import { networkFetch } from "@/lib/network-error";
import { navigate } from "@/lib/navigate";

// The page's navigation is spied on the shared object, not replaced by vi.mock: the setup file loads
// the session modules before a spec's mocks apply, so a module mock would not reach them.
let replace: ReturnType<typeof vi.spyOn>;

type Handler = (init: RequestInit | undefined) => Response | Promise<Response>;
let routes: Map<string, Handler>;
const sent = (key: string) => vi.mocked(fetch).mock.calls.filter(([url, init]) => `${init?.method ?? "GET"} ${String(url)}` === key);

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
/** Answers only when the request is aborted: a gateway that never replies. */
const hangs: Handler = (init) =>
  new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
const offline: Handler = () => {
  throw new TypeError("Failed to fetch");
};
function route(key: string, handler: Handler) {
  routes.set(key, handler);
}
const signedIn = { authenticated: true, csrf_token: "csrf-1", session_view_id: "view-a" };

beforeEach(() => {
  replace = vi.spyOn(navigate, "replace").mockImplementation(() => {});
  routes = new Map();
  route("GET /me/api/session", () => json(signedIn));
  route("POST /logout", () => json({ next: "/sign-in/" }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${String(input)}`;
      const handler = routes.get(key);
      if (!handler) throw new Error(`no route for ${key}`);
      return handler(init);
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  sessionEnd.resetSessionStateForTests();
});

describe("ending the session", () => {
  it("endSession wipes every key not on the allowlist", async () => {
    localStorage.setItem("theme", "dark");
    localStorage.setItem("azizam:unfinished-key-rotations", '{"app-1":["old-key"]}');
    localStorage.setItem("azizam.try.key", "stale");
    localStorage.setItem("some-other-key", "x");
    sessionStorage.setItem("azizam.try.forget", "1");
    sessionStorage.setItem("azizam.handoff.last", "{}");
    sessionStorage.setItem("gateway-invitation", "token");

    await sessionEnd.endSession("sign-out");

    expect(Object.keys(localStorage).sort()).toEqual(["azizam.handoff", "theme"]);
    expect(localStorage.getItem("theme")).toBe("dark");
    expect(Object.keys(sessionStorage)).toEqual(["azizam.handoff.last"]);
    expect(replace).toHaveBeenCalledWith("/sign-in/");
    expect(JSON.parse(localStorage.getItem("azizam.handoff")!)).toMatchObject({ state: "SIGNED_OUT", kind: "sign-out" });
  });

  it("the sign-out carries the CSRF token and headers the gateway expects", async () => {
    await sessionEnd.endSession("sign-out");
    const [[, init]] = sent("POST /logout");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      headers: { "x-azizam-csrf": "csrf-1", accept: "application/json" },
    });
  });

  it("a 403 gets one retry with a fresh token, then the sign-out completes", async () => {
    const tokens = ["stale", "fresh"];
    route("GET /me/api/session", () => json({ ...signedIn, csrf_token: tokens.shift() }));
    let posts = 0;
    route("POST /logout", () => {
      posts += 1;
      return posts === 1 ? json({ error: { code: "csrf_required", message: "Refresh" } }, 403) : json({ next: "/sign-in/" });
    });
    await sessionEnd.endSession("sign-out");
    expect(posts).toBe(2);
    expect(sent("POST /logout").map(([, init]) => new Headers(init?.headers).get("x-azizam-csrf"))).toEqual(["stale", "fresh"]);
    expect(replace).toHaveBeenCalledWith("/sign-in/");
  });

  it("a 403 that persists while the session is still live is a gateway error, not a sign-out", async () => {
    route("POST /logout", () => json({ error: { code: "csrf_required", message: "Refresh" } }, 403));
    await sessionEnd.endSession("sign-out");
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "failed", reason: "gateway-error", tries: 1, trying: false });
    expect(replace).not.toHaveBeenCalled();
  });

  it("a 5xx from the gateway is a gateway error", async () => {
    route("POST /logout", () => json({}, 503));
    await sessionEnd.endSession("sign-out");
    expect(sessionEnd.getSessionScreen()).toMatchObject({ kind: "failed", reason: "gateway-error" });
  });

  it("a request that never gets a reply is 'no answer' once it has waited 10 seconds", async () => {
    vi.useFakeTimers();
    route("POST /logout", hangs);
    const ending = sessionEnd.endSession("sign-out");
    await vi.advanceTimersByTimeAsync(10_000);
    await ending;
    expect(sessionEnd.getSessionScreen()).toMatchObject({ kind: "failed", reason: "no-answer" });
    expect(replace).not.toHaveBeenCalled();
  });

  it("a dropped connection is 'no answer'", async () => {
    route("POST /logout", offline);
    await sessionEnd.endSession("sign-out");
    expect(sessionEnd.getSessionScreen()).toMatchObject({ kind: "failed", reason: "no-answer" });
  });

  it("'Try again' after a failure ends the session once the gateway answers", async () => {
    let answers = 0;
    route("POST /logout", () => {
      answers += 1;
      return answers === 1 ? offline(undefined) : json({ next: "/sign-in/" });
    });
    await sessionEnd.endSession("sign-out");
    await sessionEnd.retrySignOut();
    expect(sessionEnd.getSessionScreen()).toMatchObject({ kind: "signing-out" });
    expect(replace).toHaveBeenCalledWith("/sign-in/");
  });

  it("the second failure adds the way out to the failure screen", async () => {
    route("POST /logout", offline);
    await sessionEnd.endSession("sign-out");
    await sessionEnd.retrySignOut();
    expect(sessionEnd.getSessionScreen()).toMatchObject({ kind: "failed", tries: 2 });
  });

  it("a browser with no session still ends cleanly: the gateway answers login_required", async () => {
    route("GET /me/api/session", () => json({ authenticated: false }));
    route("GET /admin/api/session", () => json({ error: { code: "login_required", message: "Sign in first." } }, 401));
    route("POST /logout", () => json({ error: { code: "login_required", message: "Sign in first." } }, 401));
    await sessionEnd.endSession("sign-out");
    expect(replace).toHaveBeenCalledWith("/sign-in/");
  });

  it("a second request to end the session while this one leaves joins it and sends nothing more", async () => {
    vi.useFakeTimers();
    route("POST /logout", hangs);
    const first = sessionEnd.endSession("sign-out");
    const second = sessionEnd.endSession("sign-out");
    expect(second).toBe(first);
    await vi.advanceTimersByTimeAsync(10_000);
    await first;
    expect(sent("POST /logout")).toHaveLength(1);
  });

  it("the owner's admin session supplies the token when the snapshot has none", async () => {
    route("GET /me/api/session", () => json({ authenticated: true, session_view_id: "view-a" }));
    route("GET /admin/api/session", () => json({ actor: { csrf_token: "admin-csrf" } }));
    await sessionEnd.endSession("sign-out");
    const [[, init]] = sent("POST /logout");
    expect(new Headers(init?.headers).get("x-azizam-csrf")).toBe("admin-csrf");
  });

  it("sign out everywhere sends the member's own CSRF token and never reads the admin session", async () => {
    route("POST /auth/api/sign-out-everywhere", () => json({ next: "/sign-in/" }));
    await sessionEnd.endSession("sign-out-everywhere");
    const [[, init]] = sent("POST /auth/api/sign-out-everywhere");
    expect(new Headers(init?.headers).get("x-azizam-csrf")).toBe("csrf-1");
    expect(sent("GET /admin/api/session")).toHaveLength(0);
    expect(replace).toHaveBeenCalledWith("/sign-in/");
  });

  it("sign out everywhere follows only a page on this site", async () => {
    route("POST /auth/api/sign-out-everywhere", () => json({ next: "https://elsewhere.example/" }));
    await sessionEnd.endSession("sign-out-everywhere");
    expect(replace).toHaveBeenCalledWith("/sign-in/");
  });
});

describe("the request guard", () => {
  it("refuses a request once the session has ended", async () => {
    await sessionEnd.endSession("sign-out");
    vi.mocked(fetch).mockClear();
    await expect(networkFetch("/me/api/home")).rejects.toMatchObject({ code: "session_ended" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("aborts a request that is in flight when the session ends", async () => {
    route("GET /me/api/home", hangs);
    const pending = networkFetch("/me/api/home");
    const signal = vi.mocked(fetch).mock.calls.at(-1)![1]!.signal!;
    route("POST /logout", () => json({ next: "/sign-in/" }));
    void sessionEnd.endSession("sign-out");
    await expect(pending).rejects.toMatchObject({ code: "session_ended" });
    expect(signal.aborted).toBe(true);
  });

  it("drops an answer that arrives after the session ended", async () => {
    let answer!: (response: Response) => void;
    route("GET /me/api/home", () => new Promise<Response>((resolve) => (answer = resolve)));
    const pending = networkFetch("/me/api/home");
    await vi.waitFor(() => expect(answer).toBeDefined());
    await sessionEnd.endSession("sign-out");
    answer(json({ late: true }));
    await expect(pending).rejects.toMatchObject({ code: "session_ended" });
  });

  it("drops an answer computed for a person the page no longer shows", async () => {
    let answer!: (response: Response) => void;
    route("GET /me/api/home", () => new Promise<Response>((resolve) => (answer = resolve)));
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-a" });
    const pending = networkFetch("/me/api/home");
    await vi.waitFor(() => expect(answer).toBeDefined());
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-b" });
    answer(json({ someone: "else" }));
    await expect(pending).rejects.toMatchObject({ code: "session_ended" });
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "other-tab", cause: "server" });
  });

  it("an expired session is left to the sign-in redirect, not the other-tab screen", () => {
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-a" });
    sessionEnd.observeViewer({ authenticated: false, epoch: null });
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "idle" });
    expect(sessionEnd.sessionIsClosed()).toBe(false);
  });
});

/** A BroadcastChannel that delivers to every other channel of the same name, as the browser does across tabs. */
class FakeChannel {
  static channels: FakeChannel[] = [];
  onmessage: ((event: { data: unknown }) => void) | null = null;
  constructor(readonly name: string) {
    FakeChannel.channels.push(this);
  }
  postMessage(data: unknown) {
    for (const other of FakeChannel.channels) {
      if (other !== this && other.name === this.name) other.onmessage?.({ data: structuredClone(data) });
    }
  }
  close() {
    FakeChannel.channels = FakeChannel.channels.filter((channel) => channel !== this);
  }
}

/** Loads a second copy of the module: a second tab with its own JavaScript heap. */
async function anotherTab() {
  vi.resetModules();
  // A fresh copy of navigate too: silence its real navigation in this second page.
  const { navigate: otherNavigate } = await import("@/lib/navigate");
  vi.spyOn(otherNavigate, "replace").mockImplementation(() => {});
  return import("@/lib/session-end");
}

describe("other tabs", () => {
  let stops: Array<() => void>;
  beforeEach(() => {
    FakeChannel.channels = [];
    stops = [];
  });
  afterEach(() => {
    for (const stop of stops) stop();
  });

  it("a second tab hears leaving at once and signed-out when the gateway confirms", async () => {
    vi.stubGlobal("BroadcastChannel", FakeChannel);
    const other = await anotherTab();
    stops.push(sessionEnd.startSessionSync(), other.startSessionSync());
    let answer!: () => void;
    route("POST /logout", () => new Promise<Response>((resolve) => (answer = () => resolve(json({ next: "/sign-in/" })))));

    const ending = sessionEnd.endSession("sign-out");
    expect(other.getSessionScreen()).toEqual({ kind: "signing-out" });
    expect(other.sessionIsClosed()).toBe(true);

    await vi.waitFor(() => expect(answer).toBeDefined());
    answer();
    await ending;
    expect(other.getSessionScreen()).toEqual({ kind: "other-tab", cause: "sign-out" });
  });

  it("a second tab mirrors a failed sign-out, so it shows the same screen", async () => {
    vi.stubGlobal("BroadcastChannel", FakeChannel);
    const other = await anotherTab();
    stops.push(sessionEnd.startSessionSync(), other.startSessionSync());
    route("POST /logout", offline);
    await sessionEnd.endSession("sign-out");
    expect(other.getSessionScreen()).toEqual({ kind: "failed", reason: "no-answer", tries: 1, trying: false });
  });

  it("the storage event carries the same record when BroadcastChannel is unavailable", async () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    const other = await anotherTab();
    stops.push(other.startSessionSync());
    let answer!: () => void;
    route("POST /logout", () => new Promise<Response>((resolve) => (answer = () => resolve(json({ next: "/sign-in/" })))));
    const ending = sessionEnd.endSession("sign-out");
    // A browser fires `storage` in the other tabs for every write; jsdom does not, so the test fires it.
    window.dispatchEvent(new StorageEvent("storage", { key: "azizam.handoff", newValue: localStorage.getItem("azizam.handoff") }));
    expect(other.getSessionScreen()).toEqual({ kind: "signing-out" });
    await vi.waitFor(() => expect(answer).toBeDefined());
    answer();
    await ending;
    window.dispatchEvent(new StorageEvent("storage", { key: "azizam.handoff", newValue: localStorage.getItem("azizam.handoff") }));
    expect(other.getSessionScreen()).toEqual({ kind: "other-tab", cause: "sign-out" });
  });

  it("a malformed handoff record is ignored, never trusted", () => {
    localStorage.setItem("azizam.handoff", "{not json");
    expect(sessionEnd.readHandoff()).toBeNull();
    localStorage.setItem("azizam.handoff", JSON.stringify({ state: "LEAVING", since: 1, kind: "wipe-everything" }));
    expect(sessionEnd.readHandoff()).toBeNull();
  });
});

describe("a page that opens or is restored during a sign-out", () => {
  it("a sign-out left pending by an earlier visit finishes before anything paints", async () => {
    localStorage.setItem(
      "azizam.handoff",
      JSON.stringify({ state: "LEAVING", since: 1, kind: "sign-out", failure: "gateway-error", tries: 2 }),
    );
    expect(sessionEnd.resumeSignOut()).toBe(true);
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "signing-out" });
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/sign-in/"));
    expect(sent("POST /logout")).toHaveLength(1);
  });

  it("a restored page whose session changed shows that the tab was signed out elsewhere", async () => {
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-a" });
    route("GET /me/api/session", () => json({ ...signedIn, session_view_id: "view-b" }));
    await sessionEnd.checkPageShown();
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "other-tab", cause: "server" });
  });

  it("a restored page names the person who switched when the record says so", async () => {
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-a" });
    route("GET /me/api/session", () => json({ authenticated: false }));
    localStorage.setItem("azizam.handoff", JSON.stringify({ state: "SIGNED_OUT", since: 1, kind: "switch" }));
    await sessionEnd.checkPageShown();
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "other-tab", cause: "switch" });
  });

  it("a restored page of the same session is shown as it was", async () => {
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-a" });
    await sessionEnd.checkPageShown();
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "idle" });
  });

  it("Continue loads the start page for someone who has signed in since, else the sign-in page", async () => {
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-a" });
    route("GET /me/api/session", () => json({ authenticated: false }));
    await sessionEnd.checkPageShown();
    await sessionEnd.continueFromOtherTab();
    expect(replace).toHaveBeenLastCalledWith("/sign-in/");
    route("GET /me/api/session", () => json(signedIn));
    await sessionEnd.continueFromOtherTab();
    expect(replace).toHaveBeenLastCalledWith("/");
  });

  it("the head script hides a page that is entering the back/forward cache", () => {
    expect(sessionEnd.SESSION_HEAD_SCRIPT).toContain("pagehide");
    expect(sessionEnd.SESSION_HEAD_SCRIPT).toContain(sessionEnd.CHECK_CLASS);
  });
});

describe("the storage that survives", () => {
  it("keeps the handoff record and nothing else of the signed-out person", async () => {
    localStorage.setItem("azizam:unfinished-key-rotations", "{}");
    await sessionEnd.endSession("switch");
    expect(JSON.parse(localStorage.getItem("azizam.handoff")!)).toMatchObject({ state: "SIGNED_OUT", kind: "switch" });
    expect(localStorage.getItem("azizam:unfinished-key-rotations")).toBeNull();
  });
});

describe("this tab's own session storage", () => {
  it("Continue on the other-tab screen clears the tab's session storage, not only the allowlisted keys", async () => {
    sessionEnd.observeViewer({ authenticated: true, epoch: "view-a" });
    route("GET /me/api/session", () => json({ authenticated: false }));
    await sessionEnd.checkPageShown();
    sessionStorage.setItem("azizam:job-notices:v1:person-1", '["job-1"]');
    sessionStorage.setItem("azizam.handoff.last", "{}");
    await sessionEnd.continueFromOtherTab();
    expect(sessionStorage.getItem("azizam:job-notices:v1:person-1")).toBeNull();
    expect(Object.keys(sessionStorage)).toEqual(["azizam.handoff.last"]);
    expect(replace).toHaveBeenCalledWith("/sign-in/");
  });
});

describe("the stores a session end clears", () => {
  it("closes an open confirm-it's-you prompt as cancelled when the session ends", async () => {
    const prompt = requestReauth({ methods: ["password"], windowSeconds: 900, oidcUrl: "/login?reauth=1" });
    await sessionEnd.endSession("sign-out");
    await expect(prompt).resolves.toBe(false);
  });
});
