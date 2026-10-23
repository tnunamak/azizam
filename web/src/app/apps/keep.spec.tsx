import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import AppsPage from "./page";
import { adminFetch, api, ApiError, type Credential } from "@/lib/api";
import { productApi } from "@/lib/product-api";
import { toast } from "@/lib/toast";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import {
  accountApi,
  type AccountApp,
  type AccountSnapshot,
} from "@/lib/account-api";

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
    keepKey: vi.fn(),
    keepTrustedAddress: vi.fn(),
    mcpConnections: vi.fn(),
    revokeMcpConnection: vi.fn(),
  },
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));

const KEY_NOTICE =
  "Made before Azizam recorded who made keys. Keep it, or it stops working at the next ownership update.";
const ADDRESS_NOTICE =
  "Made before Azizam recorded who made addresses. Keep it, or it stops working at the next ownership update.";

const NOW = Math.floor(Date.now() / 1000);
// Made before the gateway recorded creators: no creator.
const oldKey: Credential = {
  id: "key-old",
  subject_kind: "app",
  subject_id: "hall",
  app_id: "hall",
  prefix: "opk_OldKeyPrefixAbCd",
  display_name: "Old desk key",
  scopes: [],
  created_at: NOW - 864000,
  created_by: null,
  expires_at: null,
  revoked_at: null,
  last_used_at: NOW - 3600,
};
// Made by the person: nothing to keep.
const ownKey: Credential = {
  ...oldKey,
  id: "key-own",
  prefix: "opk_OwnKeyPrefixEfGhIj",
  display_name: "Phone key",
  created_by: "person-1",
  last_used_at: null,
};
// The gateway's record once the person has kept the old key.
const keptKey: Credential = { ...oldKey, created_by: "person-1" };

function appWith(overrides: Partial<AccountApp> = {}): AccountApp {
  return {
    id: "hall",
    name: "Hall display",
    kind: "app",
    disabled_at: null,
    created_at: NOW - 864000,
    access_plan: null,
    can_mint_keys: true,
    credentials: [oldKey, ownKey],
    ...overrides,
  };
}

function snapshotOf(apps: AccountApp[], role = "member"): AccountSnapshot {
  return {
    authenticated: true,
    csrf_token: "test",
    user: { id: "person-1", email: "sam@example.test", name: "Sam", role },
    apps,
  };
}

const member: Viewer = {
  authenticated: true,
  name: "Sam",
  email: null,
  role: "member",
  admin: false,
};
const owner: Viewer = { ...member, name: "Tim", role: "owner", admin: true };

type Address = {
  id: string;
  app_id: string;
  address: string;
  note: string | null;
  created_at: number;
  created_by: string | null;
};
const unkeptAddress: Address = {
  id: "tad-1",
  app_id: "hall",
  address: "10.0.0.7",
  note: null,
  created_at: NOW - 864000,
  created_by: null,
};

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});

beforeEach(() => {
  vi.spyOn(api.accessPlans, "list").mockReset().mockResolvedValue([]);
  vi.spyOn(productApi, "models").mockReset().mockResolvedValue({
    aliases: [],
    models: [],
  } as never);
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  vi.mocked(accountApi.mcpConnections).mockReset().mockResolvedValue([]);
  vi.mocked(accountApi.session).mockReset();
  vi.mocked(accountApi.keepKey).mockReset();
  vi.mocked(accountApi.keepTrustedAddress).mockReset();
  vi.mocked(adminFetch).mockReset();
  vi.mocked(toast.success).mockClear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

function renderApps(viewer: Viewer = member) {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ViewerContext.Provider value={viewer}>
        <AppsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

/** Open the app's actions menu with the keyboard, as the existing specs do. */
async function chooseFromMenu(appName: string, item: string) {
  const trigger = await screen.findByRole("button", {
    name: `Actions for ${appName}`,
  });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: item }));
}

async function openKeys(appName: string) {
  await chooseFromMenu(appName, "Rename keys");
  return screen.findByRole("dialog", { name: `Keys for ${appName}` });
}

/** The gateway's answer to the account snapshot: `after` once a keep has gone through. */
function gatewayAnswers(before: AccountSnapshot, after: AccountSnapshot) {
  let kept = false;
  vi.mocked(accountApi.session).mockImplementation(async () =>
    kept ? after : before,
  );
  return {
    markKept: () => {
      kept = true;
    },
  };
}

it("shows the keep notice and a Keep button only on a key with no recorded creator", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith()]));
  renderApps();

  const keys = await openKeys("Hall display");
  const [oldRow, ownRow] = within(keys).getAllByRole("listitem");

  expect(within(oldRow).getByText(KEY_NOTICE)).toBeVisible();
  expect(within(oldRow).getByRole("button", { name: "Keep key Old desk key" })).toBeEnabled();
  expect(within(ownRow).queryByText(KEY_NOTICE)).toBeNull();
  expect(within(ownRow).queryByRole("button", { name: /^Keep/ })).toBeNull();
});

it("keeps the key through the gateway, removes its notice and leaves focus on its Rename button", async () => {
  const gateway = gatewayAnswers(
    snapshotOf([appWith()]),
    snapshotOf([appWith({ credentials: [keptKey, ownKey] })]),
  );
  vi.mocked(accountApi.keepKey).mockImplementation(async () => {
    gateway.markKept();
    return keptKey;
  });
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));

  await waitFor(() => expect(within(keys).queryByText(KEY_NOTICE)).toBeNull());
  expect(accountApi.keepKey).toHaveBeenCalledWith("key-old");
  expect(within(keys).queryByRole("button", { name: "Keep key Old desk key" })).toBeNull();
  expect(within(keys).getByRole("button", { name: "Rename key Old desk key" })).toHaveFocus();
  expect(toast.success).toHaveBeenCalledWith("Key kept");
});

it("shows Keeping while the gateway waits for the person to confirm, and keeps nothing else meanwhile", async () => {
  let answer!: (key: Credential) => void;
  const gateway = gatewayAnswers(
    snapshotOf([appWith()]),
    snapshotOf([appWith({ credentials: [keptKey, ownKey] })]),
  );
  vi.mocked(accountApi.keepKey).mockImplementation(
    () =>
      new Promise<Credential>((resolve) => {
        answer = (key) => {
          gateway.markKept();
          resolve(key);
        };
      }),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));

  const keeping = await within(keys).findByRole("button", { name: "Keep key Old desk key" });
  expect(keeping).toHaveTextContent("Keeping…");
  expect(keeping).toBeDisabled();
  answer(keptKey);
  await waitFor(() => expect(within(keys).queryByText(KEY_NOTICE)).toBeNull());
});

it("says under the key why it could not be kept, and keeps the notice and the Keep button", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith()]));
  vi.mocked(accountApi.keepKey).mockRejectedValue(
    new ApiError("credential_not_found", "API key not found for this account", 404),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));

  expect(await within(keys).findByRole("alert")).toHaveTextContent(
    "API key not found for this account",
  );
  expect(within(keys).getByText(KEY_NOTICE)).toBeVisible();
  expect(within(keys).getByRole("button", { name: "Keep key Old desk key" })).toBeEnabled();
  expect(toast.success).not.toHaveBeenCalled();
});

it("keeps a trusted address with no recorded creator through its route, and refreshes the Apps list", async () => {
  const before = snapshotOf([appWith({ credentials: [ownKey], addresses_without_creator: 1 })], "owner");
  const after = snapshotOf([appWith({ credentials: [ownKey], addresses_without_creator: 0 })], "owner");
  const gateway = gatewayAnswers(before, after);
  let addresses: Address[] = [unkeptAddress];
  vi.mocked(adminFetch).mockImplementation(async () => ({ trusted_addresses: addresses }));
  vi.mocked(accountApi.keepTrustedAddress).mockImplementation(async () => {
    addresses = [{ ...unkeptAddress, created_by: "person-1" }];
    gateway.markKept();
  });
  renderApps(owner);

  await chooseFromMenu("Hall display", "Trusted addresses");
  const dialog = await screen.findByRole("dialog", { name: "Trusted addresses for Hall display" });
  expect(await within(dialog).findByText(ADDRESS_NOTICE)).toBeVisible();
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep address 10.0.0.7" }));

  await waitFor(() => expect(within(dialog).queryByText(ADDRESS_NOTICE)).toBeNull());
  expect(accountApi.keepTrustedAddress).toHaveBeenCalledWith("hall", "tad-1");
  expect(toast.success).toHaveBeenCalledWith("Address kept");
  expect(within(dialog).getByRole("button", { name: "Remove 10.0.0.7" })).toHaveFocus();
  await waitFor(() =>
    expect(screen.queryByRole("link", { name: /need a quick check/ })).toBeNull(),
  );
});

it("counts keys and addresses with no recorded creator in one line that opens the first such App", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf(
      [
        appWith({ id: "tidy", name: "Tidy", credentials: [ownKey] }),
        appWith({ id: "hall", name: "Hall display", credentials: [oldKey, ownKey] }),
        appWith({
          id: "kitchen",
          name: "Kitchen tablet",
          credentials: [ownKey],
          addresses_without_creator: 2,
        }),
      ],
      "owner",
    ),
  );
  renderApps(owner);

  const line = await screen.findByRole("link", { name: "3 keys or addresses need a quick check" });
  // Next drops the trailing slash of /apps/ outside the export build, so accept both spellings.
  expect(line.getAttribute("href")).toMatch(/^\/apps\/?\?app=hall$/);
});

it("uses the singular for one binding", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith()]));
  renderApps();

  const line = await screen.findByRole("link", { name: "1 key or address needs a quick check" });
  expect(line.getAttribute("href")).toMatch(/^\/apps\/?\?app=hall$/);
});

it("shows no line once every key and address names its person", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [ownKey] })], "owner"),
  );
  renderApps(owner);

  await screen.findByRole("heading", { name: "Hall display" });
  expect(screen.queryByRole("link", { name: /need a quick check/ })).toBeNull();
});
