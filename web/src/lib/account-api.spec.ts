import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { accountApi } from "./account-api";

// A rename is a PATCH to the person's own routes with the headers every other /me write sends.
type Sent = { path: string; method: string; body: unknown; headers: Headers };
let sent: Sent[];
let reply: { status: number; body: unknown };

beforeEach(() => {
  sent = [];
  reply = { status: 200, body: {} };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/me/api/session")
        return new Response(JSON.stringify({ csrf_token: "csrf-1", apps: [] }));
      sent.push({
        path,
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: new Headers(init?.headers),
      });
      return new Response(JSON.stringify(reply.body), { status: reply.status });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

it("renameApp sends PATCH {name} with the user-action and csrf headers and returns the saved app", async () => {
  reply.body = { app: { id: "app/1", name: "Saved name" } };
  const app = await accountApi.renameApp("app/1", "New name");
  expect(app).toEqual({ id: "app/1", name: "Saved name" });
  expect(sent).toHaveLength(1);
  expect(sent[0].method).toBe("PATCH");
  expect(sent[0].path).toBe("/me/api/apps/app%2F1");
  expect(sent[0].body).toEqual({ name: "New name" });
  expect(sent[0].headers.get("content-type")).toBe("application/json");
  expect(sent[0].headers.get("x-azizam-user-action")).toBe("1");
  expect(sent[0].headers.get("x-azizam-csrf")).toBe("csrf-1");
});

it("renameKey sends PATCH {display_name} with the same headers and returns the saved key", async () => {
  reply.body = { credential: { id: "key-1", display_name: "Saved label" } };
  const key = await accountApi.renameKey("key-1", "New label");
  expect(key).toEqual({ id: "key-1", display_name: "Saved label" });
  expect(sent[0].method).toBe("PATCH");
  expect(sent[0].path).toBe("/me/api/credentials/key-1");
  expect(sent[0].body).toEqual({ display_name: "New label" });
  expect(sent[0].headers.get("x-azizam-user-action")).toBe("1");
  expect(sent[0].headers.get("x-azizam-csrf")).toBe("csrf-1");
});

it("turns a refusal into an ApiError with the gateway's code, words and status", async () => {
  reply = {
    status: 409,
    body: { error: { code: "app_name_taken", message: "You already have an app with that name." } },
  };
  const failure = await accountApi.renameApp("app-1", "Taken").catch((e: unknown) => e);
  expect(failure).toBeInstanceOf(ApiError);
  expect(failure).toMatchObject({
    code: "app_name_taken",
    message: "You already have an app with that name.",
    status: 409,
  });
});
