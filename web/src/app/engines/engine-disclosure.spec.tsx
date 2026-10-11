import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type Service, type ServiceRegistry } from "@/lib/api";
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
