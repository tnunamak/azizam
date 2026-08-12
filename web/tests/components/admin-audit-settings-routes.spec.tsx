import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const adminFetch = vi.hoisted(() => vi.fn());

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", ResizeObserverStub);

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/audit",
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  adminFetch,
  adminRegistryMutation: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

import AdminSettingsPage from "@/app/admin/settings/page";

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AdminSettingsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Admin Settings mounted behavior", () => {
  it("does not open sign-in or media editors after their reads fail", async () => {
    adminFetch.mockRejectedValue(new Error("unavailable"));
    renderSettings();

    await waitFor(() => {
      expect(screen.getByText("Couldn't load sign-in settings.")).toBeVisible();
      expect(screen.getByText("Couldn't load media admission settings.")).toBeVisible();
    });
    expect(screen.getByRole("button", { name: "Configure" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
  });

  it("reports OIDC required fields through Field accessibility wiring", async () => {
    adminFetch.mockImplementation(async (path: string) => {
      if (path === "/admin/api/oidc-config") return { oidc_config: null };
      if (path === "/admin/api/extensions") return { extension_packages: { packages: {} } };
      if (path === "/admin/api/operator/overrides") return { overrides: { media_job_admission: null } };
      throw new Error(`unexpected request: ${path}`);
    });
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "Configure" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Fix the highlighted fields before saving.")).toBeVisible();
    expect(screen.getByLabelText(/Issuer URL/)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText(/Client ID/)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText(/Redirect URI/)).toHaveAttribute("aria-invalid", "true");
  });
});
