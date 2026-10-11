import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), role: "owner" }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  adminFetch: mocks.fetch,
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: mocks.role }),
}));
vi.mock("@/lib/toast", () => ({ toast: { success: vi.fn() } }));
import { ReceiptsSection } from "@/app/settings/receipts";

beforeEach(() => {
  mocks.role = "owner";
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});
function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ReceiptsSection />
    </QueryClientProvider>,
  );
}
async function choose(label: string) {
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("combobox", { name: "Keep receipts" }),
  );
  await user.click(screen.getByRole("option", { name: label, exact: true }));
  return user;
}

it("previews without saving; reverting the choice deletes nothing", async () => {
  mocks.fetch.mockImplementation((url: string) =>
    Promise.resolve(
      url.includes("preview")
        ? { cutoff: 1234, delete_count: 17 }
        : { retention_days: null, revision: "0" },
    ),
  );
  renderSection();
  await choose("30 days");
  expect(
    await screen.findByText(/Saving will permanently delete 17 receipts/),
  ).toBeVisible();
  expect(
    mocks.fetch.mock.calls.every(([, options]) => options?.method !== "PUT"),
  ).toBe(true);
  await choose("Forever");
  expect(
    screen.queryByText(/Saving will permanently delete/),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Save receipt setting" }),
  ).toBeDisabled();
  expect(
    mocks.fetch.mock.calls.every(([, options]) => options?.method !== "PUT"),
  ).toBe(true);
});

it("saves the displayed cutoff and count, then shows the saved choice", async () => {
  const preview = { cutoff: 1234, delete_count: 17 };
  mocks.fetch.mockImplementation((url: string, options?: { method?: string }) =>
    Promise.resolve(
      options?.method === "PUT"
        ? { retention_days: 90, revision: "1", deleted_count: 17 }
        : url.includes("preview")
          ? preview
          : { retention_days: null, revision: "0" },
    ),
  );
  renderSection();
  const user = await choose("90 days");
  await screen.findByText(/Saving will permanently delete 17 receipts/);
  await user.click(
    screen.getByRole("button", { name: "Save receipt setting" }),
  );
  await waitFor(() =>
    expect(mocks.fetch).toHaveBeenCalledWith("/admin/api/settings/receipts", {
      method: "PUT",
      body: { retention_days: 90, preview },
      headers: { "If-Match": "0" },
    }),
  );
  expect(await screen.findByText(/Saved: 90 days/)).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Save receipt setting" }),
  ).toBeDisabled();
});

it("blocks Save while the deletion preview is unavailable", async () => {
  mocks.fetch.mockImplementation((url: string) =>
    url.includes("preview")
      ? Promise.reject(new Error("Preview unavailable"))
      : Promise.resolve({ retention_days: null, revision: "0" }),
  );
  renderSection();
  await choose("1 year");
  expect(await screen.findByText("Couldn’t preview deletion")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Save receipt setting" }),
  ).toBeDisabled();
  expect(
    mocks.fetch.mock.calls.every(([, options]) => options?.method !== "PUT"),
  ).toBe(true);
});

it("shows members that only the owner can change receipt retention", async () => {
  mocks.role = "member";
  renderSection();
  expect(screen.getByText("Only the owner can change this.")).toBeVisible();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(mocks.fetch).not.toHaveBeenCalled();
});
