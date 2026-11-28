// Proof that a stub of an endpoint whose structure comes from the oracle gateway
// (fixtures/oracle.ts) is checked against what that gateway really returned:
// the right slice passes, and a missing required key or an invented key fails.
// A renamed gateway field is proved by a production mutant, not here (see
// lanes/g2fin-REPORT.md): the oracle then returns the new name and the required
// key the registry lists is gone from the real body.
import type { Route } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

// A Route that records what it was asked to answer; fulfilJson needs only the
// request's method and URL, and fulfill().
function routeFor(method: string, url: string) {
  const answered: unknown[] = [];
  const route = {
    request: () => ({ url: () => url, method: () => method }),
    fulfill: async (options: unknown) => void answered.push(options),
  } as unknown as Route;
  return { route, answered };
}

type Case = {
  name: string;
  method: string;
  path: string;
  // A stub that carries the keys the product reads, taken from the specs.
  good: Record<string, unknown>;
  // One key the registry lists as required for this endpoint.
  required: string;
};

const cases: Case[] = [
  {
    name: "invite created",
    method: "POST",
    path: "/admin/api/invites",
    good: { invite: { id: "link_1" }, url: "/invite/#token=fixture" },
    required: "url",
  },
  {
    name: "media job state",
    method: "GET",
    path: "/gateway/v1/media/jobs/mj_fixture",
    good: { object: "media.job", id: "mj_fixture", status: "running" },
    required: "status",
  },
  {
    name: "invite link copied again",
    method: "GET",
    path: "/admin/api/invites/link_fixture/link",
    good: { url: "/invite/#token=fixture" },
    required: "url",
  },
  {
    name: "person changed",
    method: "PATCH",
    path: "/admin/api/people/usr_fixture",
    good: { ok: true },
    required: "ok",
  },
  {
    name: "sign in",
    method: "POST",
    path: "/auth/api/login",
    good: { next: "/" },
    required: "next",
  },
];

for (const item of cases) {
  test.describe(`stub of ${item.name} (${item.method} ${item.path})`, () => {
    const attempt = (body: Record<string, unknown>) => {
      const { route, answered } = routeFor(item.method, `http://127.0.0.1${item.path}`);
      return { run: () => fulfilJson(route, body), answered };
    };

    test("the keys the product reads pass and are answered", async () => {
      const { run, answered } = attempt(item.good);
      await run();
      expect(answered).toHaveLength(1);
    });

    test("a missing required key fails", async () => {
      const without = { ...item.good };
      delete without[item.required];
      const { run, answered } = attempt(without);
      await expect(run()).rejects.toThrow(`/${item.required}: required key missing`);
      expect(answered).toHaveLength(0);
    });

    test("a key the gateway never sends fails", async () => {
      const { run, answered } = attempt({ ...item.good, zz_invented_by_the_stub: 1 });
      await expect(run()).rejects.toThrow("/zz_invented_by_the_stub: key the real response never has");
      expect(answered).toHaveLength(0);
    });

    test("another type fails", async () => {
      const { run } = attempt({ ...item.good, [item.required]: 7 });
      await expect(run()).rejects.toThrow(/expected \w+, got number/);
    });
  });
}
