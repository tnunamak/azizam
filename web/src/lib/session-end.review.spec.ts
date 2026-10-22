// Review repros for W1 (not part of the branch). Each `it` states the behaviour the design asks for;
// a failing one is a finding.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as sessionEnd from "@/lib/session-end";
import { navigate } from "@/lib/navigate";

const sonner = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn() }));
vi.mock("sonner", () => ({ toast: sonner }));

let replace: ReturnType<typeof vi.spyOn>;
type Handler = (init: RequestInit | undefined) => Response | Promise<Response>;
let routes: Map<string, Handler>;
const sent = (key: string) =>
  vi.mocked(fetch).mock.calls.filter(([url, init]) => `${init?.method ?? "GET"} ${String(url)}` === key);
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const offline: Handler = () => {
  throw new TypeError("Failed to fetch");
};
const signedIn = { authenticated: true, csrf_token: "csrf-1", session_view_id: "view-a" };

beforeEach(() => {
  replace = vi.spyOn(navigate, "replace").mockImplementation(() => {});
  routes = new Map();
  routes.set("GET /me/api/session", () => json(signedIn));
  routes.set("POST /logout", () => json({ next: "/sign-in/" }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const handler = routes.get(`${init?.method ?? "GET"} ${String(input)}`);
      if (!handler) throw new Error(`no route for ${init?.method ?? "GET"} ${String(input)}`);
      return handler(init);
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  // @ts-expect-error restore the prototype getter
  delete navigator.onLine;
  sessionEnd.resetSessionStateForTests();
});

describe("R1 navigator.onLine is a hint, not a gate", () => {
  it("Try again asks the gateway even when the browser says it is offline", async () => {
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });
    await sessionEnd.endSession("sign-out");
    expect(sessionEnd.getSessionScreen()).toMatchObject({ kind: "failed", reason: "no-answer" });
    await sessionEnd.retrySignOut();
    // The gateway is reachable (fetch is stubbed to answer), so the sign-out must complete.
    expect(sent("POST /logout")).toHaveLength(1);
    expect(replace).toHaveBeenCalledWith("/sign-in/");
  });
});

describe("R2 a restored page that never saw a session_view_id", () => {
  it("still learns that the browser signed out (service-admin cookie sessions have no view id)", async () => {
    routes.set("GET /me/api/session", () => json({ authenticated: false }));
    localStorage.setItem("azizam.handoff", JSON.stringify({ state: "SIGNED_OUT", since: 1, kind: "sign-out" }));
    await sessionEnd.checkPageShown(); // epoch was never observed: the page was showing an admin-key session
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "other-tab", cause: "sign-out" });
  });
});

describe("R3 the other tabs wipe their own sessionStorage", () => {
  it("a tab that hears signed-out removes the person's job notices and opened-creation ids", () => {
    const stop = sessionEnd.startSessionSync();
    sessionStorage.setItem("azizam:job-notices:v1:person-1", '["job-1"]');
    sessionStorage.setItem("azizam:opened-creations:v1:person-1", '["asset-1"]');
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: "azizam.handoff",
        newValue: JSON.stringify({ state: "SIGNED_OUT", since: 1, kind: "sign-out" }),
      }),
    );
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "other-tab", cause: "sign-out" });
    expect(sessionStorage.getItem("azizam:job-notices:v1:person-1")).toBeNull();
    expect(sessionStorage.getItem("azizam:opened-creations:v1:person-1")).toBeNull();
    stop();
  });
});

describe("R4 toasts of the ended session", () => {
  it("a toast queued before the session ended is not shown after it", async () => {
    const { toast, markToasterReady } = await import("@/lib/toast");
    toast.success("Moved “Birthday photos” to the trash"); // queued: the toaster is not mounted yet
    await sessionEnd.endSession("sign-out");
    markToasterReady(); // the lazy Toaster mounts after the cover is up
    await vi.waitFor(() => expect(sonner.success.mock.calls.length + sonner.error.mock.calls.length).toBeGreaterThanOrEqual(0));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(sonner.success).not.toHaveBeenCalled();
  });
});

describe("R5 a tab that mirrors a sign-out whose starter vanished", () => {
  it("does not stay on 'Signing out…' forever: it takes over or offers Try again", async () => {
    vi.useFakeTimers();
    const stop = sessionEnd.startSessionSync();
    // Another tab started the sign-out and was closed before it published a result.
    const record = { state: "LEAVING", since: Date.now(), kind: "sign-out", failure: null, tries: 0 };
    localStorage.setItem("azizam.handoff", JSON.stringify(record));
    window.dispatchEvent(new StorageEvent("storage", { key: "azizam.handoff", newValue: JSON.stringify(record) }));
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "signing-out" });
    await vi.advanceTimersByTimeAsync(120_000);
    const screen = sessionEnd.getSessionScreen();
    const recovered = screen.kind === "failed" || sent("POST /logout").length > 0 || replace.mock.calls.length > 0;
    expect(recovered).toBe(true);
    stop();
  });
});

describe("R6 no CSRF token anywhere", () => {
  it("does not post /logout with an empty token (the replaced test 'does not submit logout after fallback failure')", async () => {
    routes.set("GET /me/api/session", () => json({}));
    routes.set("GET /admin/api/session", () => json({}));
    routes.set("POST /logout", () => json({ error: { code: "csrf_required" } }, 403));
    await sessionEnd.endSession("sign-out");
    expect(sent("POST /logout")).toHaveLength(0);
  });
});

describe("R7 a 200 from something that is not the gateway", () => {
  it("a captive portal that answers 200 HTML to /logout is not a sign-out", async () => {
    routes.set("POST /logout", () => new Response("<html>Wi-Fi login</html>", { status: 200, headers: { "content-type": "text/html" } }));
    // The session is still live afterwards.
    await sessionEnd.endSession("sign-out");
    expect(replace).not.toHaveBeenCalled();
  });
});

// keep TS from dropping the unused import in older configs
void offline;
