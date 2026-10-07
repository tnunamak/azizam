import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
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
    expect(screen.getByRole("heading", { name: "standby" })).not.toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "disabled" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Available presets")).not.toBeInTheDocument();
    expect(presets).not.toHaveBeenCalled();
    const article = primary.closest("article")!;
    const rowButton = within(article).getByRole("button", {
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
    const toggle = vi
      .spyOn(api.services, "toggleEnabled")
      .mockResolvedValue(registry);
    const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /^primary\b/ }));
    await user.click(screen.getByRole("switch", { name: "Toggle primary" }));
    expect(
      screen.getByRole("switch", { name: "Toggle primary" }),
    ).not.toBeChecked();
    expect(toggle).not.toHaveBeenCalled();
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
