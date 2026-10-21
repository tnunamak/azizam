import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
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
  await user.click(screen.getByRole("option", { name: label }));
  return user;
}

it("never saves a late preview for a different retention choice", async () => {
  type Preview = { cutoff: number; delete_count: number };
  const pending = new Map<string, (value: Preview) => void>();
  let finishSave!: (value: { retention_days: number; revision: string }) => void;
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation((url: string, options?: { method?: string }) => {
    if (options?.method === "PUT") {
      return new Promise((resolve) => { finishSave = resolve; });
    }
    if (url.includes("/preview?")) {
      const days = new URL(url, "http://fixture.test").searchParams.get("retention_days")!;
      return new Promise<Preview>((resolve) => { pending.set(days, resolve); });
    }
    return Promise.resolve({ retention_days: null, revision: "7" });
  });
  renderSection();
  const user = await choose("30 days");
  await waitFor(() => expect(pending.has("30")).toBe(true));
  await choose("90 days");
  await waitFor(() => expect(pending.has("90")).toBe(true));
  const saveButton = () => screen.getByRole("button", { name: "Save receipt setting" });
  expect(saveButton()).toBeDisabled();

  await act(async () => pending.get("30")!({ cutoff: 300, delete_count: 99 }));
  expect(saveButton()).toBeDisabled();
  expect(screen.queryByText(/permanently delete 99 receipts/)).not.toBeInTheDocument();

  const reviewed = { cutoff: 900, delete_count: 3 };
  await act(async () => pending.get("90")!(reviewed));
  expect(await screen.findByText(/permanently delete 3 receipts older than 90 days/)).toBeVisible();
  const puts = () => mocks.fetch.mock.calls.filter(([, options]) => options?.method === "PUT");
  expect(puts()).toHaveLength(0);
  await user.click(saveButton());
  await waitFor(() => expect(puts()).toHaveLength(1));
  expect(puts()[0]).toEqual([
    "/admin/api/settings/receipts",
    { method: "PUT", body: { retention_days: 90, preview: reviewed }, headers: { "If-Match": "7" } },
  ]);
  expect(screen.getByRole("combobox", { name: "Keep receipts" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Saving…" }));
  expect(puts()).toHaveLength(1);
  await act(async () => finishSave({ retention_days: 90, revision: "8" }));
  expect(await screen.findByText(/Saved: 90 days/)).toBeVisible();
  expect(saveButton()).toBeDisabled();
});

it("adopts a conflicted retention revision and requires a newly reviewed save", async () => {
  let revision = "7";
  let saved: number | null = null;
  let writes = 0;
  const previews: Record<string, { cutoff: number; delete_count: number }> = {
    "7": { cutoff: 700, delete_count: 17 },
    "8": { cutoff: 800, delete_count: 2 },
  };
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation(async (url: string, options?: { method?: string }) => {
    if (options?.method === "PUT") {
      writes += 1;
      if (writes === 1) {
        revision = "8";
        saved = 30;
        throw new Error("Settings changed elsewhere");
      }
      saved = 90;
      return { retention_days: 90, revision: "9" };
    }
    if (url.includes("/preview?")) return previews[revision];
    return { retention_days: saved, revision };
  });
  renderSection();
  const user = await choose("90 days");
  await screen.findByText(/permanently delete 17 receipts/);
  await user.click(screen.getByRole("button", { name: "Save receipt setting" }));
  expect(await screen.findByText(/Saved: 30 days/)).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Keep receipts" })).toHaveTextContent("30 days");
  expect(screen.getByRole("button", { name: "Save receipt setting" })).toBeDisabled();
  expect(writes).toBe(1);

  await choose("90 days");
  expect(await screen.findByText(/permanently delete 2 receipts/)).toBeVisible();
  expect(writes).toBe(1);
  await user.click(screen.getByRole("button", { name: "Save receipt setting" }));
  expect(await screen.findByText(/Saved: 90 days/)).toBeVisible();
  const puts = mocks.fetch.mock.calls.filter(([, options]) => options?.method === "PUT");
  expect(puts).toHaveLength(2);
  expect(puts[1][1]).toEqual({
    method: "PUT",
    body: { retention_days: 90, preview: previews["8"] },
    headers: { "If-Match": "8" },
  });
});
