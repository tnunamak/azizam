import { afterEach, expect, it, vi } from "vitest";
import { fetchViewer } from "@/components/shell/session";

afterEach(() => vi.unstubAllGlobals());

it("keeps email separate from a missing display name", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        authenticated: true,
        user: {
          name: "owner@example.test",
          email: "owner@example.test",
          role: "owner",
        },
      }),
    }),
  );
  expect(await fetchViewer()).toMatchObject({
    name: "Your account",
    email: "owner@example.test",
    admin: true,
  });
});

it("preserves the person's display name", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        authenticated: true,
        user: {
          name: "  River Jones  ",
          email: "river@example.test",
          role: "member",
        },
      }),
    }),
  );
  expect(await fetchViewer()).toMatchObject({
    name: "River Jones",
    admin: false,
  });
});

it("uses a neutral name for legacy owner sessions", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ authenticated: false }),
      })
      .mockResolvedValueOnce({
        ok: true,
        headers: new Headers(),
        json: async () => ({
          actor: {
            email: "owner@example.test",
            roles: ["owner"],
            csrf_token: "test",
          },
        }),
      }),
  );
  expect(await fetchViewer()).toMatchObject({
    name: "Gateway owner",
    email: "owner@example.test",
    admin: true,
  });
});
