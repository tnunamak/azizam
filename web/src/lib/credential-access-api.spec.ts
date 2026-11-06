import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { accountApi } from "./account-api";

type Sent = {
  path: string;
  method: string;
  body: unknown;
  headers: Headers;
  credentials?: RequestCredentials;
};
let sent: Sent[];

beforeEach(() => {
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      sent.push({
        path: String(input),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: new Headers(init?.headers),
        credentials: init?.credentials,
      });
      return new Response(
        JSON.stringify({
          credential: {
            id: "credential/one",
            prefix: "opk_example",
            display_name: "Laptop",
            app_id: "app-one",
          },
          capabilities: [{ intent: "chat.generate", capability: "chat", model: "house-chat" }],
        }),
      );
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

it("reads policy access for exactly one credential using the signed-in session", async () => {
  const access = await accountApi.credentialAccess("credential/one");

  expect(access.credential.id).toBe("credential/one");
  expect(access.capabilities).toEqual([
    { intent: "chat.generate", capability: "chat", model: "house-chat" },
  ]);
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatchObject({
    path: "/me/api/credentials/credential%2Fone/access",
    method: "GET",
    body: undefined,
    credentials: "same-origin",
  });
  expect(sent[0].headers.has("authorization")).toBe(false);
  expect(sent[0].headers.has("x-azizam-admin-action")).toBe(false);
});
