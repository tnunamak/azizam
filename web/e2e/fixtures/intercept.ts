// The only place a browser test answers a request itself (scripts/ci/policy.py
// counts `.fulfill(` anywhere else). A JSON stub goes through `fulfilJson`,
// which names the endpoint it stands in for (intercept-registry.ts), asks the
// real gateway of the same fixture server what that endpoint returns, and
// fails the test when the stub's structure differs: a key the server never
// sends, a key the server always sends but the stub left out, or another type.
import { request, test, type APIRequestContext, type APIResponse, type Route } from "@playwright/test";
import { appendFileSync } from "node:fs";
import { absentPaths, check, derive, merge, type Shape } from "./intercept-shape";
import { REGISTRY, errorSamples, lookup, type Endpoint } from "./intercept-registry";

interface Options {
  status?: number;
  headers?: Record<string, string>;
  // The real response fetched with route.fetch(); its headers and status carry over.
  response?: APIResponse;
  // For a stub that is deliberately NOT what the server sends (a resilience
  // test of a malformed response). The reason is mandatory and counted by
  // scripts/ci/policy.py; a stub that only drifted is fixed, not marked.
  malformed?: string;
}

// Discovery (INTERCEPT_DISCOVER=file): log each stub's key paths and each probe's, for
// choosing an endpoint's `required` list. The check itself is skipped.
function keyPaths(value: unknown, at = "", into = new Set<string>()): Set<string> {
  if (Array.isArray(value)) value.forEach((item) => keyPaths(item, `${at}/[]`, into));
  else if (value && typeof value === "object")
    for (const [key, item] of Object.entries(value)) {
      into.add(`${at}/${key}`);
      keyPaths(item, `${at}/${key}`, into);
    }
  return into;
}

const probes = new Map<string, Promise<Shape | null>>();
const apis = new Map<string, Promise<APIRequestContext>>();

function api(origin: string): Promise<APIRequestContext> {
  let found = apis.get(origin);
  if (!found) {
    const storageState = test.info().project.use.storageState;
    found = request.newContext({
      baseURL: origin,
      ...(typeof storageState === "string" ? { storageState } : {}),
    });
    apis.set(origin, found);
  }
  return found;
}

async function realShape(origin: string, endpoint: Endpoint): Promise<Shape | null> {
  const key = `${origin} ${endpoint.id}`;
  let found = probes.get(key);
  if (!found) {
    found = (async () => {
      if (!endpoint.probe) return null;
      const body = await endpoint.probe(await api(origin));
      const options = { open: endpoint.open, required: endpoint.required };
      const shape = derive(body, options);
      const gone = absentPaths(shape, endpoint.required ?? []);
      if (gone.length)
        throw new Error(`fulfilJson: ${endpoint.id} lists required keys the real gateway no longer sends: ${gone.join(", ")}`);
      return shape;
    })();
    probes.set(key, found);
  }
  return found;
}

async function errorShape(origin: string): Promise<Shape | null> {
  const key = `${origin} error`;
  let found = probes.get(key);
  if (!found) {
    found = errorSamples(await api(origin)).then((bodies) =>
      bodies.map((body) => derive(body, { open: ["/error/details"] })).reduce(merge),
    );
    probes.set(key, found);
  }
  return found;
}

export async function fulfilJson(route: Route, body: unknown, options: Options = {}) {
  const status = options.status ?? options.response?.status() ?? 200;
  const url = new URL(route.request().url());
  const method = route.request().method();
  if (!options.malformed) {
    const endpoint = lookup(method, url.pathname);
    const discovery = process.env.INTERCEPT_DISCOVER;
    if (discovery) {
      const real = endpoint?.probe && status < 400 ? await endpoint.probe(await api(url.origin)).catch(() => null) : null;
      appendFileSync(
        discovery,
        `${JSON.stringify({ method, path: url.pathname, status, id: endpoint?.id ?? null, stub: [...keyPaths(body)], real: real && [...keyPaths(real)] })}\n`,
      );
      return void (await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }));
    }
    if (!endpoint)
      throw new Error(`fulfilJson: ${method} ${url.pathname} is not in e2e/fixtures/intercept-registry.ts; add the endpoint with a real probe`);
    const shape =
      status >= 400 ? await errorShape(url.origin) : await realShape(url.origin, endpoint);
    if (!shape) {
      if (!endpoint.unprobed) throw new Error(`fulfilJson: ${endpoint.id} has no probe and no stated reason`);
    } else {
      const report = check(shape, body);
      if (report.problems.length)
        throw new Error(
          `fulfilJson: the stub for ${method} ${url.pathname} (${endpoint.id}, status ${status}) differs from the real gateway:\n  ${report.problems.join("\n  ")}`,
        );
    }
  }
  await route.fulfill({
    ...(options.response ? { response: options.response } : { status }),
    contentType: "application/json",
    ...(options.headers ? { headers: options.headers } : {}),
    body: JSON.stringify(body),
  });
}

/** A stub that is not JSON from the gateway: media bytes, text, an SVG. */
export async function fulfilBytes(
  route: Route,
  options: { status?: number; contentType?: string; headers?: Record<string, string>; body?: string | Buffer; path?: string },
) {
  await route.fulfill(options);
}

export { REGISTRY };
