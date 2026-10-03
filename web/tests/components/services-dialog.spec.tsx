import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
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
import { AddServiceDialogContent } from "@/components/admin/add-service-dialog";

afterEach(() => {
  vi.restoreAllMocks();
});

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

describe("Engines add dialog", () => {
  it("fails if Radix reports a missing dialog description", async () => {
    const failure = (args: unknown[]) => {
      if (args.some((arg) => String(arg).includes("Missing `Description`"))) {
        throw new Error("Radix dialog description warning");
      }
    };
    vi.spyOn(console, "warn").mockImplementation((...args) => failure(args));
    vi.spyOn(console, "error").mockImplementation((...args) => failure(args));

    render(<AddServiceFixture />);
    expect(
      await screen.findByRole("dialog", { name: "Add engine" }),
    ).toBeVisible();
  });

  it("marks Preset as required and focuses the validation summary", async () => {
    render(<AddServiceFixture />);

    const preset = await screen.findByRole("combobox", { name: /Preset/ });
    expect(preset).toBeRequired();

    const form = (
      screen.getByRole("button", { name: "Add engine" }) as HTMLButtonElement
    ).form;
    if (!form) throw new Error("Expected the add-engine form");
    fireEvent.submit(form);

    const summary = await screen.findByRole("alert");
    await waitFor(() => expect(summary).toHaveFocus());
    expect(summary).toHaveTextContent(
      "Fix the highlighted fields before adding the engine.",
    );
    expect(
      screen.getByText("Select an available engine preset."),
    ).toBeVisible();
  });
});
