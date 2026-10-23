import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as sessionEnd from "@/lib/session-end";
import { navigate } from "@/lib/navigate";

// The sign-in page's Forget line and its order need the tile of the person who left. The browser cannot
// read a tile from the session, so the sign-out matches the signed-in name against the remembered list
// before it ends the session (SECURITY.md section 4, `azizam.handoff.last`). These cases pin that record.
let replace: ReturnType<typeof vi.spyOn>;
type Handler = (init: RequestInit | undefined) => Response | Promise<Response>;
let routes: Map<string, Handler>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
const lea = { tile: "t_lea", name: "Léa Okafor", avatar: { variant: "v4", initial: "L" }, method: "password", provider: null, hint: null };
const robin = { tile: "t_robin", name: "Robin Okafor", avatar: { variant: "v1", initial: "R" }, method: "password", provider: null, hint: null };
const leaSession = {
  authenticated: true,
  csrf_token: "csrf-1",
  session_view_id: "view-a",
  user: { person_id: "usr_lea", name: "Léa Okafor", email: "lea@example.test" },
};

beforeEach(() => {
  replace = vi.spyOn(navigate, "replace").mockImplementation(() => {});
  routes = new Map();
  routes.set("GET /me/api/session", () => json(leaSession));
  routes.set("POST /logout", () => json({ next: "/sign-in/" }));
  routes.set("POST /auth/api/sign-out-everywhere", () => json({ next: "/sign-in/" }));
  routes.set("GET /auth/api/remembered", () => json({ remember: true, people: [lea, robin] }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${String(input)}`;
      const handler = routes.get(key);
      if (!handler) return json({ error: { code: "not_found", message: key } }, 404);
      return handler(init);
    }),
  );
  sessionStorage.clear();
  localStorage.clear();
  sessionEnd.resetSessionStateForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionEnd.resetSessionStateForTests();
});

const lastRecord = () => sessionStorage.getItem("azizam.handoff.last");

describe("the record of who left", () => {
  it("a sign-out records the tile of the person who left, matched by their name", async () => {
    await sessionEnd.endSession("sign-out");
    expect(lastRecord()).toBe(JSON.stringify({ tile: "t_lea" }));
    expect(replace).toHaveBeenCalledWith("/sign-in/?after=sign-out");
  });

  it("a switch records the same tile, so the sign-in page can name that person and move them last", async () => {
    await sessionEnd.endSession("switch");
    expect(lastRecord()).toBe(JSON.stringify({ tile: "t_lea" }));
    expect(replace).toHaveBeenCalledWith("/sign-in/?after=switch");
  });

  it("a sign-out everywhere records no tile, since its sign-in page offers no Forget line", async () => {
    await sessionEnd.endSession("sign-out-everywhere");
    expect(lastRecord()).toBe("{}");
    expect(replace).toHaveBeenCalledWith("/sign-in/?after=everywhere");
  });

  it("records no tile when the person who left is not on the list", async () => {
    routes.set("GET /auth/api/remembered", () => json({ remember: true, people: [robin] }));
    await sessionEnd.endSession("sign-out");
    expect(lastRecord()).toBe("{}");
    expect(replace).toHaveBeenCalledWith("/sign-in/?after=sign-out");
  });

  it("records no tile when two remembered people share the name", async () => {
    routes.set("GET /auth/api/remembered", () =>
      json({ remember: true, people: [lea, { ...lea, tile: "t_lea2", hint: "l…@other.test" }] }),
    );
    await sessionEnd.endSession("sign-out");
    expect(lastRecord()).toBe("{}");
  });

  it("records no tile when the list cannot be read, and still ends the session", async () => {
    routes.set("GET /auth/api/remembered", () => json({ error: { code: "login_rate_limited", message: "Too many" } }, 429));
    await sessionEnd.endSession("sign-out");
    expect(lastRecord()).toBe("{}");
    expect(sessionEnd.getSessionScreen()).toEqual({ kind: "signing-out" });
    expect(replace).toHaveBeenCalledWith("/sign-in/?after=sign-out");
  });

  it("a sign-out whose person is unknown to this tab records no tile", async () => {
    routes.set("GET /me/api/session", () => json({ ...leaSession, user: undefined }));
    await sessionEnd.endSession("sign-out");
    expect(lastRecord()).toBe("{}");
  });

  // Pins today's name match, which the server's exact match will replace. The signed-in person has no display name
  // (the session names them by email) and no remembered entry of their own, but another remembered person is named
  // "lea", the local part of that email. Today the browser records that other person as the one who left, and the
  // sign-in page would offer to forget them. When the server names the signed-in person's entry exactly, this case
  // changes with it.
  it("pins the name match today: a signed-in person who is not remembered is matched to another person named as their email's local part", async () => {
    const other = { ...robin, tile: "t_other", name: "lea" };
    routes.set("GET /me/api/session", () =>
      json({ ...leaSession, user: { person_id: "usr_lea", name: "lea@example.test", email: "lea@example.test" } }),
    );
    routes.set("GET /auth/api/remembered", () => json({ remember: true, people: [robin, other] }));
    await sessionEnd.endSession("sign-out");
    expect(lastRecord()).toBe(JSON.stringify({ tile: "t_other" }));
    expect(replace).toHaveBeenCalledWith("/sign-in/?after=sign-out");
  });
});
