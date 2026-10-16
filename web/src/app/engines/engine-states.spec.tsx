import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, ApiError, type ServiceRegistry, type StatusPayload } from "@/lib/api";
import { enginesApi } from "@/lib/engines-api";
import { definitions, engineView } from "./__fixtures__/test-utils";
import Page from "./page";

// Moved from e2e/status-engines-services.spec.ts: seeded, empty and error
// pictures of the Engines page, and the add / edit / toggle / remove request
// bodies, with the response shapes the browser spec intercepted.
const search = vi.hoisted(() => ({ value: "" }));
const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(search.value),
  useRouter: () => router,
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ admin: true, role: "owner" }),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("@/lib/toast", () => ({ toast }));
// Radix Select needs these pointer methods, which jsdom lacks.
for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"]) {
  Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value: () => false });
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  search.value = "";
  router.push.mockReset();
  toast.success.mockClear();
  toast.error.mockClear();
});

const registry = {
  schema_version: 1,
  services: {
    chat: {
      id: "chat",
      name: "Local chat engine",
      kind: "engine-a",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["chat"],
      role: "primary",
      config: {
        host: "engine-a.test",
        base_path: "/v1",
        protocol: "protocol-a",
        health_endpoint: "/health",
        custom_flag: "keep-me",
        models: { "existing-chat": { upstream_model: "existing-upstream" } },
      },
    },
    image: {
      id: "image",
      name: "Local image engine",
      kind: "engine-b",
      source: "ui",
      enabled: false,
      editable: true,
      capabilities: ["image", "video"],
      role: "optional",
      config: { base_url: "http://engine-b.test" },
    },
    managed: {
      id: "managed",
      name: "Gateway substrate",
      kind: "engine-c",
      source: "builtin",
      enabled: true,
      editable: false,
      capabilities: [],
      role: "required",
      config: {
        base_url: "http://engine-c.test",
        protocol: "protocol-a",
        note: "Managed by the gateway.",
      },
    },
  },
} as unknown as ServiceRegistry;

const observed = (
  id: string,
  name: string,
  enabled: boolean,
  observation: string,
  tone: string,
  role = "primary",
) => ({
  service_id: id,
  name,
  configured: true,
  enabled,
  role,
  routable: enabled ? true : null,
  observation,
  tone,
  last_error: null,
});
const status = {
  current_health: {
    state: "ok",
    tone: "ok",
    label: "Ready",
    detail: "Configured engines are reachable.",
    observation_available: true,
    services: [
      observed("chat", "Local chat engine", true, "up", "ok"),
      observed("managed", "Gateway substrate", true, "up", "ok", "required"),
      observed("image", "Local image engine", false, "disabled", "neutral", "optional"),
    ],
  },
  backends: [{ service_id: "chat", live_status: "up", latency_ms: 42 }],
} as unknown as StatusPayload;

function mount(
  options: {
    registry?: ServiceRegistry | Error;
    status?: StatusPayload | Error;
  } = {},
) {
  const reg = options.registry ?? registry;
  const st = options.status ?? status;
  const regSpy = vi.spyOn(api.services, "registry");
  if (reg instanceof Error) regSpy.mockRejectedValue(reg);
  else regSpy.mockResolvedValue(reg);
  const stSpy = vi.spyOn(api, "status");
  if (st instanceof Error) stSpy.mockRejectedValue(st);
  else stSpy.mockResolvedValue(st);
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
    >
      <Page />
    </QueryClientProvider>,
  );
}
const gatewayError = (code: string, message: string) =>
  new ApiError(code, message, 503);

const engineRow = (name: string) =>
  screen.getAllByRole("row").find((r) => r.textContent?.includes(name))!;

/** Open an engine through the row's name button. */
async function openRow(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(within(engineRow(name)).getAllByRole("button", { name: new RegExp(name) })[0]);
}

/**
 * Put a value in a field with one input event. The test checks the bodies the
 * form sends, not key handling; typing ~150 keys re-rendered the sheet on each
 * one and made this the slowest spec in the suite (it timed out under gate load).
 * Paste also takes "{" literally, where user.type reads it as a key chord.
 */
async function enter(user: ReturnType<typeof userEvent.setup>, field: HTMLElement, text: string) {
  await user.clear(field);
  await user.paste(text);
}

it("lists the in-use engine without its model names, hides unused engines, and opens a row as its record page", async () => {
  const user = userEvent.setup();
  mount();
  await screen.findByRole("heading", { name: "Engines", level: 1 });
  const chat = await screen.findByRole("row", { name: /Local chat engine/ });
  expect(within(chat).queryByText("existing-chat")).toBeNull();
  expect(screen.queryByText("Local image engine")).toBeNull();
  await user.click(screen.getByRole("switch", { name: "Show not in use" }));
  expect(screen.getByText("Local image engine")).toBeVisible();
  await openRow(user, "Local chat engine");
  expect(router.push).toHaveBeenCalledWith("/engines/?engine=chat");
});

it("explains an empty engine list and opens the wizard from its Add engine action", async () => {
  const user = userEvent.setup();
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
  mount({
    registry: { schema_version: 1, services: {} },
    status: { backends: [] } as unknown as StatusPayload,
  });
  const heading = await screen.findByText("No engines configured");
  expect(heading).toBeVisible();
  expect(screen.getByText("An engine is a server that runs AI models for Azizam.")).toBeVisible();
  // Scope to the empty list so the header's separate Add engine cannot mask a broken CTA.
  const emptyList = heading.closest("section")!;
  const add = within(emptyList).getByRole("button", { name: "Add engine" });
  expect(add).toBeVisible();
  expect(add).toBeEnabled();
  await user.click(add);
  const dialog = await screen.findByRole("dialog", { name: "Add engine" });
  expect(await within(dialog).findByRole("heading", { name: "What kind of server is it?" })).toBeVisible();
  expect(within(dialog).getByText("Step 1 of 5 · Kind")).toBeVisible();
});

it("reports an unloadable engine list as unknown and keeps the gateway's words under Technical details", async () => {
  const user = userEvent.setup();
  mount({ registry: gatewayError("offline", "Registry offline") });
  expect(await screen.findByText("Engine list could not be loaded")).toBeVisible();
  expect(
    screen.getByText("Each engine's state is unknown. Try loading the engine list again."),
  ).toBeVisible();
  await user.click(screen.getByText("Technical details"));
  expect(screen.getByText("Registry offline")).toBeVisible();
});

const unobserved = {
  ...status,
  current_health: {
    ...status.current_health,
    state: "unavailable",
    tone: "neutral",
    label: "Live status unavailable",
    detail: "The gateway could not observe engines.",
    observation_available: false,
  },
  backends: [{ service_id: "chat", live_status: "up" }],
} as unknown as StatusPayload;

it("does not infer a green state when observations are unavailable", async () => {
  mount({ status: unobserved });
  await screen.findByRole("row", { name: /Local chat engine/ });
  expect(screen.queryByText("Not measured yet")).toBeNull();
  expect(screen.queryByText("Ready")).toBeNull();
});

it("clears the live projection when the status refresh fails", async () => {
  const user = userEvent.setup();
  mount({ status: gatewayError("status_down", "Status refresh failed") });
  expect(await screen.findByText("Live status unavailable")).toBeVisible();
  expect(
    screen.getByText("Engine states are unknown. Try loading live status again."),
  ).toBeVisible();
  await user.click(screen.getByText("Technical details"));
  expect(screen.getByText("Status refresh failed")).toBeVisible();
  expect(screen.queryByText("Not measured yet")).toBeNull();
  expect(screen.queryByText("Ready")).toBeNull();
});

it("selects a not-in-use engine named in the query string and opens it", async () => {
  search.value = "service=image";
  const user = userEvent.setup();
  mount();
  expect(await screen.findByText("Local image engine")).toBeVisible();
  const selected = document.querySelector("tr[aria-current='true']") as HTMLElement;
  expect(within(selected).getByText("Local image engine")).toBeVisible();
  await user.click(within(selected).getAllByRole("button", { name: /Local image engine/ })[0]);
  expect(router.push).toHaveBeenCalledWith("/engines/?engine=image");
});

it("says when a kept-off engine is checked, or that it waits for the owner's key", async () => {
  const user = userEvent.setup();
  mount({
    registry: {
      ...registry,
      services: {
        ...registry.services,
        image: { ...registry.services.image, config: { engine_check: { status: "not_started" } } },
        locked: {
          ...registry.services.image,
          id: "locked",
          name: "Locked server",
          config: { engine_check: { status: "not_started", reason: "needkey" } },
        },
      },
    } as ServiceRegistry,
  });
  await screen.findByRole("row", { name: /Local chat engine/ });
  await user.click(screen.getByRole("switch", { name: "Show not in use" }));
  expect(within(engineRow("Local image engine")).getByText("Not checked yet; checked when you turn it on or press Check now")).toBeVisible();
  expect(within(engineRow("Locked server")).getByText("Waiting for the owner to add a key")).toBeVisible();
});

it("opens the record page from ?engine=, goes back, and removes the engine there", async () => {
  search.value = "engine=chat";
  const user = userEvent.setup();
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
  vi.spyOn(enginesApi, "view").mockResolvedValue(engineView({ service_id: "chat", name: "Local chat engine" }));
  const remove = vi.spyOn(api.services, "remove").mockResolvedValue({ schema_version: 1, services: {} });
  mount();
  expect(await screen.findByRole("heading", { level: 1, name: "Local chat engine" })).toBeVisible();
  expect(screen.queryByRole("table", { name: "Engines" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Engines" }));
  expect(router.push).toHaveBeenCalledWith("/engines/");
  await user.click(screen.getByRole("button", { name: "Remove engine" }));
  const confirm = await screen.findByRole("dialog", { name: "Remove engine?" });
  await enter(user, within(confirm).getByRole("textbox"), "Local chat engine");
  await user.click(within(confirm).getByRole("button", { name: "Remove engine" }));
  await waitFor(() => expect(remove).toHaveBeenCalledWith("chat"));
  await waitFor(() => expect(router.push).toHaveBeenLastCalledWith("/engines/"));
});

it("labels degraded and standby engines as such, never Busy or Starting", async () => {
  mount({
    registry: {
      ...registry,
      services: {
        ...registry.services,
        image: { ...registry.services.image, enabled: true },
      },
    } as ServiceRegistry,
    status: {
      current_health: {
        state: "warn",
        tone: "warn",
        label: "Engines need attention",
        detail: "One engine is degraded and one is stopped on standby.",
        observation_available: true,
        services: [
          observed("chat", "Local chat engine", true, "degraded", "warn"),
          observed("image", "Local image engine", true, "standby_stopped", "neutral", "optional"),
        ],
      },
      backends: [],
    } as unknown as StatusPayload,
  });
  expect(await screen.findByText("Degraded", { exact: true })).toBeVisible();
  expect(screen.getByText("Standby, stopped", { exact: true })).toBeVisible();
  expect(screen.queryByText("Busy")).toBeNull();
  expect(screen.queryByText("Starting")).toBeNull();
});

it("removes an engine through the existing API with the documented request", async () => {
  const user = userEvent.setup();
  mount();
  // Only the network is faked: the production API client serializes every body.
  const writes: { method: string; path: string; ifMatch: string | null }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === "/admin/api/session") {
        return new Response(JSON.stringify({ actor: { csrf_token: "csrf-fixture" } }), {
          headers: { "content-type": "application/json" },
        });
      }
      if (method !== "GET") writes.push({ method, path, ifMatch: new Headers(init?.headers).get("if-match") });
      return new Response(JSON.stringify({ service_registry: registry }), {
        headers: { "content-type": "application/json", etag: "test-etag" },
      });
    }),
  );
  try {
    await screen.findByRole("row", { name: /Local chat engine/ });
    await user.click(screen.getByRole("switch", { name: "Show not in use" }));
    await user.click(within(engineRow("Local image engine")).getByRole("button", { name: "Actions for Local image engine" }));
    await user.click(await screen.findByRole("menuitem", { name: "Remove engine" }));
    const confirm = await screen.findByRole("dialog", { name: "Remove engine?" });
    await enter(user, within(confirm).getByRole("textbox"), "Local image engine");
    await user.click(within(confirm).getByRole("button", { name: "Remove engine" }));
    await waitFor(() => expect(writes.filter((w) => w.method === "DELETE")).toHaveLength(1));
    expect(writes[0]).toMatchObject({ method: "DELETE", path: "/admin/api/operator/services/image", ifMatch: "test-etag" });
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Engine removed"));
  } finally {
    vi.unstubAllGlobals();
  }
});
