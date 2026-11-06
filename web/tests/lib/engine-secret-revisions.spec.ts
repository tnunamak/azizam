import { beforeEach, describe, expect, it, vi } from "vitest";

const registry = { schema_version: 1, services: {} };
const response = (payload: unknown, etag?: string, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: new Headers(etag ? { etag } : {}),
  json: async () => payload,
});

beforeEach(() => vi.resetModules());

describe("secret command registry revisions", () => {
  it("sends the view revision and uses the returned revision for the next record write", async () => {
    const fetch = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/admin/api/session") return response({ actor: { csrf_token: "test-csrf" } });
      if (!init?.method || init.method === "GET") return response({ service_registry: registry }, '"os-3"');
      return response({ service_registry: registry }, path.endsWith("secret-entries") ? '"os-4"' : '"os-5"');
    });
    vi.stubGlobal("fetch", fetch);
    const { enginesApi } = await import("@/lib/engines-api");
    await enginesApi.secretEntries("engine/one", [{ carrier: "config.headers", operation: "replace", entry_id: "ent_header", value: "new-header-value" }], '"os-3"');
    const secretCall = fetch.mock.calls.find(([path]) => path.endsWith("secret-entries"))!;
    expect(secretCall[0]).toBe("/admin/api/operator/services/engine%2Fone/secret-entries");
    const headers = new Headers(secretCall[1]?.headers);
    expect(headers.get("If-Match")).toBe('"os-3"');
    expect(headers.get("x-azizam-csrf")).toBe("test-csrf");
    expect(JSON.parse(secretCall[1]?.body as string)).toEqual({ commands: [{ carrier: "config.headers", operation: "replace", entry_id: "ent_header", value: "new-header-value" }] });
    await enginesApi.update("engine/one", { name: "Renamed" });
    const recordCall = fetch.mock.calls.find(([, init]) => init?.method === "PATCH")!;
    expect(new Headers(recordCall[1]?.headers).get("If-Match")).toBe('"os-4"');
    expect(fetch.mock.calls.filter(([path]) => path === "/admin/api/operator/service-registry")).toHaveLength(1);
  });

  it("surfaces 412 without replaying commands, then uses the refreshed revision only for an explicit retry", async () => {
    let writes = 0;
    const fetch = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/admin/api/session") return response({ actor: { csrf_token: "test-csrf" } });
      if (path.endsWith("secret-entries")) {
        writes += 1;
        return writes === 1
          ? response({ error: { code: "generation_mismatch", message: "The registry changed." } }, undefined, 412)
          : response({ service_registry: registry }, '"os-6"');
      }
      return response({ service_registry: registry }, '"os-5"');
    });
    vi.stubGlobal("fetch", fetch);
    const { enginesApi } = await import("@/lib/engines-api");
    const commands = [{ carrier: "config.headers", operation: "clear", entry_id: "ent_header" }];
    await expect(enginesApi.secretEntries("engine", commands, '"os-2"')).rejects.toMatchObject({ status: 412, code: "generation_mismatch" });
    expect(writes).toBe(1);
    expect(fetch.mock.calls.filter(([path]) => path === "/admin/api/operator/service-registry")).toHaveLength(2);
    await enginesApi.secretEntries("engine", commands);
    expect(writes).toBe(2);
    const writeCalls = fetch.mock.calls.filter(([path]) => path.endsWith("secret-entries"));
    expect(new Headers(writeCalls[0][1]?.headers).get("If-Match")).toBe('"os-2"');
    expect(new Headers(writeCalls[1][1]?.headers).get("If-Match")).toBe('"os-5"');
    expect(JSON.parse(writeCalls[1][1]?.body as string)).toEqual({ commands });
  });
});
