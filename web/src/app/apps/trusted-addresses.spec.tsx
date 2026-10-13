import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AppsPage from "./page";
import { accountApi, type AccountSnapshot } from "@/lib/account-api";
import { adminFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("@/lib/account-api", () => ({
  accountApi: { session: vi.fn(), discovery: vi.fn() },
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));

const app = {
  id: "display",
  name: "Living room display",
  kind: "service",
  disabled_at: null,
  created_at: 1800000000,
  access_plan: null,
  can_mint_keys: true,
  credentials: [],
};
const snapshot = (role: string): AccountSnapshot => ({
  authenticated: true,
  csrf_token: "test",
  user: { id: "u", email: "o@example.test", name: "Owner", role },
  apps: [app],
});
let entries: {
  id: string;
  app_id: string;
  address: string;
  note: string | null;
  created_at: number;
}[];

beforeEach(() => {
  entries = [];
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  vi.mocked(adminFetch)
    .mockReset()
    .mockImplementation(
      async (path: string, init?: { method?: string; body?: unknown }) => {
        if (init?.method === "POST") {
          const body = init.body as { address: string; note: string };
          entries = [
            ...entries,
            {
              id: "tad_1",
              app_id: "display",
              address: body.address,
              note: body.note || null,
              created_at: 1,
            },
          ];
          return { trusted_address: entries[0] };
        }
        if (init?.method === "DELETE") {
          entries = [];
          return {};
        }
        expect(path).toBe("/admin/api/apps/display/trusted-addresses");
        return { trusted_addresses: entries };
      },
    );
});
afterEach(cleanup);

function renderApps() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <AppsPage />
    </QueryClientProvider>,
  );
}

function openMenu() {
  const trigger = screen.getByRole("button", {
    name: "Actions for Living room display",
  });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "Enter" });
}

it("offers Trusted addresses to the owner only", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshot("none"));
  renderApps();
  await screen.findByRole("heading", { name: "Living room display" });
  openMenu();
  await screen.findByRole("menuitem", { name: "Remove app" });
  expect(
    screen.queryByRole("menuitem", { name: "Trusted addresses" }),
  ).toBeNull();
});

it("starts empty, adds an address with a note, and removes it", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshot("owner"));
  renderApps();
  await screen.findByRole("heading", { name: "Living room display" });
  openMenu();
  fireEvent.click(
    await screen.findByRole("menuitem", { name: "Trusted addresses" }),
  );
  expect(
    await screen.findByText(/No address is trusted for this app/),
  ).toBeVisible();

  fireEvent.change(screen.getByLabelText("IP address or range"), {
    target: { value: "192.168.1.0/24" },
  });
  fireEvent.change(screen.getByLabelText("Note (optional)"), {
    target: { value: "Display" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Trust address" }));
  expect(await screen.findByText("192.168.1.0/24")).toBeVisible();
  expect(screen.getByText("Display")).toBeVisible();

  fireEvent.click(
    screen.getByRole("button", { name: "Remove 192.168.1.0/24" }),
  );
  await waitFor(() =>
    expect(
      screen.getByText(/No address is trusted for this app/),
    ).toBeVisible(),
  );
});

it("shows the gateway's sentence when an address is refused", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshot("owner"));
  renderApps();
  await screen.findByRole("heading", { name: "Living room display" });
  openMenu();
  fireEvent.click(
    await screen.findByRole("menuitem", { name: "Trusted addresses" }),
  );
  await screen.findByText(/No address is trusted/);
  vi.mocked(adminFetch).mockRejectedValueOnce(
    new Error("A range of every address would trust the whole internet."),
  );
  fireEvent.change(screen.getByLabelText("IP address or range"), {
    target: { value: "0.0.0.0/0" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Trust address" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "would trust the whole internet",
  );
});
