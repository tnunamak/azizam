import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

const { toggleEnabled } = vi.hoisted(() => ({ toggleEnabled: vi.fn() }));
vi.stubGlobal("matchMedia", () => ({
  matches: true,
  addEventListener: () => undefined,
  removeEventListener: () => undefined,
}));
vi.mock("@/lib/api", () => ({
  api: {
    status: vi.fn(async () => ({ backends: [], current_health: { services: [] } })),
    services: {
      registry: vi.fn(async () => ({
        services: {
          first: { id: "first", name: "First", enabled: true, capabilities: [], config: {}, editable: true },
          second: { id: "second", name: "Second", enabled: true, capabilities: [], config: {}, editable: true },
        },
      })),
      toggleEnabled,
    },
  },
  ApiError: class ApiError extends Error {
    constructor(public code: string, public message: string, public status: number) {
      super(message);
    }
  },
  queryKeys: {
    serviceRegistry: ["services"],
    status: ["status"],
    servicePresets: ["service-presets"],
  },
}));

import AdminServicesPage from "@/app/admin/services/page";

afterEach(() => {
  cleanup();
  toggleEnabled.mockReset();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function Fixture() {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AdminServicesPage />
    </QueryClientProvider>
  );
}

describe("Services page heading", () => {
  it("gives Services a real page-level h1 without inflating the compact heading", async () => {
    render(<Fixture />);
    const heading = await screen.findByRole("heading", {
      name: "Services",
      level: 1,
    });
    expect(heading.tagName).toBe("H1");
    expect(document.querySelectorAll("h1")).toHaveLength(1);
  });
});

describe("Services concurrent toggles", () => {
  it("keeps another pending intent after a 412 settles", async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    toggleEnabled.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<Fixture />);

    const firstToggle = await screen.findByRole("switch", { name: "Toggle First" });
    const secondToggle = screen.getByRole("switch", { name: "Toggle Second" });
    fireEvent.click(firstToggle);
    fireEvent.click(secondToggle);

    await waitFor(() => {
      expect(firstToggle).toBeDisabled();
      expect(secondToggle).toBeDisabled();
    });

    first.reject(new (await import("@/lib/api")).ApiError("generation_mismatch", "changed", 412));

    await waitFor(() => {
      expect(firstToggle).toBeChecked();
      expect(firstToggle).not.toBeDisabled();
      expect(secondToggle).not.toBeChecked();
      expect(secondToggle).toBeDisabled();
    });

    second.resolve({
      services: {
        first: { id: "first", name: "First", enabled: true, capabilities: [], config: {}, editable: true },
        second: { id: "second", name: "Second", enabled: false, capabilities: [], config: {}, editable: true },
      },
    });

    await waitFor(() => {
      expect(firstToggle).not.toBeDisabled();
      expect(secondToggle).not.toBeDisabled();
      expect(toggleEnabled).toHaveBeenCalledTimes(2);
    });
  });

  it("keeps another pending intent after a successful response settles", async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    toggleEnabled.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<Fixture />);

    const firstToggle = await screen.findByRole("switch", { name: "Toggle First" });
    const secondToggle = screen.getByRole("switch", { name: "Toggle Second" });
    fireEvent.click(firstToggle);
    fireEvent.click(secondToggle);

    first.resolve({
      services: {
        first: { id: "first", name: "First", enabled: false, capabilities: [], config: {}, editable: true },
        second: { id: "second", name: "Second", enabled: true, capabilities: [], config: {}, editable: true },
      },
    });

    await waitFor(() => {
      expect(firstToggle).not.toBeDisabled();
      expect(secondToggle).not.toBeChecked();
      expect(secondToggle).toBeDisabled();
    });

    second.resolve({
      services: {
        first: { id: "first", name: "First", enabled: false, capabilities: [], config: {}, editable: true },
        second: { id: "second", name: "Second", enabled: false, capabilities: [], config: {}, editable: true },
      },
    });
    await waitFor(() => expect(secondToggle).not.toBeDisabled());
  });
});

describe("Services row selection", () => {
  it("populates the right context panel with the clicked service's own details", async () => {
    render(<Fixture />);

    // Both the generic "nothing selected" summary and each service's own
    // panel must be distinguishable, otherwise a click that silently no-ops
    // would be indistinguishable from a correct render.
    await screen.findByText("First");
    expect(screen.queryByRole("heading", { name: "First" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Second" })).not.toBeInTheDocument();

    const firstRow = screen.getByRole("button", { name: /First — click for details/i });
    fireEvent.click(firstRow);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "First" })).toBeVisible();
    });
    expect(screen.queryByRole("heading", { name: "Second" })).not.toBeInTheDocument();

    const secondRow = screen.getByRole("button", { name: /Second — click for details/i });
    fireEvent.click(secondRow);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Second" })).toBeVisible();
    });
    expect(screen.queryByRole("heading", { name: "First" })).not.toBeInTheDocument();
  });

  it("deselects and restores the generic summary when the selected row is clicked again", async () => {
    render(<Fixture />);
    const firstRow = await screen.findByRole("button", { name: /First — click for details/i });
    fireEvent.click(firstRow);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "First" })).toBeVisible();
    });

    fireEvent.click(firstRow);
    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "First" })).not.toBeInTheDocument();
    });
  });
});
