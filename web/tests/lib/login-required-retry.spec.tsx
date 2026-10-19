import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { adminFetch, ApiError, resetSessionCache } from "@/lib/api";
import { accountApi } from "@/lib/account-api";

// A write answered `login_required` wrote nothing (decision D41). The session may have renewed while the request
// was in flight, so the client sends it once more with the cookie the browser holds by then.

const loginRequired = () =>
  Response.json(
    { error: { code: "login_required", message: "Sign in again to do this." } },
    { status: 401 },
  );

type Call = { path: string; method: string };
let calls: Call[];
let replies: Map<string, Array<() => Response>>;

const callsTo = (path: string) => calls.filter((call) => call.path === path);

beforeEach(() => {
  calls = [];
  replies = new Map();
  resetSessionCache();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      calls.push({ path, method: init?.method ?? "GET" });
      if (path === "/admin/api/session")
        return Response.json({ actor: { subject: "owner", email: null, roles: ["owner"], csrf_token: "csrf" } });
      if (path === "/me/api/session") return Response.json({ authenticated: true, csrf_token: "me-csrf" });
      const next = replies.get(path)?.shift();
      return next ? next() : Response.json({});
    }),
  );
});
afterEach(() => {
  resetSessionCache();
  vi.unstubAllGlobals();
});

it("sends an admin write once more when it answers login_required, and returns the second answer", async () => {
  replies.set("/admin/api/apps/a1/keys", [loginRequired, () => Response.json({ credential: { id: "c1" } }, { status: 201 })]);

  const result = await adminFetch<{ credential: { id: string } }>("/admin/api/apps/a1/keys", {
    method: "POST",
    body: { label: "x" },
  });

  expect(result.credential.id).toBe("c1");
  expect(callsTo("/admin/api/apps/a1/keys")).toHaveLength(2);
});

it("sends an admin write only once more: a second login_required is the answer", async () => {
  replies.set("/admin/api/apps/a1/keys", [loginRequired, loginRequired, loginRequired]);

  const request = adminFetch("/admin/api/apps/a1/keys", { method: "POST", body: {} });

  await expect(request).rejects.toMatchObject({ code: "login_required", status: 401 });
  await expect(request).rejects.toBeInstanceOf(ApiError);
  expect(callsTo("/admin/api/apps/a1/keys")).toHaveLength(2);
});

it("does not repeat a read that answers login_required", async () => {
  replies.set("/admin/api/apps", [loginRequired, () => Response.json({ apps: [] })]);

  await expect(adminFetch("/admin/api/apps")).rejects.toMatchObject({ code: "login_required" });
  expect(callsTo("/admin/api/apps")).toHaveLength(1);
});

it("sends a member's key request once more when it answers login_required", async () => {
  replies.set("/me/api/apps/app_1/keys", [loginRequired, () => Response.json({ credential: { id: "c2" } }, { status: 201 })]);

  const credential = await accountApi.mintKey("app_1", "laptop");

  expect(credential).toMatchObject({ id: "c2" });
  expect(callsTo("/me/api/apps/app_1/keys")).toHaveLength(2);
});

it("sends a member's write only once more", async () => {
  replies.set("/me/api/apps/app_1/keys", [loginRequired, loginRequired, loginRequired]);

  await expect(accountApi.mintKey("app_1", "laptop")).rejects.toMatchObject({ code: "login_required", status: 401 });
  expect(callsTo("/me/api/apps/app_1/keys")).toHaveLength(2);
});
