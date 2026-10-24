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
import { adminFetch, api, ApiError, type Credential, type MintedCredential } from "@/lib/api";
import { productApi } from "@/lib/product-api";
import { toast } from "@/lib/toast";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import {
  accountApi,
  type AccountApp,
  type AccountSnapshot,
  type AdministratorsOnRecord,
} from "@/lib/account-api";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("@/lib/toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/toast")>()),
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/lib/account-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/account-api")>()),
  accountApi: {
    session: vi.fn(),
    discovery: vi.fn(),
    keepKey: vi.fn(),
    replaceKey: vi.fn(),
    keepTrustedAddress: vi.fn(),
    readdTrustedAddress: vi.fn(),
    addTrustedAddress: vi.fn(),
    removeTrustedAddress: vi.fn(),
    mcpConnections: vi.fn(),
    revokeMcpConnection: vi.fn(),
  },
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));

// The notices and buttons the gateway's remedy decides between. A binding the cut-over keeps is never shown the others.
const KEEP_NOTICE =
  "Made before Azizam recorded who made keys. Keep it, or it stops working at the next ownership update.";
const KEY_REPLACE_UNNAMED =
  "Made before Azizam recorded who made keys. It stops working at the next ownership update. Replace it with a new key.";
const KEY_REPLACE_ALEX =
  "Made by Alex. It stops working at the next ownership update. Replace it with a new key.";
const ADDRESS_KEEP_NOTICE =
  "Made before Azizam recorded who made addresses. Keep it, or it stops working at the next ownership update.";
const ADDRESS_ADD_AGAIN_ALEX =
  "Made by Alex. It stops working at the next ownership update. Add it again yourself to keep it.";

const NOW = Math.floor(Date.now() / 1000);
// Made before the gateway recorded creators: no creator on record.
const unkeptKey: Credential = {
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
// The cut-over would remove it, and keeping it is the one remedy the person has.
const oldKey: Credential = { ...unkeptKey, cutover: "keep" };
// The same key, with no creator on record, that the keep rule does not accept: the person replaces it.
const replaceableKey: Credential = { ...unkeptKey, cutover: "replace" };
// Made by the person: nothing to keep.
const ownKey: Credential = {
  ...unkeptKey,
  id: "key-own",
  prefix: "opk_OwnKeyPrefixEfGhIj",
  display_name: "Phone key",
  created_by: "person-1",
  last_used_at: null,
};
// The gateway's record once the person has kept the old key.
const keptKey: Credential = { ...unkeptKey, created_by: "person-1" };
// Made by another person: the cut-over removes it, and the person replaces it. The gateway names the maker.
const otherKey: Credential = {
  ...ownKey,
  id: "key-desk",
  prefix: "opk_DeskKeyPrefixKlMnOp",
  display_name: "Desk laptop",
  created_by: "person-2",
  made_by_name: "Alex",
  cutover: "replace",
};
// The new key a replacement makes: same App and name, made by the person. The record is what the account snapshot
// holds afterwards. The secret comes back once, with the replacement, and never in the snapshot.
const replacementRecord: Credential = {
  ...ownKey,
  id: "key-new",
  prefix: "opk_NewKeyPrefixQrStUv",
  display_name: "Old desk key",
};
const replacementKey: MintedCredential = {
  ...replacementRecord,
  api_key: "opk_NewKeyPrefixQrStUvWxYz0123456789",
};

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
  cutover?: "keep" | "readd";
  made_by_name?: string;
  administrators?: AdministratorsOnRecord;
};
const unkeptAddress: Address = {
  id: "tad-1",
  app_id: "hall",
  address: "10.0.0.7",
  note: null,
  created_at: NOW - 864000,
  created_by: null,
  cutover: "keep",
};
// Made by another person: the cut-over removes it, and the person adds it again.
const otherAddress: Address = {
  id: "tad-2",
  app_id: "hall",
  address: "10.0.0.8",
  note: "Hall speaker",
  created_at: NOW - 864000,
  created_by: "person-2",
  cutover: "readd",
  made_by_name: "Alex",
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
  vi.mocked(accountApi.replaceKey).mockReset();
  vi.mocked(accountApi.keepTrustedAddress).mockReset();
  vi.mocked(accountApi.readdTrustedAddress).mockReset();
  vi.mocked(accountApi.addTrustedAddress).mockReset();
  vi.mocked(accountApi.removeTrustedAddress).mockReset();
  vi.mocked(adminFetch).mockReset();
  vi.mocked(toast.success).mockClear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

function renderApps(viewer: Viewer = member): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={viewer}>
        <AppsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  return client;
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

/** The owner's Allow keyless access… item in an app's menu opens its keyless addresses. */
async function openTrustedAddresses(appName: string) {
  await chooseFromMenu(appName, "Allow keyless access…");
}

async function openKeys(appName: string) {
  await chooseFromMenu(appName, "Rename keys");
  return screen.findByRole("dialog", { name: `Keys for ${appName}` });
}

/** The gateway's answer to the account snapshot: `after` once a change has gone through. */
function gatewayAnswers(before: AccountSnapshot, after: AccountSnapshot) {
  let changed = false;
  vi.mocked(accountApi.session).mockImplementation(async () =>
    changed ? after : before,
  );
  return {
    markKept: () => {
      changed = true;
    },
  };
}

it("shows the keep notice and a Keep button only on a key the cut-over keeps", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith()]));
  renderApps();

  const keys = await openKeys("Hall display");
  const [oldRow, ownRow] = within(keys).getAllByRole("listitem");

  expect(within(oldRow).getByText(KEEP_NOTICE)).toBeVisible();
  expect(within(oldRow).getByRole("button", { name: "Keep key Old desk key" })).toBeEnabled();
  expect(within(oldRow).queryByRole("button", { name: /^Replace key/ })).toBeNull();
  expect(within(ownRow).queryByText(KEEP_NOTICE)).toBeNull();
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

  await waitFor(() => expect(within(keys).queryByText(KEEP_NOTICE)).toBeNull());
  expect(accountApi.keepKey).toHaveBeenCalledWith("key-old", {});
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
  await waitFor(() => expect(within(keys).queryByText(KEEP_NOTICE)).toBeNull());
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
  expect(within(keys).getByText(KEEP_NOTICE)).toBeVisible();
  expect(within(keys).getByRole("button", { name: "Keep key Old desk key" })).toBeEnabled();
  expect(toast.success).not.toHaveBeenCalled();
});

it("names the maker of a key another person made and offers one Replace key button in its place", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [otherKey, ownKey] })]),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  const [otherRow] = within(keys).getAllByRole("listitem");

  expect(within(otherRow).getByText(KEY_REPLACE_ALEX)).toBeVisible();
  expect(within(otherRow).getByRole("button", { name: "Replace key Desk laptop" })).toBeEnabled();
  expect(within(otherRow).queryByRole("button", { name: /^Keep/ })).toBeNull();
});

it("says a key with no recorded creator needs replacing when the gateway says keeping it is not provable", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [replaceableKey] })]),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  const [row] = within(keys).getAllByRole("listitem");

  expect(within(row).getByText(KEY_REPLACE_UNNAMED)).toBeVisible();
  expect(within(row).getByRole("button", { name: "Replace key Old desk key" })).toBeEnabled();
  expect(within(row).queryByRole("button", { name: /^Keep/ })).toBeNull();
});

it("replaces the key through the gateway, shows the new key once, and takes the old key's row away", async () => {
  const gateway = gatewayAnswers(
    snapshotOf([appWith({ credentials: [replaceableKey, ownKey] })]),
    snapshotOf([appWith({ credentials: [replacementRecord, ownKey] })]),
  );
  vi.mocked(accountApi.replaceKey).mockImplementation(async () => {
    gateway.markKept();
    return { credential: replacementKey, replaced_credential_id: "key-old" };
  });
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Replace key Old desk key" }));

  const reveal = await screen.findByRole("dialog", { name: "New key for Hall display" });
  expect(accountApi.replaceKey).toHaveBeenCalledWith("key-old");
  expect(within(reveal).getByRole("textbox", { name: "Your key" })).toHaveValue(replacementKey.api_key);
  expect(within(reveal).getByText("Copy the key now. It is shown only once.")).toBeVisible();

  fireEvent.click(within(reveal).getByRole("button", { name: "I’ve saved the key" }));

  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "New key for Hall display" })).toBeNull(),
  );
  expect(within(keys).queryByRole("button", { name: "Replace key Old desk key" })).toBeNull();
  expect(within(keys).queryByText(KEY_REPLACE_UNNAMED)).toBeNull();
  expect(within(keys).getByRole("button", { name: "Rename key Old desk key" })).toHaveFocus();
  expect(toast.success).toHaveBeenCalledWith("Key saved");
});

it("keeps the new key's secret out of the account snapshot the page caches", async () => {
  const gateway = gatewayAnswers(
    snapshotOf([appWith({ credentials: [replaceableKey, ownKey] })]),
    snapshotOf([appWith({ credentials: [replacementRecord, ownKey] })]),
  );
  vi.mocked(accountApi.replaceKey).mockImplementation(async () => {
    gateway.markKept();
    return { credential: replacementKey, replaced_credential_id: "key-old" };
  });
  const client = renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Replace key Old desk key" }));
  await screen.findByRole("dialog", { name: "New key for Hall display" });

  const cached = JSON.stringify(client.getQueryData(["account", "session"]));
  expect(cached).toContain(replacementRecord.id);
  expect(cached).not.toContain(replacementKey.api_key);
});

it("shows Replacing while the gateway waits for the person to confirm, and replaces nothing else meanwhile", async () => {
  let answer!: (replaced: { credential: MintedCredential; replaced_credential_id: string }) => void;
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [replaceableKey, ownKey] })]),
  );
  vi.mocked(accountApi.replaceKey).mockImplementation(
    () =>
      new Promise((resolve) => {
        answer = resolve;
      }),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Replace key Old desk key" }));

  const replacing = await within(keys).findByRole("button", { name: "Replace key Old desk key" });
  expect(replacing).toHaveTextContent("Replacing…");
  expect(replacing).toBeDisabled();
  // Rename stays available while a replacement runs; only editing another key blocks it.
  expect(within(keys).getByRole("button", { name: "Rename key Phone key" })).toBeEnabled();
  answer({ credential: replacementKey, replaced_credential_id: "key-old" });
  expect(await screen.findByRole("dialog", { name: "New key for Hall display" })).toBeVisible();
});

it("says under the key why it could not be replaced, and keeps the old key and its button", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [replaceableKey, ownKey] })]),
  );
  vi.mocked(accountApi.replaceKey).mockRejectedValue(
    new ApiError("app_disabled", "App is disabled", 403),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Replace key Old desk key" }));

  expect(await within(keys).findByRole("alert")).toHaveTextContent("App is disabled");
  expect(within(keys).getByText(KEY_REPLACE_UNNAMED)).toBeVisible();
  expect(within(keys).getByRole("button", { name: "Replace key Old desk key" })).toBeEnabled();
  expect(screen.queryByRole("dialog", { name: "New key for Hall display" })).toBeNull();
});

it("keeps a trusted address with no recorded creator through its route, and refreshes the Apps list", async () => {
  const before = snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })], "owner");
  const after = snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 0 })], "owner");
  const gateway = gatewayAnswers(before, after);
  let addresses: Address[] = [unkeptAddress];
  vi.mocked(adminFetch).mockImplementation(async () => ({ trusted_addresses: addresses }));
  vi.mocked(accountApi.keepTrustedAddress).mockImplementation(async () => {
    addresses = [{ ...unkeptAddress, created_by: "person-1", cutover: undefined }];
    gateway.markKept();
  });
  renderApps(owner);

  await openTrustedAddresses("Hall display");
  const dialog = await screen.findByRole("dialog", { name: "Keyless access for Hall display" });
  expect(await within(dialog).findByText(ADDRESS_KEEP_NOTICE)).toBeVisible();
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep address 10.0.0.7" }));

  await waitFor(() => expect(within(dialog).queryByText(ADDRESS_KEEP_NOTICE)).toBeNull());
  expect(accountApi.keepTrustedAddress).toHaveBeenCalledWith("hall", "tad-1", {});
  expect(toast.success).toHaveBeenCalledWith("Address kept");
  expect(within(dialog).getByRole("button", { name: "Remove 10.0.0.7" })).toHaveFocus();
  await waitFor(() =>
    expect(screen.queryByRole("link", { name: /need a quick check/ })).toBeNull(),
  );
});

it("adds an address again in one call: the gateway replaces the old entry with the same address, made by this person", async () => {
  const before = snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })], "owner");
  const after = snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 0 })], "owner");
  const gateway = gatewayAnswers(before, after);
  let addresses: Address[] = [otherAddress];
  vi.mocked(adminFetch).mockImplementation(async () => ({ trusted_addresses: addresses }));
  vi.mocked(accountApi.readdTrustedAddress).mockImplementation(async () => {
    addresses = [{ ...otherAddress, id: "tad-3", created_by: "person-1", cutover: undefined, made_by_name: undefined }];
    gateway.markKept();
    return { trusted_address: { id: "tad-3" }, replaced_entry_id: "tad-2" };
  });
  renderApps(owner);

  await openTrustedAddresses("Hall display");
  const dialog = await screen.findByRole("dialog", { name: "Keyless access for Hall display" });
  expect(await within(dialog).findByText(ADDRESS_ADD_AGAIN_ALEX)).toBeVisible();
  fireEvent.click(within(dialog).getByRole("button", { name: "Add address 10.0.0.8 again" }));

  await waitFor(() => expect(within(dialog).queryByText(ADDRESS_ADD_AGAIN_ALEX)).toBeNull());
  expect(accountApi.readdTrustedAddress).toHaveBeenCalledTimes(1);
  expect(accountApi.readdTrustedAddress).toHaveBeenCalledWith("hall", "tad-2");
  expect(accountApi.removeTrustedAddress).not.toHaveBeenCalled();
  expect(accountApi.addTrustedAddress).not.toHaveBeenCalled();
  expect(toast.success).toHaveBeenCalledWith("Address added again");
  expect(within(dialog).getByRole("button", { name: "Remove 10.0.0.8" })).toHaveFocus();
});

it("says an address could not be added again, and its old entry stays listed", async () => {
  vi.mocked(adminFetch).mockImplementation(async () => ({ trusted_addresses: [otherAddress] }));
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })], "owner"),
  );
  vi.mocked(accountApi.readdTrustedAddress).mockRejectedValue(
    new ApiError("trusted_address_not_found", "Trusted address not found for this App", 404),
  );
  renderApps(owner);

  await openTrustedAddresses("Hall display");
  const dialog = await screen.findByRole("dialog", { name: "Keyless access for Hall display" });
  fireEvent.click(await within(dialog).findByRole("button", { name: "Add address 10.0.0.8 again" }));

  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "Trusted address not found for this App",
  );
  expect(within(dialog).getByRole("button", { name: "Add address 10.0.0.8 again" })).toBeEnabled();
  expect(accountApi.removeTrustedAddress).not.toHaveBeenCalled();
  expect(toast.success).not.toHaveBeenCalled();
});

it("words an address with no recorded maker as made before Azizam recorded it, not as made by someone else", async () => {
  vi.mocked(adminFetch).mockImplementation(async () => ({
    trusted_addresses: [{ ...otherAddress, created_by: null, made_by_name: undefined }],
  }));
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })], "owner"),
  );
  renderApps(owner);

  await openTrustedAddresses("Hall display");
  const dialog = await screen.findByRole("dialog", { name: "Keyless access for Hall display" });
  expect(
    await within(dialog).findByText(
      "Made before Azizam recorded who made addresses. It stops working at the next ownership update. Add it again yourself to keep it.",
    ),
  ).toBeVisible();
});

it("counts keys and addresses with a remedy in one line that opens the first such App", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf(
      [
        appWith({ id: "tidy", name: "Tidy", credentials: [ownKey] }),
        appWith({ id: "hall", name: "Hall display", credentials: [oldKey, ownKey] }),
        appWith({
          id: "kitchen",
          name: "Kitchen tablet",
          credentials: [ownKey],
          addresses_to_check: 2,
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

it("counts a key another person made, and an address that needs adding again, alongside the kept ones", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf(
      [
        appWith({ id: "hall", name: "Hall display", credentials: [otherKey, oldKey, ownKey], addresses_to_check: 1 }),
      ],
      "owner",
    ),
  );
  renderApps(owner);

  expect(await screen.findByRole("link", { name: "3 keys or addresses need a quick check" })).toBeVisible();
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

/** jsdom has no layout, so the call that brings a row into view is recorded rather than run. */
function recordScrolls(): Element[] {
  const scrolled: Element[] = [];
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: function scrollIntoView(this: Element) {
      scrolled.push(this);
    },
  });
  return scrolled;
}

afterEach(() => {
  Reflect.deleteProperty(Element.prototype, "scrollIntoView");
});

it("the quick-check line opens the first App with a key to check, with that key's remedy in view and focused", async () => {
  const scrolled = recordScrolls();
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf(
      [
        appWith({ id: "tidy", name: "Tidy", credentials: [ownKey] }),
        appWith({ id: "hall", name: "Hall display", credentials: [ownKey, oldKey] }),
      ],
      "owner",
    ),
  );
  renderApps(owner);

  fireEvent.click(await screen.findByRole("link", { name: "1 key or address needs a quick check" }));

  const dialog = await screen.findByRole("dialog", { name: "Keys for Hall display" });
  const keep = within(dialog).getByRole("button", { name: "Keep key Old desk key" });
  await waitFor(() => expect(keep).toHaveFocus());
  expect(scrolled).toContain(keep.closest("li"));
  expect(screen.queryByRole("dialog", { name: "Keys for Tidy" })).toBeNull();
});

it("with only an address to check, the same line opens the App's trusted addresses on that address", async () => {
  const scrolled = recordScrolls();
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })], "owner"),
  );
  vi.mocked(adminFetch).mockImplementation(async () => ({ trusted_addresses: [unkeptAddress] }));
  renderApps(owner);

  fireEvent.click(await screen.findByRole("link", { name: "1 key or address needs a quick check" }));

  const dialog = await screen.findByRole("dialog", { name: "Keyless access for Hall display" });
  const keep = await within(dialog).findByRole("button", { name: "Keep address 10.0.0.7" });
  await waitFor(() => expect(keep).toHaveFocus());
  expect(scrolled).toContain(keep.closest("li"));
});

it("a member with only an address to check opens no dialog: addresses are the owner's to change", async () => {
  recordScrolls();
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })]),
  );
  renderApps(member);

  fireEvent.click(await screen.findByRole("link", { name: "1 key or address needs a quick check" }));

  expect(screen.queryByRole("dialog")).toBeNull();
});

// Keeping a binding that administrators on record could also have made waits for the owner's confirmation.
const ADMINISTRATORS: AdministratorsOnRecord = { people: ["Simon Agent"], unnamed_people: 1, admin_keys: 23 };
const keyWithAdmins: Credential = { ...oldKey, administrators: ADMINISTRATORS };
const addressWithAdmins: Address = { ...unkeptAddress, administrators: ADMINISTRATORS };
// The sentence the owner reads for ADMINISTRATORS: the named people, the people with no name, and the keys, counted.
const NAMED_IN_THE_BODY = "Simon Agent, 1 person with no name, and 23 administrator keys";
function confirmBody(named: string): string {
  return `Azizam did not record who made it. Administrators at the time could also have made it: ${named}. Keep it only if you made it or trust them.`;
}

it("asks the owner to confirm the administrators before keeping a key they could have made", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith({ credentials: [keyWithAdmins] })]));
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));

  const dialog = await screen.findByRole("dialog", { name: "Keep this key?" });
  expect(within(dialog).getByText(confirmBody(NAMED_IN_THE_BODY))).toBeVisible();
  expect(within(dialog).getByRole("button", { name: "Keep key" })).toBeEnabled();
  expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeEnabled();
  expect(accountApi.keepKey).not.toHaveBeenCalled();
});

it("confirming keeps the key with the owner's confirmation, and removes its notice", async () => {
  const gateway = gatewayAnswers(
    snapshotOf([appWith({ credentials: [keyWithAdmins] })]),
    snapshotOf([appWith({ credentials: [keptKey, ownKey] })]),
  );
  vi.mocked(accountApi.keepKey).mockImplementation(async () => {
    gateway.markKept();
    return keptKey;
  });
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));
  const dialog = await screen.findByRole("dialog", { name: "Keep this key?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep key" }));

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keep this key?" })).toBeNull());
  expect(accountApi.keepKey).toHaveBeenCalledWith("key-old", { confirmed: true });
  expect(toast.success).toHaveBeenCalledWith("Key kept");
  await waitFor(() => expect(within(keys).queryByText(KEEP_NOTICE)).toBeNull());
});

it("cancelling the confirmation keeps nothing, and the key keeps its Keep button", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith({ credentials: [keyWithAdmins] })]));
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));
  const dialog = await screen.findByRole("dialog", { name: "Keep this key?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keep this key?" })).toBeNull());
  expect(accountApi.keepKey).not.toHaveBeenCalled();
  expect(within(keys).getByRole("button", { name: "Keep key Old desk key" })).toBeEnabled();
});

it("a keep the gateway asks to confirm, on a page that was not up to date, opens the same dialog with the names it gives", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith()]));
  vi.mocked(accountApi.keepKey).mockRejectedValue(
    new ApiError("keep_needs_confirmation", "Administrators at the time could also have made this key.", 409, {
      error: {
        code: "keep_needs_confirmation",
        message: "Administrators at the time could also have made this key.",
        administrators: { people: ["Sam"], unnamed_people: 0, admin_keys: 0 },
      },
    }),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));

  const dialog = await screen.findByRole("dialog", { name: "Keep this key?" });
  expect(within(dialog).getByText(confirmBody("Sam"))).toBeVisible();
  expect(accountApi.keepKey).toHaveBeenCalledWith("key-old", {});
});

it("a confirmed keep that fails says why in the dialog, and the dialog stays open", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith({ credentials: [keyWithAdmins] })]));
  vi.mocked(accountApi.keepKey).mockRejectedValue(new ApiError("keep_not_provable", "This key cannot be kept.", 409));
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));
  const dialog = await screen.findByRole("dialog", { name: "Keep this key?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep key" }));

  expect(await within(dialog).findByRole("alert")).toHaveTextContent("This key cannot be kept.");
  expect(screen.getByRole("dialog", { name: "Keep this key?" })).toBeVisible();
  expect(toast.success).not.toHaveBeenCalled();
});

it("if the administrators changed before the owner confirmed, the dialog names the new ones and asks again", async () => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshotOf([appWith({ credentials: [keyWithAdmins] })]));
  vi.mocked(accountApi.keepKey).mockRejectedValue(
    new ApiError("keep_needs_confirmation", "Administrators at the time could also have made this key.", 409, {
      error: { code: "keep_needs_confirmation", message: "", administrators: { people: ["Sam"], unnamed_people: 0, admin_keys: 0 } },
    }),
  );
  renderApps();

  const keys = await openKeys("Hall display");
  fireEvent.click(within(keys).getByRole("button", { name: "Keep key Old desk key" }));
  const dialog = await screen.findByRole("dialog", { name: "Keep this key?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep key" }));

  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "The administrators changed. Read them again, then keep it.",
  );
  expect(within(dialog).getByText(confirmBody("Sam"))).toBeVisible();
});

it("asks the owner to confirm the administrators before keeping an address they could have added", async () => {
  vi.mocked(adminFetch).mockImplementation(async () => ({ trusted_addresses: [addressWithAdmins] }));
  vi.mocked(accountApi.session).mockResolvedValue(
    snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })], "owner"),
  );
  renderApps(owner);

  await openTrustedAddresses("Hall display");
  await screen.findByRole("dialog", { name: "Keyless access for Hall display" });
  fireEvent.click(await screen.findByRole("button", { name: "Keep address 10.0.0.7" }));

  const dialog = await screen.findByRole("dialog", { name: "Keep this address?" });
  expect(within(dialog).getByText(confirmBody(NAMED_IN_THE_BODY))).toBeVisible();
  expect(within(dialog).getByRole("button", { name: "Keep address" })).toBeEnabled();
  expect(accountApi.keepTrustedAddress).not.toHaveBeenCalled();
});

it("confirming keeps the address with the owner's confirmation, and cancelling keeps nothing", async () => {
  const before = snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 1 })], "owner");
  const after = snapshotOf([appWith({ credentials: [ownKey], addresses_to_check: 0 })], "owner");
  const gateway = gatewayAnswers(before, after);
  let addresses: Address[] = [addressWithAdmins];
  vi.mocked(adminFetch).mockImplementation(async () => ({ trusted_addresses: addresses }));
  vi.mocked(accountApi.keepTrustedAddress).mockImplementation(async () => {
    addresses = [{ ...unkeptAddress, created_by: "person-1", cutover: undefined }];
    gateway.markKept();
  });
  renderApps(owner);

  await openTrustedAddresses("Hall display");
  await screen.findByRole("dialog", { name: "Keyless access for Hall display" });
  fireEvent.click(await screen.findByRole("button", { name: "Keep address 10.0.0.7" }));
  const dialog = await screen.findByRole("dialog", { name: "Keep this address?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keep this address?" })).toBeNull());
  expect(accountApi.keepTrustedAddress).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Keep address 10.0.0.7" }));
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Keep this address?" })).getByRole("button", { name: "Keep address" }));

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keep this address?" })).toBeNull());
  expect(accountApi.keepTrustedAddress).toHaveBeenCalledWith("hall", "tad-1", { confirmed: true });
  expect(toast.success).toHaveBeenCalledWith("Address kept");
});
