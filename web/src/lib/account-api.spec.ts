import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import {
  accountApi,
  administratorsSentence,
  administratorsToConfirm,
  hasAdministrators,
  type AdministratorsOnRecord,
} from "./account-api";
import { requestReauth } from "./reauth";

vi.mock("./reauth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./reauth")>()),
  requestReauth: vi.fn(),
}));

// A rename is a PATCH to the person's own routes with the headers every other /me write sends.
type Sent = { path: string; method: string; body: unknown; headers: Headers };
let sent: Sent[];
let reply: { status: number; body: unknown };

beforeEach(() => {
  sent = [];
  reply = { status: 200, body: {} };
  vi.mocked(requestReauth).mockReset();
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

const reauthRequired = {
  status: 401,
  body: {
    error: {
      code: "reauth_required",
      message: "Confirm it is you to continue.",
      reauth: { methods: ["password"], window_seconds: 900, oidc_url: "/login?reauth=1" },
    },
  },
};

it("keepKey posts to the key's adopt route with the user-action and csrf headers and returns the kept key", async () => {
  reply.body = { credential: { id: "key-1", created_by: "person-1" } };
  const key = await accountApi.keepKey("key-1");
  expect(key).toEqual({ id: "key-1", created_by: "person-1" });
  expect(sent).toHaveLength(1);
  expect(sent[0].method).toBe("POST");
  expect(sent[0].path).toBe("/me/api/credentials/key-1/adopt");
  expect(sent[0].headers.get("x-azizam-user-action")).toBe("1");
  expect(sent[0].headers.get("x-azizam-csrf")).toBe("csrf-1");
});

it("keepTrustedAddress posts to the entry's adopt route with the same headers", async () => {
  await accountApi.keepTrustedAddress("app-1", "tad-1");
  expect(sent).toHaveLength(1);
  expect(sent[0].method).toBe("POST");
  expect(sent[0].path).toBe("/me/api/apps/app-1/trusted-addresses/tad-1/adopt");
  expect(sent[0].headers.get("x-azizam-user-action")).toBe("1");
  expect(sent[0].headers.get("x-azizam-csrf")).toBe("csrf-1");
});

it("keepKey asks for a recent sign-in, then sends the keep again once the person confirms", async () => {
  const kept = { id: "key-1", created_by: "person-1" };
  reply = reauthRequired;
  vi.mocked(requestReauth).mockImplementation(async () => {
    reply = { status: 200, body: { credential: kept } };
    return true;
  });
  const key = await accountApi.keepKey("key-1");
  expect(key).toEqual(kept);
  expect(requestReauth).toHaveBeenCalledTimes(1);
  expect(requestReauth).toHaveBeenCalledWith(
    expect.objectContaining({ methods: ["password"], windowSeconds: 900 }),
  );
  expect(sent.map((request) => request.path)).toEqual([
    "/me/api/credentials/key-1/adopt",
    "/me/api/credentials/key-1/adopt",
  ]);
});

it("keepKey keeps nothing when the person does not confirm, and returns the gateway's words", async () => {
  reply = reauthRequired;
  vi.mocked(requestReauth).mockResolvedValue(false);
  const failure = await accountApi.keepKey("key-1").catch((e: unknown) => e);
  expect(failure).toBeInstanceOf(ApiError);
  expect(failure).toMatchObject({
    code: "reauth_required",
    message: "Confirm it is you to continue.",
    status: 401,
  });
  expect(sent).toHaveLength(1);
});

it("replaceKey posts to the key's replace route with the same headers, and returns the new key and the one it replaced", async () => {
  reply.body = {
    credential: { id: "key-new", api_key: "opk_new_secret" },
    replaced_credential_id: "key-1",
  };
  const replaced = await accountApi.replaceKey("key-1");
  expect(replaced).toEqual(reply.body);
  expect(sent).toHaveLength(1);
  expect(sent[0].method).toBe("POST");
  expect(sent[0].path).toBe("/me/api/credentials/key-1/replace");
  expect(sent[0].body).toBeUndefined();
  expect(sent[0].headers.get("x-azizam-user-action")).toBe("1");
  expect(sent[0].headers.get("x-azizam-csrf")).toBe("csrf-1");
});

it("replaceKey asks for a recent sign-in, then replaces the key once the person confirms", async () => {
  const replaced = {
    credential: { id: "key-new", api_key: "opk_new_secret" },
    replaced_credential_id: "key-1",
  };
  reply = reauthRequired;
  vi.mocked(requestReauth).mockImplementation(async () => {
    reply = { status: 201, body: replaced };
    return true;
  });
  const answer = await accountApi.replaceKey("key-1");
  expect(answer).toEqual(replaced);
  expect(requestReauth).toHaveBeenCalledTimes(1);
  expect(sent.map((request) => request.path)).toEqual([
    "/me/api/credentials/key-1/replace",
    "/me/api/credentials/key-1/replace",
  ]);
});

it("replaceKey replaces nothing when the person does not confirm, and returns the gateway's words", async () => {
  reply = reauthRequired;
  vi.mocked(requestReauth).mockResolvedValue(false);
  const failure = await accountApi.replaceKey("key-1").catch((e: unknown) => e);
  expect(failure).toBeInstanceOf(ApiError);
  expect(failure).toMatchObject({
    code: "reauth_required",
    message: "Confirm it is you to continue.",
    status: 401,
  });
  expect(sent).toHaveLength(1);
});

it("replaceKey turns a refusal for a turned-off App into an ApiError with the gateway's code", async () => {
  reply = {
    status: 403,
    body: { error: { code: "app_disabled", message: "App is disabled" } },
  };
  const failure = await accountApi.replaceKey("key-1").catch((e: unknown) => e);
  expect(failure).toMatchObject({ code: "app_disabled", message: "App is disabled", status: 403 });
});

it("addTrustedAddress posts the address and note to the App's list, and returns the new entry", async () => {
  reply = { status: 201, body: { trusted_address: { id: "tad-9" } } };
  const added = await accountApi.addTrustedAddress("app-1", { address: "10.0.0.8", note: "Hall speaker" });
  expect(added).toEqual({ trusted_address: { id: "tad-9" } });
  expect(sent).toHaveLength(1);
  expect(sent[0].method).toBe("POST");
  expect(sent[0].path).toBe("/me/api/apps/app-1/trusted-addresses");
  expect(sent[0].body).toEqual({ address: "10.0.0.8", note: "Hall speaker" });
  expect(sent[0].headers.get("x-azizam-user-action")).toBe("1");
});

it("removeTrustedAddress deletes one entry on the App with the same headers", async () => {
  reply = { status: 200, body: { trusted_address: { id: "tad-1" } } };
  await accountApi.removeTrustedAddress("app-1", "tad-1");
  expect(sent).toHaveLength(1);
  expect(sent[0].method).toBe("DELETE");
  expect(sent[0].path).toBe("/me/api/apps/app-1/trusted-addresses/tad-1");
  expect(sent[0].headers.get("x-azizam-csrf")).toBe("csrf-1");
});

it("keepKey sends the owner's confirmation only when the owner has confirmed the administrators", async () => {
  reply.body = { credential: { id: "key-1" } };
  await accountApi.keepKey("key-1", { confirmed: true });
  await accountApi.keepKey("key-2");
  expect(sent.map((request) => request.body)).toEqual([{ confirm_admins_could_have_made_it: true }, undefined]);
});

it("keepTrustedAddress sends the owner's confirmation in the same body", async () => {
  await accountApi.keepTrustedAddress("app-1", "tad-1", { confirmed: true });
  expect(sent[0].path).toBe("/me/api/apps/app-1/trusted-addresses/tad-1/adopt");
  expect(sent[0].body).toEqual({ confirm_admins_could_have_made_it: true });
});

it("administratorsToConfirm returns the administrators a keep asks the owner to confirm, and nothing for other refusals", async () => {
  reply = {
    status: 409,
    body: {
      error: {
        code: "keep_needs_confirmation",
        message: "Confirm them.",
        administrators: { people: ["Simon Agent"], unnamed_people: 1, admin_keys: 23 },
      },
    },
  };
  const asked = await accountApi.keepKey("key-1").catch((error: unknown) => error);
  expect(administratorsToConfirm(asked)).toEqual({ people: ["Simon Agent"], unnamed_people: 1, admin_keys: 23 });

  reply = { status: 409, body: { error: { code: "keep_needs_confirmation", message: "Confirm them." } } };
  const withoutNames = await accountApi.keepKey("key-1").catch((error: unknown) => error);
  expect(administratorsToConfirm(withoutNames)).toEqual({ people: [], unnamed_people: 0, admin_keys: 0 });

  reply = { status: 409, body: { error: { code: "keep_not_provable", message: "This key cannot be kept." } } };
  const refused = await accountApi.keepKey("key-1").catch((error: unknown) => error);
  expect(administratorsToConfirm(refused)).toBeNull();
  expect(administratorsToConfirm(new Error("offline"))).toBeNull();
});

it("administratorsSentence names the people, then counts the people with no name and the administrator keys", () => {
  const cases: [AdministratorsOnRecord, string][] = [
    [{ people: ["Simon Agent"], unnamed_people: 1, admin_keys: 23 }, "Simon Agent, 1 person with no name, and 23 administrator keys"],
    [{ people: [], unnamed_people: 2, admin_keys: 0 }, "2 people with no name"],
    [{ people: [], unnamed_people: 0, admin_keys: 1 }, "1 administrator key"],
    [{ people: ["Ann", "Bob"], unnamed_people: 0, admin_keys: 0 }, "Ann and Bob"],
    [{ people: ["Ann"], unnamed_people: 1, admin_keys: 0 }, "Ann and 1 person with no name"],
    [{ people: ["Ann", "Bob"], unnamed_people: 0, admin_keys: 2 }, "Ann, Bob, and 2 administrator keys"],
    [{ people: ["Ann"], unnamed_people: 0, admin_keys: 1 }, "Ann and 1 administrator key"],
    [{ people: [], unnamed_people: 0, admin_keys: 0 }, ""],
  ];
  for (const [administrators, sentence] of cases) {
    expect(administratorsSentence(administrators)).toBe(sentence);
  }
});

it("hasAdministrators is true when the gateway names anyone the owner must confirm", () => {
  expect(hasAdministrators({ people: ["Ann"], unnamed_people: 0, admin_keys: 0 })).toBe(true);
  expect(hasAdministrators({ people: [], unnamed_people: 0, admin_keys: 1 })).toBe(true);
  expect(hasAdministrators({ people: [], unnamed_people: 0, admin_keys: 0 })).toBe(false);
  expect(hasAdministrators(undefined)).toBe(false);
});

it("readdTrustedAddress posts to the entry's readd route with the same headers, and returns the new entry and the one it replaced", async () => {
  reply = { status: 201, body: { trusted_address: { id: "tad-3" }, replaced_entry_id: "tad-1" } };
  const readded = await accountApi.readdTrustedAddress("app-1", "tad-1");
  expect(readded).toEqual({ trusted_address: { id: "tad-3" }, replaced_entry_id: "tad-1" });
  expect(sent).toHaveLength(1);
  expect(sent[0].method).toBe("POST");
  expect(sent[0].path).toBe("/me/api/apps/app-1/trusted-addresses/tad-1/readd");
  expect(sent[0].headers.get("x-azizam-user-action")).toBe("1");
  expect(sent[0].headers.get("x-azizam-csrf")).toBe("csrf-1");
});
