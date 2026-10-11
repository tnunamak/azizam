import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

import { Dialog, DialogTrigger } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { AddServiceDialogContent } from "@/components/admin/add-service-dialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function AddServiceFixture({ initiallyOpen = true }: { initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  const [client] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false } },
  }));
  return (
    <QueryClientProvider client={client}>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <Button type="button">Open add engine</Button>
        </DialogTrigger>
        {open && (
          <AddServiceDialogContent
            onOpenChange={setOpen}
            onSaved={() => undefined}
          />
        )}
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

it("keeps the body scrollable and the form footer outside it", async () => {
  render(<AddServiceFixture />);
  const submit = await screen.findByRole("button", { name: "Add engine" });
  const dialog = screen.getByRole("dialog", { name: "Add engine" });
  const body = dialog.querySelector<HTMLElement>('[data-slot="dialog-body"]');
  const footer = dialog.querySelector<HTMLElement>('[data-slot="dialog-footer"]');
  expect(dialog).toHaveClass("flex", "max-h-[85dvh]", "overflow-hidden");
  expect(body).toHaveClass("min-h-0", "flex-1", "overflow-y-auto");
  expect(footer).toHaveClass("shrink-0");
  expect(footer).toContainElement(submit);
  expect(body).not.toContainElement(footer);
  expect((submit as HTMLButtonElement).form).toHaveClass("min-h-0", "flex-1");
});

it("keeps field labels and errors associated and Cancel from submitting", async () => {
  const user = userEvent.setup();
  render(<AddServiceFixture initiallyOpen={false} />);
  const trigger = screen.getByRole("button", { name: "Open add engine" });
  await user.click(trigger);
  const name = await screen.findByRole("textbox", { name: /^Name/ });
  expect(screen.getByRole("textbox", { name: /^Base URL/ })).toBeVisible();
  expect(screen.getByRole("textbox", { name: "API key env var" })).toBeVisible();
  const submit = screen.getByRole("button", { name: "Add engine" }) as HTMLButtonElement;
  fireEvent.submit(submit.form!);
  expect(name).toHaveAttribute("aria-invalid", "true");
  expect(name).toHaveAccessibleDescription("Enter an engine name.");
  const add = vi.mocked(api.services.add);
  add.mockClear();
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(add).not.toHaveBeenCalled();
  await waitFor(() => expect(trigger).toHaveFocus());
  await user.click(trigger);
  await screen.findByRole("textbox", { name: /^Name/ });
  await user.keyboard("{Escape}");
  await waitFor(() => expect(trigger).toHaveFocus());
});

it("submits the add form by Enter and disables the pending submit", async () => {
  const user = userEvent.setup();
  let finish!: (value: Awaited<ReturnType<typeof api.services.add>>) => void;
  const add = vi.mocked(api.services.add);
  add.mockClear();
  add.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  render(<AddServiceFixture />);
  const preset = await screen.findByRole("combobox", { name: /Preset/ });
  const scrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  try {
    preset.focus();
    await user.keyboard("{ArrowDown}");
    await screen.findByRole("option", { name: "OpenAI compatible" });
    await user.keyboard("{Enter}");
  } finally {
    if (scrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollIntoView);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
  }
  await user.type(screen.getByRole("textbox", { name: /^Name/ }), "example");
  await user.type(screen.getByRole("textbox", { name: /^Base URL/ }), "http://localhost:8000/v1{Enter}");
  await waitFor(() => expect(add).toHaveBeenCalledOnce());
  expect(add).toHaveBeenCalledWith({
    name: "example",
    preset: "openai-compatible",
    base_url: "http://localhost:8000/v1",
  });
  const submit = screen.getByRole("button", { name: "Add engine" });
  expect(submit).toBeDisabled();
  expect(submit).toHaveAttribute("aria-busy", "true");
  finish({ schema_version: 1, services: {} });
  await waitFor(() => expect(submit).not.toBeDisabled());
});
