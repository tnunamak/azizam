import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, ApiError, type ServiceRegistry, type StatusPayload } from "@/lib/api";
import Page from "./page";

// Moved from e2e/status-engines-services.spec.ts: seeded, empty and error
// pictures of the Engines page, and the add / edit / toggle / remove request
// bodies, with the response shapes the browser spec intercepted.
const search = vi.hoisted(() => ({ value: "" }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(search.value),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ admin: true }),
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
  toast.success.mockClear();
  toast.error.mockClear();
});

const registry = {
  schema_version: 1,
  services: {
    chat: {
      id: "chat",
      name: "Local chat engine",
      kind: "llama",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["chat"],
      role: "primary",
      config: {
        host: "127.0.0.1",
        port: 18081,
        base_path: "/v1",
        protocol: "openai-compatible",
        health_endpoint: "/health",
        custom_flag: "keep-me",
        models: { "existing-chat": { upstream_model: "existing-upstream" } },
      },
    },
    image: {
      id: "image",
      name: "Local image engine",
      kind: "comfy",
      source: "ui",
      enabled: false,
      editable: true,
      capabilities: ["image", "video"],
      role: "optional",
      config: { base_url: "http://127.0.0.1:18082" },
    },
    managed: {
      id: "managed",
      name: "Gateway substrate",
      kind: "litellm",
      source: "builtin",
      enabled: true,
      editable: false,
      capabilities: [],
      role: "required",
      config: {
        base_url: "http://litellm:4000",
        protocol: "openai-compatible",
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

const presets = {
  presets: [
    {
      id: "openai-compatible",
      label: "OpenAI compatible",
      base_path: "/v1",
      default_port: null,
      protocol: "openai-compatible",
      capabilities: ["chat"],
      native_passthrough: false,
    },
  ],
};

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
  vi.spyOn(api.services, "presets").mockResolvedValue(presets);
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

/** Open the edit sheet through the row's name button. */
async function openEdit(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(
    within(engineRow(name)).getAllByRole("button", { name: new RegExp(name) })[0],
  );
  return screen.findByRole("dialog", { name: "Edit engine" });
}

it("lists the in-use engine without its model names, hides unused engines, and opens an edit sheet with test and detect", async () => {
  const user = userEvent.setup();
  mount();
  await screen.findByRole("heading", { name: "Engines", level: 1 });
  const chat = await screen.findByRole("row", { name: /Local chat engine/ });
  expect(within(chat).queryByText("existing-chat")).toBeNull();
  expect(screen.queryByText("Local image engine")).toBeNull();
  await user.click(screen.getByRole("switch", { name: "Show not in use" }));
  expect(screen.getByText("Local image engine")).toBeVisible();

  vi.spyOn(api.services, "testConnection").mockResolvedValue({
    ok: true,
    status: 200,
    error: "",
    url: "http://127.0.0.1:18081/v1/health",
  });
  vi.spyOn(api.services, "detectModels").mockResolvedValue({
    ok: true,
    models: ["demo-chat"],
    error: "",
  });
  const sheet = await openEdit(user, "Local chat engine");
  await user.click(within(sheet).getByText("Connection and models", { exact: true }));
  expect(within(sheet).getByText("existing-chat", { exact: true })).toBeVisible();
  await user.click(within(sheet).getByRole("button", { name: "Test" }));
  expect(await within(sheet).findByText(/connection passed/)).toBeVisible();
  await user.click(within(sheet).getByRole("button", { name: "Detect" }));
  expect(
    within(within(sheet).getByRole("group", { name: "Model 1" })).getByRole("textbox", {
      name: "Model name",
    }),
  ).toHaveValue("demo-chat");
});

it("says No engines configured when the registry is empty", async () => {
  mount({
    registry: { schema_version: 1, services: {} },
    status: { backends: [] } as unknown as StatusPayload,
  });
  expect(await screen.findByText("No engines configured")).toBeVisible();
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

it("selects a not-in-use engine named in the query string and opens it with its switch off", async () => {
  search.value = "service=image";
  const user = userEvent.setup();
  mount();
  expect(await screen.findByText("Local image engine")).toBeVisible();
  const selected = document.querySelector("tr[aria-current='true']") as HTMLElement;
  expect(within(selected).getByText("Local image engine")).toBeVisible();
  await user.click(within(selected).getAllByRole("button", { name: /Local image engine/ })[0]);
  const sheet = await screen.findByRole("dialog", { name: "Edit engine" });
  expect(within(sheet).getByRole("switch", { name: "Toggle Local image engine" })).not.toBeChecked();
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

it("adds, edits, toggles and removes engines through the existing APIs with the documented bodies", async () => {
  const user = userEvent.setup();
  mount();
  // Only the network is faked: the production API client serializes every body.
  const writes: { method: string; path: string; body: Record<string, unknown> | undefined; ifMatch: string | null }[] = [];
  const registryReply = () =>
    new Response(JSON.stringify({ service_registry: registry }), {
      headers: { "content-type": "application/json", etag: "test-etag" },
    });
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
      if (method !== "GET") {
        writes.push({
          method,
          path,
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          ifMatch: new Headers(init?.headers).get("if-match"),
        });
      }
      return registryReply();
    }),
  );
  const sent = (method: string) => writes.filter((write) => write.method === method);
  try {

  await screen.findByRole("row", { name: /Local chat engine/ });
  await user.click(screen.getAllByRole("button", { name: "Add engine" })[0]);
  const addDialog = await screen.findByRole("dialog", { name: "Add engine" });
  await user.click(within(addDialog).getByRole("combobox", { name: "Preset" }));
  await user.click(await screen.findByRole("option", { name: /OpenAI.?compatible/i }));
  await user.click(within(addDialog).getByLabelText(/^Name/));
  await user.paste("Added engine");
  await user.click(within(addDialog).getByLabelText(/^Base URL/));
  await user.paste("http://127.0.0.1:18083/v1");
  await user.click(within(addDialog).getByRole("button", { name: "Add engine" }));
  await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Engine added"));
  expect(sent("POST")).toHaveLength(1);
  expect(sent("POST")[0]).toMatchObject({
    path: "/admin/api/operator/services",
    ifMatch: "test-etag",
    body: { name: "Added engine", preset: "openai-compatible", base_url: "http://127.0.0.1:18083/v1" },
  });

  const dialog = await openEdit(user, "Local chat engine");
  const name = within(dialog).getByLabelText(/^Name/);
  await user.clear(name);
  await user.click(name);
  await user.paste("Renamed chat engine");
  const endpoint = within(dialog).getByLabelText("Endpoint");
  await user.clear(endpoint);
  await user.click(endpoint);
  await user.paste("https://example.test/v1");
  await user.click(within(dialog).getByRole("button", { name: "Save engine" }));
  expect(await within(dialog).findByText(/HTTPS endpoints are not supported/)).toBeVisible();
  await user.click(within(dialog).getByText("Advanced fields", { exact: true }));
  const json = within(dialog).getByLabelText("Advanced JSON", { exact: true }) as HTMLTextAreaElement;
  const config = json.value;
  await user.clear(json);
  await user.click(json);
  await user.paste("null");
  await user.click(within(dialog).getByRole("button", { name: "Save engine" }));
  expect(await within(dialog).findByText("Advanced fields must be a JSON object.")).toBeVisible();
  expect(sent("PATCH")).toHaveLength(0);
  // user.type reads "{" as a key chord, so paste the original JSON back.
  await user.clear(json);
  await user.click(json);
  await user.paste(config);
  await user.clear(endpoint);
  await user.click(endpoint);
  await user.paste("http://127.0.0.1:19000/v1");
  await user.click(within(dialog).getByRole("button", { name: "Save engine" }));
  await waitFor(() => expect(sent("PATCH")).toHaveLength(1));
  expect(sent("PATCH")[0].path).toBe("/admin/api/operator/services/chat");
  expect(sent("PATCH")[0].body).toMatchObject({
    name: "Renamed chat engine",
    host: "127.0.0.1",
    port: 19000,
    base_path: "/v1",
    custom_flag: "keep-me",
    enabled: true,
  });
  expect(sent("PATCH")[0].body).not.toHaveProperty("base_url");
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit engine" })).toBeNull());

  await user.click(screen.getByText("Show infrastructure", { exact: true }));
  const managed = await openEdit(user, "Gateway substrate");
  const mname = within(managed).getByLabelText(/^Name/);
  await user.clear(mname);
  await user.click(mname);
  await user.paste("Renamed substrate");
  const note = within(managed).getByLabelText("Note");
  await user.clear(note);
  await user.click(note);
  await user.paste("Managed note changed.");
  expect(within(managed).queryByLabelText("Endpoint")).toBeNull();
  await user.click(within(managed).getByRole("button", { name: "Save engine" }));
  await waitFor(() => expect(sent("PATCH")).toHaveLength(2));
  expect(sent("PATCH")[1]).toMatchObject({
    path: "/admin/api/operator/services/managed",
    body: { name: "Renamed substrate", note: "Managed note changed.", enabled: true },
  });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit engine" })).toBeNull());

  const toggle = await openEdit(user, "Local chat engine");
  await user.click(within(toggle).getByRole("switch", { name: "Toggle Local chat engine" }));
  expect(sent("PATCH")).toHaveLength(2);
  await user.click(within(toggle).getByRole("button", { name: "Save engine" }));
  await waitFor(() => expect(sent("PATCH")).toHaveLength(3));
  expect(sent("PATCH")[2]).toMatchObject({ path: "/admin/api/operator/services/chat", body: { enabled: false } });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit engine" })).toBeNull());

  await user.click(screen.getByRole("switch", { name: "Show not in use" }));
  await user.click(
    within(engineRow("Local image engine")).getByRole("button", { name: "Actions for Local image engine" }),
  );
  await user.click(await screen.findByRole("menuitem", { name: "Remove engine" }));
  const confirm = await screen.findByRole("dialog", { name: "Remove engine?" });
  await user.click(within(confirm).getByRole("textbox"));
  await user.paste("Local image engine");
  await user.click(within(confirm).getByRole("button", { name: "Remove engine" }));
  await waitFor(() => expect(sent("DELETE")).toHaveLength(1));
  expect(sent("DELETE")[0]).toMatchObject({ path: "/admin/api/operator/services/image", ifMatch: "test-etag" });
  await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Engine removed"));
  } finally {
    vi.unstubAllGlobals();
  }
});
