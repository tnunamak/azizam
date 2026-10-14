import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    services: {
      presets: vi.fn(async () => ({
        presets: [
          {
            id: "openai-compatible",
            label: "OpenAI compatible",
            base_path: "/v1",
            default_port: null,
            protocol: null,
          },
        ],
      })),
      add: vi.fn(async () => ({ schema_version: 1, services: {} })),
    },
  },
  ApiError: class ApiError extends Error {},
  queryKeys: {
    servicePresets: ["service-presets"],
    serviceRegistry: ["services"],
    status: ["status"],
  },
}));

import { ViewerContext, type Viewer } from "@/components/shell/session";
import { AddServiceDialogContent } from "@/components/admin/add-service-dialog";
import { Dialog } from "@/components/ui/dialog";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const viewer = (role: "owner" | "admin"): Viewer => ({
  authenticated: true,
  name: "Someone",
  email: null,
  role,
  admin: true,
});

function renderAs(role: "owner" | "admin") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={viewer(role)}>
        <Dialog open onOpenChange={() => undefined}>
          <AddServiceDialogContent
            onOpenChange={() => undefined}
            onSaved={() => undefined}
          />
        </Dialog>
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

it("shows an administrator the key field read-only with the owner-only note", async () => {
  renderAs("admin");
  const key = await screen.findByRole("textbox", {
    name: /Key environment variable/,
  });
  expect(key).toBeDisabled();
  expect(key).toHaveValue("");
  expect(screen.getByText("Only the owner can change this.")).toBeVisible();
});

it("lets the owner type the key variable and shows no owner-only note", async () => {
  const user = userEvent.setup();
  renderAs("owner");
  const key = await screen.findByRole("textbox", {
    name: /Key environment variable/,
  });
  expect(key).toBeEnabled();
  expect(
    screen.queryByText("Only the owner can change this."),
  ).not.toBeInTheDocument();
  await user.type(key, "ENGINE_KEY");
  expect(key).toHaveValue("ENGINE_KEY");
});
