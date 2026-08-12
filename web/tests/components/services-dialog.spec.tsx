import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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
      add: vi.fn(),
    },
  },
  ApiError: class ApiError extends Error {},
  queryKeys: {
    servicePresets: ["service-presets"],
    serviceRegistry: ["services"],
    status: ["status"],
  },
}));

import { Dialog } from "@/components/ui";
import { AddServiceDialogContent } from "@/app/admin/services/page";

function AddServiceFixture() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <Dialog open onOpenChange={() => undefined}>
        <AddServiceDialogContent
          onOpenChange={() => undefined}
          onSaved={() => undefined}
        />
      </Dialog>
    </QueryClientProvider>
  );
}

describe("Services add dialog", () => {
  it("marks Provider as required and focuses the validation summary", async () => {
    render(<AddServiceFixture />);

    await screen.findByRole("option", { name: "OpenAI compatible" });
    expect(screen.getByRole("combobox", { name: /Provider/ })).toBeRequired();

    const form = (
      screen.getByRole("button", { name: "Add service" }) as HTMLButtonElement
    ).form;
    if (!form) throw new Error("Expected the add-service form");
    fireEvent.submit(form);

    const summary = await screen.findByRole("alert");
    await waitFor(() => expect(summary).toHaveFocus());
    expect(summary).toHaveTextContent(
      "Fix the highlighted fields before adding the service.",
    );
    expect(
      screen.getByText("Select an available provider preset."),
    ).toBeVisible();
  });
});
