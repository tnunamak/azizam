import { afterEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, ApiError, type Service, type ServiceRegistry } from "@/lib/api";
import Page from "./page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ admin: true }),
}));
vi.mock("./engine-checks", () => ({
  EngineChecks: ({ service }: { service: Service }) => (
    <button>Test {service.name}</button>
  ),
}));

const service = (id: string, role = "primary", enabled = true): Service => ({
  id,
  name: id,
  kind: "http",
  source: "ui",
  enabled,
  editable: true,
  capabilities: ["chat"],
  role,
  config: { host: "localhost", port: 18090 },
});
const registry: ServiceRegistry = {
  schema_version: 1,
  services: {
    primary: service("primary"),
    standby: service("standby", "standby"),
    disabled: service("disabled", "optional", false),
  },
};
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Page />
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("engine progressive disclosure", () => {
  it("starts compact, reveals fallback and disabled engines, and keeps edit and test actions", async () => {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry);
    vi.spyOn(api, "status").mockResolvedValue({});
    const presets = vi
      .spyOn(api.services, "presets")
      .mockResolvedValue({ presets: [] });
    const user = userEvent.setup();
    mount();
    const primary = await screen.findByRole("heading", { name: "primary" });
    expect(screen.getByRole("heading", { name: "Chat" })).toBeVisible();
    // Fallback engines are rows of the one Engines table, rendered only once opened.
    expect(
      screen.queryByRole("heading", { name: "standby" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "disabled" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Available presets")).not.toBeInTheDocument();
    expect(presets).not.toHaveBeenCalled();
    const row = primary.closest("tr")!;
    const rowButton = within(row).getByRole("button", {
      name: /^primary\b/,
    });
    expect(rowButton).toBeVisible();
    expect(screen.queryByRole("button", { name: "Test primary" })).toBeNull();
    await user.click(rowButton);
    const sheet = await screen.findByRole("dialog", { name: "Edit engine" });
    expect(
      within(sheet).getByRole("button", { name: "Test primary" }),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Edit engine" })).toBeNull();
    expect(rowButton).toHaveFocus();
    await user.click(screen.getByText("Standby and fallback engines (1)"));
    expect(screen.getByRole("heading", { name: "standby" })).toBeVisible();
    await user.click(screen.getByRole("switch", { name: "Show not in use" }));
    expect(screen.getByRole("heading", { name: "disabled" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add engine" }));
    expect(
      await screen.findByRole("dialog", { name: "Add engine" }),
    ).toBeVisible();
    expect(presets).toHaveBeenCalledTimes(1);
  });
});

it.each(["Cancel", "Escape"])(
  "stages Enabled and discards it on %s",
  async (dismiss) => {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry);
    vi.spyOn(api, "status").mockResolvedValue({});
    const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /^primary\b/ }));
    await user.click(screen.getByRole("switch", { name: "Toggle primary" }));
    expect(
      screen.getByRole("switch", { name: "Toggle primary" }),
    ).not.toBeChecked();
    expect(edit).not.toHaveBeenCalled();
    if (dismiss === "Escape") await user.keyboard("{Escape}");
    else await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: /^primary\b/ }));
    expect(
      screen.getByRole("switch", { name: "Toggle primary" }),
    ).toBeChecked();
  },
);
it("saves Enabled together with the other edit fields", async () => {
  vi.spyOn(api.services, "registry").mockResolvedValue(registry);
  vi.spyOn(api, "status").mockResolvedValue({});
  const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: /^primary\b/ }));
  await user.click(screen.getByRole("switch", { name: "Toggle primary" }));
  await user.click(screen.getByRole("button", { name: "Save engine" }));
  expect(edit).toHaveBeenCalledWith(
    "primary",
    expect.objectContaining({ enabled: false }),
  );
});
it.each([true, false])(
  "keeps degraded observations and backend fallback consistent (health=%s)",
  async (useHealth) => {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry);
    vi.spyOn(api, "status").mockResolvedValue({
      current_health: {
        state: "warn",
        tone: "warn",
        label: "",
        detail: "",
        observation_available: true,
        services: useHealth
          ? [
              {
                service_id: "primary",
                name: "primary",
                enabled: true,
                configured: true,
                role: "primary",
                routable: true,
                observation: "degraded",
                tone: "warn",
                last_error: null,
              },
            ]
          : [],
      },
      backends: [{ service_id: "primary", status: "degraded" }],
    });
    mount();
    expect(await screen.findByText("Degraded", { exact: true })).toBeVisible();
    expect(screen.queryByText("Unavailable", { exact: true })).toBeNull();
  },
);

it("submits the edit form by Enter with accessible field names", async () => {
  vi.spyOn(api.services, "registry").mockResolvedValue(registry);
  vi.spyOn(api, "status").mockResolvedValue({});
  const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: /^primary\b/ }));
  const name = screen.getByRole("textbox", { name: /^Name/ });
  expect(screen.getByRole("textbox", { name: "Endpoint" })).toBeVisible();
  await user.clear(name);
  await user.type(name, "renamed{Enter}");
  expect(edit).toHaveBeenCalledWith(
    "primary",
    expect.objectContaining({ name: "renamed" }),
  );
});

describe("engine dialog focus restoration", () => {
  const engines: ServiceRegistry = {
    schema_version: 1,
    services: {
      alpha: service("alpha"),
      bravo: service("bravo"),
      charlie: service("charlie"),
    },
  };

  async function openRemoval(
    user: ReturnType<typeof userEvent.setup>,
    id: string,
  ) {
    const actions = await screen.findByRole("button", {
      name: `Actions for ${id}`,
    });
    await user.click(actions);
    await user.click(screen.getByRole("menuitem", { name: "Remove engine" }));
    return actions;
  }

  it("returns focus to the row actions when removal is cancelled", async () => {
    vi.spyOn(api.services, "registry").mockResolvedValue(engines);
    vi.spyOn(api, "status").mockResolvedValue({});
    const remove = vi.spyOn(api.services, "remove");
    const user = userEvent.setup();
    mount();
    const actions = await openRemoval(user, "bravo");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(actions).toHaveFocus());
    expect(remove).not.toHaveBeenCalled();
  });

  it.each([
    ["bravo", ["alpha", "bravo", "charlie"], "Actions for charlie"],
    ["charlie", ["alpha", "bravo", "charlie"], "Actions for bravo"],
    ["alpha", ["alpha"], "Add engine"],
  ])(
    "focuses %s's surviving neighbour or Add engine after removal",
    async (id, ids, target) => {
      const initial = {
        ...engines,
        services: Object.fromEntries(ids.map((key) => [key, service(key)])),
      };
      const remaining = {
        ...initial,
        services: Object.fromEntries(
          Object.entries(initial.services).filter(([key]) => key !== id),
        ),
      };
      vi.spyOn(api.services, "registry").mockResolvedValue(initial);
      vi.spyOn(api, "status").mockResolvedValue({});
      const remove = vi
        .spyOn(api.services, "remove")
        .mockResolvedValue(remaining);
      const user = userEvent.setup();
      mount();
      const actions = await openRemoval(user, id);
      await user.type(screen.getByRole("textbox"), id);
      await user.click(screen.getByRole("button", { name: "Remove engine" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(remove).toHaveBeenCalledWith(id);
      expect(actions.isConnected).toBe(false);
      await waitFor(() =>
        expect(
          screen.getAllByRole("button", { name: target })[0],
        ).toHaveFocus(),
      );
    },
  );

  it.each([false, true])(
    "restores surviving focus after disabling an engine (last=%s)",
    async (last) => {
      const initial = last
        ? { ...engines, services: { bravo: service("bravo") } }
        : engines;
      const disabled = {
        ...initial,
        services: {
          ...initial.services,
          bravo: service("bravo", "primary", false),
        },
      };
      vi.spyOn(api.services, "registry").mockResolvedValue(initial);
      vi.spyOn(api, "status").mockResolvedValue({});
      const edit = vi.spyOn(api.services, "edit").mockResolvedValue(disabled);
      const user = userEvent.setup();
      mount();
      const invoker = await screen.findByRole("button", { name: /^bravo\b/ });
      await user.click(invoker);
      await user.click(screen.getByRole("switch", { name: "Toggle bravo" }));
      await user.click(screen.getByRole("button", { name: "Save engine" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(edit).toHaveBeenCalledWith(
        "bravo",
        expect.objectContaining({ enabled: false }),
      );
      expect(invoker.isConnected).toBe(false);
      await waitFor(() =>
        expect(
          screen.getAllByRole("button", {
            name: last ? "Add engine" : "Actions for charlie",
          })[0],
        ).toHaveFocus(),
      );
    },
  );
});

describe("add-engine address checks", () => {
  async function openAdd() {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry);
    vi.spyOn(api, "status").mockResolvedValue({});
    vi.spyOn(api.services, "presets").mockResolvedValue({
      presets: [{ id: "test-preset", label: "Test engine" }],
    } as Awaited<ReturnType<typeof api.services.presets>>);
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "Add engine" }));
    const address = await screen.findByRole("textbox", { name: /^Base URL/ });
    return { user, address };
  }

  it.each(["success", "failure"])(
    "ignores an old probe %s after editing and preserves the newer result",
    async (outcome) => {
      type Probe = Awaited<ReturnType<typeof api.services.testConnection>>;
      let resolveOld!: (probe: Probe) => void;
      let rejectOld!: (error: Error) => void;
      let resolveNew!: (probe: Probe) => void;
      const old = new Promise<Probe>((resolve, reject) => {
        resolveOld = resolve; rejectOld = reject;
      });
      const current = new Promise<Probe>((resolve) => { resolveNew = resolve; });
      const test = vi.spyOn(api.services, "testConnection")
        .mockReturnValueOnce(old).mockReturnValueOnce(current);
      const { user, address } = await openAdd();
      fireEvent.change(address, { target: { value: "http://first.test" } });
      await user.click(screen.getByRole("button", { name: "Test connection" }));
      expect(screen.getByRole("button", { name: "Checking this address…" })).toBeDisabled();
      fireEvent.change(address, { target: { value: "http://second.test" } });
      await user.click(screen.getByRole("button", { name: "Test connection" }));
      await act(async () => {
        if (outcome === "success") {
          resolveOld({ ok: true, status: 200, error: "", url: "http://first.test" });
        } else rejectOld(new Error("Old probe exception"));
      });
      expect(screen.getByRole("button", { name: "Checking this address…" })).toBeDisabled();
      expect(screen.queryByText("Connected to this address.")).toBeNull();
      expect(screen.queryByText("Old probe exception")).toBeNull();
      await act(async () => {
        resolveNew({ ok: true, status: 200, error: "", url: "http://second.test" });
      });
      expect(screen.getByText("Connected to this address.")).toBeVisible();
      expect(test).toHaveBeenNthCalledWith(2, { base_url: "http://second.test" });
      const details = screen.getByText("HTTP 200").closest("details")!;
      expect(details).not.toHaveAttribute("open");
      fireEvent.change(address, { target: { value: "http://third.test" } });
      expect(screen.queryByText("Connected to this address.")).toBeNull();
    },
  );

  it("clears probe state on close and ignores responses after reopening", async () => {
    type Probe = Awaited<ReturnType<typeof api.services.testConnection>>;
    let resolve!: (probe: Probe) => void;
    vi.spyOn(api.services, "testConnection").mockReturnValue(
      new Promise<Probe>((done) => { resolve = done; }),
    );
    const { user, address } = await openAdd();
    expect(screen.getByText("Choose the preset that matches your engine.")).toBeVisible();
    expect(screen.getByLabelText("Key environment variable")).toBeVisible();
    fireEvent.change(address, { target: { value: "http://first.test" } });
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Add engine" }));
    await act(async () => {
      resolve({ ok: true, status: 200, error: "", url: "http://first.test" });
    });
    expect(screen.queryByText("Connected to this address.")).toBeNull();
    expect(screen.getByRole("textbox", { name: /^Base URL/ })).toHaveValue("");
  });

  it("keeps raw connection failures in closed technical details", async () => {
    vi.spyOn(api.services, "testConnection").mockRejectedValue(
      new Error("InternalError HTTP 500"),
    );
    const { user, address } = await openAdd();
    fireEvent.change(address, { target: { value: "http://first.test" } });
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(
      "The connection check failed. Check the address and try again.",
    )).toBeVisible();
    expect(screen.getByText("InternalError HTTP 500").closest("details")).not.toHaveAttribute("open");
  });
});

it("uses Voice groups and leaves owner-authored capability names unchanged", async () => {
  vi.spyOn(api.services, "registry").mockResolvedValue({
    schema_version: 1,
    services: { voice: { ...service("voice"), name: "My Speech engine", capabilities: ["speech"] } },
  });
  vi.spyOn(api, "status").mockResolvedValue({});
  mount();
  const heading = await screen.findByRole("heading", { name: "My Speech engine" });
  expect(screen.getByRole("heading", { name: "Voice" })).toBeVisible();
  expect(within(heading.closest("tr")!).queryByText("Voice")).toBeNull();
  expect(within(heading.closest("tr")!).queryByText("Speech")).toBeNull();
});

it("keeps raw save failures under technical details", async () => {
  vi.spyOn(api.services, "registry").mockResolvedValue(registry);
  vi.spyOn(api, "status").mockResolvedValue({});
  vi.spyOn(api.services, "edit").mockRejectedValue(new ApiError("internal", "SchemaError HTTP 500", 500));
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: /^primary\b/ }));
  await user.click(screen.getByRole("button", { name: "Save engine" }));
  expect(await screen.findByText(
    "Engine settings could not be saved. We could not identify the cause. Try saving again.",
  )).toBeVisible();
  expect(screen.getByText("SchemaError HTTP 500").closest("details")).not.toHaveAttribute("open");
});

it("names a refused save and keeps the gateway's reason under technical details", async () => {
  vi.spyOn(api.services, "registry").mockResolvedValue(registry);
  vi.spyOn(api, "status").mockResolvedValue({});
  vi.spyOn(api.services, "edit").mockRejectedValue(new ApiError("invalid_service", "a name is required", 400));
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: /^primary\b/ }));
  await user.click(screen.getByRole("button", { name: "Save engine" }));
  expect(await screen.findByText(
    "The gateway did not accept these engine settings. Check the fields and save again.",
  )).toBeVisible();
  expect(screen.getByText("a name is required").closest("details")).not.toHaveAttribute("open");
});

it("shows each detected model once with its engine name in technical details", async () => {
  const { EngineChecks } = await vi.importActual<typeof import("./engine-checks")>("./engine-checks");
  vi.spyOn(api.services, "detectModels").mockResolvedValue({
    ok: true, models: ["first-model", "second-model"], error: "",
  } as Awaited<ReturnType<typeof api.services.detectModels>>);
  const user = userEvent.setup();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <EngineChecks service={service("primary")} endpoint="http://localhost:18090"
        onSaved={vi.fn()} onEdit={vi.fn()} />
    </QueryClientProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Detect" }));
  const first = await screen.findByRole("group", { name: "Model 1" });
  const second = screen.getByRole("group", { name: "Model 2" });
  expect(screen.getAllByRole("textbox", { name: "Model name" })).toHaveLength(2);
  expect(within(first).getByRole("textbox", { name: "Model name" })).toHaveValue("first-model");
  expect(within(second).getByRole("textbox", { name: "Model name" })).toHaveValue("second-model");
  expect(screen.queryByText(/^Detected models:/)).toBeNull();
  expect(screen.getByText("Engine model name: first-model")).not.toBeVisible();
  expect(screen.getByText("Engine model name: second-model")).not.toBeVisible();
  await user.click(within(first).getByText("Technical details"));
  expect(screen.getByText("Engine model name: first-model")).toBeVisible();
  await user.clear(within(first).getByRole("textbox", { name: "Model name" }));
  await user.type(within(first).getByRole("textbox", { name: "Model name" }), "Friendly model");
  expect(within(second).getByRole("textbox", { name: "Model name" })).toHaveValue("second-model");
  expect(screen.getByText(/Access plans that allow all models may use these models immediately/)).toBeVisible();
});

it("stops showing cached Ready after a failed health refresh and recovers", async () => {
  const { queryKeys } = await import("@/lib/api");
  const healthy: Awaited<ReturnType<typeof api.status>> = {
    current_health: {
      state: "ok",
      tone: "ok",
      label: "Ready",
      detail: "Fixture engine is reachable.",
      observation_available: true,
      services: [{
        service_id: "primary",
        name: "primary",
        configured: true,
        enabled: true,
        role: "primary",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      }],
    },
    backends: [{ service_id: "primary", status: "up" }],
  };
  vi.spyOn(api.services, "registry").mockResolvedValue(registry);
  const status = vi.spyOn(api, "status").mockResolvedValue(healthy);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <Page />
    </QueryClientProvider>,
  );

  try {
    const heading = await screen.findByRole("heading", { name: "primary" });
    const row = within(heading.closest("tr")!);
    await waitFor(() => expect(row.getByText("Ready", { exact: true })).toBeVisible());

    status.mockRejectedValue(new Error("Fixture health is unavailable"));
    await act(async () => {
      await client.refetchQueries({ queryKey: queryKeys.status, exact: true });
    });

    expect(await screen.findByText("Live status unavailable")).toBeVisible();
    expect(client.getQueryData(queryKeys.status)).toEqual(healthy);
    expect(row.queryByText("Ready", { exact: true })).toBeNull();

    status.mockResolvedValue(healthy);
    await act(async () => {
      await client.refetchQueries({ queryKey: queryKeys.status, exact: true });
    });

    await waitFor(() => expect(row.getByText("Ready", { exact: true })).toBeVisible());
    expect(screen.queryByText("Live status unavailable")).toBeNull();
  } finally {
    view.unmount();
    client.clear();
  }
});
