import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

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

describe("Services concurrent toggles", () => {
  it("keeps each in-flight row disabled and rolls back only the rejected 412", async () => {
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
});
