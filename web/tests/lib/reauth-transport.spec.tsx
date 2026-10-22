import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  adminDownload,
  adminFetch,
  ApiError,
  confirmWithPassword,
  resetSessionCache,
} from "@/lib/api";
import { accountApi } from "@/lib/account-api";
import { errorCopy } from "@/lib/error-copy";
import { parseReauth, settleReauth, usePendingReauth } from "@/lib/reauth";

// The step-up contract: a risky action from a stale session answers 401
// reauth_required; adminFetch opens ONE shared prompt and retries once.

const session = (token = "csrf") =>
  Response.json({ actor: { subject: "owner", email: null, roles: ["owner"], csrf_token: token } });
const reauthBody = (methods: string[] = ["password"]) => ({
  error: {
    code: "reauth_required",
    message: "Confirm it's you to do this.",
    reauth: { methods, window_seconds: 900, oidc_url: "/login?reauth=1" },
  },
});
const stale = (methods?: string[]) => Response.json(reauthBody(methods), { status: 401 });

type Call = { path: string; method: string; headers: Headers };
let calls: Call[];
/** Replies per path, in order; a path with none left answers {}. */
let replies: Map<string, Array<() => Response>>;
let sessions: string[];

function reply(path: string, ...makers: Array<() => Response>) {
  replies.set(path, makers);
}
const callsTo = (path: string) => calls.filter((call) => call.path === path);

beforeEach(() => {
  shown = [];
  calls = [];
  replies = new Map();
  sessions = ["csrf-1", "csrf-2", "csrf-3"];
  resetSessionCache();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      calls.push({ path, method: init?.method ?? "GET", headers: new Headers(init?.headers) });
      if (path === "/admin/api/session") return session(sessions.shift() ?? "csrf-n");
      // A person's own snapshot: what the /auth/api/ routes take their CSRF token from.
      if (path === "/me/api/session") return Response.json({ authenticated: true, csrf_token: "me-csrf" });
      const next = replies.get(path)?.shift();
      return next ? next() : Response.json({});
    }),
  );
});
afterEach(() => {
  resetSessionCache();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(URL, "createObjectURL");
  Reflect.deleteProperty(URL, "revokeObjectURL");
});

/** Watches the shared prompt; `shown` holds each distinct prompt that opened. */
let shown: unknown[];
function openPrompt() {
  return renderHook(() => {
    const info = usePendingReauth();
    if (info && !shown.includes(info)) shown.push(info);
    return info;
  });
}

/** jsdom has no blob URLs; install stand-ins and remove them after the test. */
function stubBlobUrls() {
  const create = vi.fn(() => "blob:fixture");
  Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() });
  return create;
}

it("opens the prompt, then retries the request once the person confirms", async () => {
  reply("/admin/api/risky", stale, () => Response.json({ saved: true }));
  const prompt = openPrompt();
  const request = adminFetch<{ saved: boolean }>("/admin/api/risky", { method: "PUT", body: { a: 1 } });
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  expect(prompt.result.current).toEqual({
    methods: ["password"],
    windowSeconds: 900,
    oidcUrl: "/login?reauth=1",
  });
  expect(callsTo("/admin/api/risky")).toHaveLength(1);

  act(() => settleReauth(true));
  await expect(request).resolves.toEqual({ saved: true });
  expect(callsTo("/admin/api/risky")).toHaveLength(2);
  expect(prompt.result.current).toBeNull();
  // Confirming re-issues the cookie, so the retry carries a fresh CSRF token.
  const [first, second] = callsTo("/admin/api/risky");
  expect(first.headers.get("x-azizam-csrf")).toBe("csrf-1");
  expect(second.headers.get("x-azizam-csrf")).toBe("csrf-2");
  expect(second.headers.get("x-azizam-admin-action")).toBe("1");
});

it("asks a member to confirm before a key is made from an old sign-in, then sends it once more", async () => {
  reply("/me/api/apps/app_1/keys", stale, () =>
    Response.json({ credential: { id: "cred_1", api_key: "opk_x", prefix: "opk_x" } }, { status: 201 }),
  );
  const prompt = openPrompt();
  const request = accountApi.mintKey("app_1", "laptop");
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  expect(callsTo("/me/api/apps/app_1/keys")).toHaveLength(1);

  act(() => settleReauth(true));
  await expect(request).resolves.toMatchObject({ id: "cred_1" });
  expect(callsTo("/me/api/apps/app_1/keys")).toHaveLength(2);
  expect(prompt.result.current).toBeNull();
});

it("leaves the member's request refused when they cancel the confirmation, and does not ask twice", async () => {
  reply("/me/api/apps/app_1/keys", stale, stale);
  const prompt = openPrompt();
  const request = accountApi.mintKey("app_1", "laptop");
  const outcome = request.catch((error: unknown) => error);
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  act(() => settleReauth(false));
  expect(await outcome).toMatchObject({ code: "reauth_required", status: 401 });
  expect(callsTo("/me/api/apps/app_1/keys")).toHaveLength(1);
});

it("does not loop when the gateway still asks after a confirmation", async () => {
  reply("/me/api/try-key", stale, stale);
  const prompt = openPrompt();
  const request = accountApi.tryKey(null);
  const outcome = request.catch((error: unknown) => error);
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  act(() => settleReauth(true));
  expect(await outcome).toMatchObject({ code: "reauth_required" });
  expect(callsTo("/me/api/try-key")).toHaveLength(2);
});

it("retries a risky GET too", async () => {
  reply("/admin/api/invites/inv_1/link", stale, () => Response.json({ url: "https://x" }));
  const prompt = openPrompt();
  const request = adminFetch("/admin/api/invites/inv_1/link");
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  act(() => settleReauth(true));
  await expect(request).resolves.toEqual({ url: "https://x" });
});

it("shares one prompt between requests that are refused at the same time", async () => {
  reply("/admin/api/a", stale, () => Response.json({ name: "a" }));
  reply("/admin/api/b", stale, () => Response.json({ name: "b" }));
  const prompt = openPrompt();
  const a = adminFetch("/admin/api/a");
  const b = adminFetch("/admin/api/b");
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  await vi.waitFor(() => expect(calls.filter((c) => c.path.startsWith("/admin/api/") && c.path !== "/admin/api/session")).toHaveLength(2));

  act(() => settleReauth(true));
  await expect(Promise.all([a, b])).resolves.toEqual([{ name: "a" }, { name: "b" }]);
  // One answer from the person, one retry each, and the prompt never reopened.
  expect(shown).toHaveLength(1);
  expect(callsTo("/admin/api/a")).toHaveLength(2);
  expect(callsTo("/admin/api/b")).toHaveLength(2);
  expect(prompt.result.current).toBeNull();
});

it("rejects with the original reauth_required error when the person cancels", async () => {
  reply("/admin/api/risky", stale);
  const prompt = openPrompt();
  const request = adminFetch("/admin/api/risky");
  const outcome = request.catch((error: unknown) => error);
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  act(() => settleReauth(false));
  const error = await outcome;
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ code: "reauth_required", status: 401, message: "Confirm it's you to do this." });
  expect(callsTo("/admin/api/risky")).toHaveLength(1);
  expect(prompt.result.current).toBeNull();
});

it("leaves an ordinary 401 alone: no prompt, the usual error", async () => {
  reply("/admin/api/risky", () =>
    Response.json({ error: { code: "session_expired", message: "Sign in again." } }, { status: 401 }),
  );
  const prompt = openPrompt();
  await expect(adminFetch("/admin/api/risky")).rejects.toMatchObject({ code: "session_expired", status: 401 });
  expect(prompt.result.current).toBeNull();
  expect(callsTo("/admin/api/risky")).toHaveLength(1);
});

it("does not ask twice for one request", async () => {
  reply("/admin/api/risky", stale, stale);
  const prompt = openPrompt();
  const request = adminFetch("/admin/api/risky");
  const outcome = request.catch((error: unknown) => error);
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  act(() => settleReauth(true));
  expect(await outcome).toMatchObject({ code: "reauth_required", status: 401 });
  expect(callsTo("/admin/api/risky")).toHaveLength(2);
  expect(prompt.result.current).toBeNull();
});

it("treats a reauth_required body without a usable method as sign-in only", () => {
  expect(parseReauth({ error: { code: "reauth_required", message: "x", reauth: { methods: [] } } })).toMatchObject({
    methods: ["sign_in"],
  });
  expect(parseReauth({ error: { code: "session_expired", message: "x" } })).toBeNull();
  expect(parseReauth(null)).toBeNull();
});

it("saves a download after the person confirms, under the name the gateway gives", async () => {
  reply(
    "/admin/api/settings/effective/export",
    stale,
    () =>
      new Response('{"config":{}}', {
        headers: { "content-disposition": 'attachment; filename="gateway-effective-config.json"' },
      }),
  );
  stubBlobUrls();
  const saved: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(`${this.download} ${this.href}`);
  });
  const prompt = openPrompt();
  const request = adminDownload("/admin/api/settings/effective/export", "fallback.json");
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  expect(saved).toEqual([]);
  act(() => settleReauth(true));
  await request;
  expect(saved).toEqual(["gateway-effective-config.json blob:fixture"]);
  expect(callsTo("/admin/api/settings/effective/export")).toHaveLength(2);
});

it("does not save anything when the person cancels a download", async () => {
  reply("/admin/api/settings/effective/export", stale);
  const create = stubBlobUrls();
  const prompt = openPrompt();
  const outcome = adminDownload("/admin/api/settings/effective/export", "x.json").catch((e: unknown) => e);
  await vi.waitFor(() => expect(prompt.result.current).not.toBeNull());
  act(() => settleReauth(false));
  expect(await outcome).toMatchObject({ code: "reauth_required" });
  expect(create).not.toHaveBeenCalled();
});

it("confirms a password with the CSRF header and surfaces a wrong one", async () => {
  reply(
    "/auth/api/reauth",
    () => Response.json({ error: { code: "invalid_password", message: "That password is not right." } }, { status: 401 }),
    () => Response.json({ ok: true, authenticated_at: 1 }),
  );
  await expect(confirmWithPassword("nope")).rejects.toMatchObject({
    code: "invalid_password",
    status: 401,
    message: "That password is not right.",
  });
  await expect(confirmWithPassword("right")).resolves.toBeUndefined();
  const [wrong, right] = callsTo("/auth/api/reauth");
  expect(wrong.method).toBe("POST");
  expect(wrong.headers.get("x-azizam-csrf")).toBe("me-csrf");
  expect(right.headers.get("content-type")).toBe("application/json");
});

it("words reauth_required as a request to confirm, not as a signed-out session", () => {
  const copy = errorCopy(new ApiError("reauth_required", "Confirm it's you to do this.", 401));
  expect(copy.message).toBe("Confirm it’s you to continue.");
  expect(copy.recovery).toBe("retry");
  expect(errorCopy(new ApiError("session_expired", "x", 401)).recovery).toBe("sign-in");
});
