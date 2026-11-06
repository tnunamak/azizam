import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type Service, type ServiceRegistry } from "@/lib/api";
import { enginesApi } from "@/lib/engines-api";
import { definitions } from "./__fixtures__/test-utils";
import Page from "./page";

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => router,
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ admin: true, role: "owner" }),
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
  config: { base_url: "http://engine-a.test" },
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
  router.push.mockReset();
});

describe("engine progressive disclosure", () => {
  it("starts compact, reveals fallback and disabled engines, opens rows as record pages, and opens Add engine", async () => {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry);
    vi.spyOn(api, "status").mockResolvedValue({});
    const defs = vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
    const user = userEvent.setup();
    mount();
    const primary = await screen.findByRole("heading", { name: "primary" });
    expect(screen.getByRole("heading", { name: "Chat" })).toBeVisible();
    // Fallback engines are rows of the one Engines table, rendered only once opened.
    expect(screen.queryByRole("heading", { name: "standby" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "disabled" })).not.toBeInTheDocument();
    expect(defs).not.toHaveBeenCalled();
    const row = primary.closest("tr")!;
    const rowButton = within(row).getByRole("button", { name: /^primary\b/ });
    expect(rowButton).toBeVisible();
    await user.click(rowButton);
    expect(router.push).toHaveBeenCalledWith("/engines/?engine=primary");
    await user.click(within(row).getByRole("button", { name: "Actions for primary" }));
    await user.click(screen.getByRole("menuitem", { name: "Open engine" }));
    expect(router.push).toHaveBeenLastCalledWith("/engines/?engine=primary");
    await user.click(screen.getByText("Standby and fallback engines (1)"));
    expect(screen.getByRole("heading", { name: "standby" })).toBeVisible();
    await user.click(screen.getByRole("switch", { name: "Show not in use" }));
    expect(screen.getByRole("heading", { name: "disabled" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add engine" }));
    const dialog = await screen.findByRole("dialog", { name: "Add engine" });
    expect(await within(dialog).findByText("Step 1 of 5 · Kind")).toBeVisible();
    expect(defs).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Advanced JSON/)).toBeNull();
    expect(document.querySelector("textarea")).toBeNull();
  });
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

it("shows an observed-up engine with a bad health tone as unavailable, never ready", async () => {
  vi.spyOn(api.services, "registry").mockResolvedValue({
    schema_version: 1,
    services: {
      engine_chat: {
        id: "engine_chat",
        name: "Chat engine",
        kind: "upstream",
        source: "ui",
        enabled: true,
        editable: true,
        capabilities: ["chat"],
        role: "primary",
        config: { protocol: "protocol-a" },
      },
    },
  });
  vi.spyOn(api, "status").mockResolvedValue({
    current_health: {
      observation_available: true,
      services: [
        {
          service_id: "engine_chat",
          name: "Chat engine",
          enabled: true,
          observation: "up",
          tone: "bad",
        },
      ],
    },
    backends: [],
  } as unknown as Awaited<ReturnType<typeof api.status>>);
  mount();
  const row = (await screen.findByText("Chat engine")).closest("tr")!;
  expect(await within(row).findByText("Unavailable", { exact: true })).toBeVisible();
  expect(within(row).queryByText("Ready", { exact: true })).toBeNull();
});
