import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AppsPage from "./page";
import { type Credential, type MintedCredential } from "@/lib/api";
import { productApi } from "@/lib/product-api";
import type { ModelsCatalog } from "@/lib/product-api-types";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { accountApi, type AccountApp, type AccountSnapshot } from "@/lib/account-api";
import { unsavedWorkNow } from "@/lib/unsaved-work";

// The key reveal on the Apps page: the Connect guide it links to opens in a new tab, and the key stays held
// until its person confirms saving it (components/shell/leave-guard.tsx asks before anything else takes it).

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("@/lib/toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/toast")>()),
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    session: vi.fn(),
    discovery: vi.fn(),
    createApp: vi.fn(),
    mintKey: vi.fn(),
    revokeKey: vi.fn(),
    replaceKey: vi.fn(),
    mcpConnections: vi.fn(),
    revokeMcpConnection: vi.fn(),
  },
}));

const member: Viewer = {
  authenticated: true,
  name: "Sam",
  email: null,
  role: "member",
  admin: false,
};
const app: AccountApp = {
  id: "saved-app",
  name: "My laptop",
  kind: "app",
  disabled_at: null,
  created_at: 1800000000,
  access_plan: null,
  can_mint_keys: true,
  credentials: [],
};
const snapshot: AccountSnapshot = {
  authenticated: true,
  csrf_token: "test",
  apps: [],
};
const catalog = { aliases: [], models: [] } as unknown as ModelsCatalog;
const mintedKey: MintedCredential = {
  id: "new-key",
  subject_kind: "app",
  subject_id: app.id,
  app_id: app.id,
  prefix: "test",
  display_name: "My laptop",
  scopes: [],
  created_at: 1800000000,
  expires_at: null,
  revoked_at: null,
  last_used_at: null,
  api_key: "test-secret-not-real",
};

beforeEach(() => {
  vi.spyOn(productApi, "models").mockResolvedValue(catalog);
  vi.mocked(accountApi.session).mockResolvedValue(snapshot);
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  vi.mocked(accountApi.mcpConnections).mockResolvedValue([]);
  vi.mocked(accountApi.createApp).mockResolvedValue(app);
  vi.mocked(accountApi.mintKey).mockResolvedValue(mintedKey);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderApps() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={member}>
        <AppsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

/** Stops jsdom from following a link, which it cannot do, after the guard's decision has been read. */
function clickWithoutFollowing(link: HTMLElement) {
  link.addEventListener("click", (event) => event.preventDefault(), { once: true });
  fireEvent.click(link);
}

it("the Connect guide opens in a new tab, and the new key stays on screen and held", async () => {
  renderApps();
  // "New app" is disabled until the session has loaded, so wait for the empty state first.
  await screen.findByText("Connect your first app");
  fireEvent.click(screen.getAllByRole("button", { name: "New app" })[0]);
  fireEvent.change(screen.getByLabelText("App name"), { target: { value: "My laptop" } });
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));

  const reveal = await screen.findByRole("dialog", { name: "New key for My laptop" });
  expect(within(reveal).getByRole("textbox", { name: "Your key" })).toHaveValue("test-secret-not-real");
  expect(unsavedWorkNow()).toEqual([expect.objectContaining({ kind: "new-key", count: 1 })]);

  const guide = within(reveal).getByRole("link", { name: "Connect" });
  // Next's Link may drop the trailing slash, so either form is the Connect page.
  expect(guide.getAttribute("href")).toMatch(/^\/connect\/?$/);
  expect(guide).toHaveAttribute("target", "_blank");
  expect(guide).toHaveAttribute("rel", "noopener");
  clickWithoutFollowing(guide);

  // The dialog stays open and still shows the key: the link did not unmount the page that holds the only copy.
  expect(screen.getByRole("dialog", { name: "New key for My laptop" })).toBeVisible();
  expect(screen.getByRole("textbox", { name: "Your key" })).toHaveValue("test-secret-not-real");
  expect(unsavedWorkNow()).toEqual([expect.objectContaining({ kind: "new-key" })]);

  // Confirming the save releases the hold at once, before the click returns.
  fireEvent.click(within(reveal).getByRole("button", { name: "I’ve saved the key" }));
  expect(unsavedWorkNow()).toEqual([]);
});

it("a key replaced from its row is held until its person confirms saving it", async () => {
  const oldKey = {
    prefix: "opk_test",
    id: "old-key",
    created_by: null,
    last_used_at: null,
    cutover: "replace",
  } as Credential;
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, kind: "personal", credentials: [oldKey] }],
  });
  vi.mocked(accountApi.replaceKey).mockResolvedValue({
    credential: { ...mintedKey, id: "new-key" },
    replaced_credential_id: "old-key",
  });
  renderApps();
  const row = within((await screen.findByRole("heading", { name: "My laptop" })).closest("li")!);
  fireEvent.click(row.getByRole("button", { name: "Replace key" }));

  const reveal = await screen.findByRole("dialog", { name: "New key for My laptop" });
  expect(accountApi.replaceKey).toHaveBeenCalledWith("old-key");
  expect(unsavedWorkNow()).toEqual([expect.objectContaining({ kind: "new-key" })]);
  fireEvent.click(within(reveal).getByRole("button", { name: "I’ve saved the key" }));
  expect(unsavedWorkNow()).toEqual([]);
});
