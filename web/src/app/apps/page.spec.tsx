import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import AppsPage from "./page";
import { api, type AccessPlan, type Credential, type MintedCredential } from "@/lib/api";
import { productApi } from "@/lib/product-api";
import type { ModelsCatalog } from "@/lib/product-api-types";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import {
  accountApi,
  type AccountApp,
  type AccountSnapshot,
} from "@/lib/account-api";

const navigation = vi.hoisted(() => ({ search: "" }));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(navigation.search),
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
    mcpConnections: vi.fn(),
    revokeMcpConnection: vi.fn(),
  },
}));

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

const member: Viewer = {
  authenticated: true,
  name: "Sam",
  email: null,
  role: "member",
  admin: false,
};
const owner: Viewer = { ...member, name: "Tim", role: "owner", admin: true };
const familyPlan = { id: "plan_family", name: "Family", summary: "Chat and Images." };
const plans = [
  { id: "plan_family", name: "Family", summary: "Chat and Images." },
  { id: "plan_local", name: "Local only", summary: "Chat. No paid models." },
] as AccessPlan[];
const catalog = {
  aliases: [
    { id: "house-chat", capability: "chat", capabilities: [{ capability: "chat" }], recommended: true },
    { id: "house-whisper", capability: "transcribe", capabilities: [{ capability: "transcribe" }] },
  ],
  models: [],
} as unknown as ModelsCatalog;

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});

beforeEach(() => {
  vi.spyOn(api.accessPlans, "list").mockReset().mockResolvedValue(plans);
  vi.spyOn(api.apps, "setAccessPlan").mockReset().mockResolvedValue({} as never);
  vi.spyOn(productApi, "models").mockReset().mockResolvedValue(catalog);
  navigation.search = "";
  vi.mocked(accountApi.session).mockReset().mockResolvedValue(snapshot);
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  vi.mocked(accountApi.mcpConnections).mockReset().mockResolvedValue([]);
  vi.mocked(accountApi.revokeMcpConnection).mockReset().mockResolvedValue({});
  vi.mocked(accountApi.createApp).mockReset().mockResolvedValue(app);
  vi.mocked(accountApi.mintKey)
    .mockReset()
    .mockRejectedValue(new Error("Key creation failed"));
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

function renderApps(viewer: Viewer = member) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={viewer}>
        <AppsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  return client;
}

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
  api_key: "test-key-only",
};

function openMenu(appName: string) {
  const trigger = screen.getByRole("button", {
    name: `Actions for ${appName}`,
  });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "Enter" });
}

it("marks only the personal app whose key has no recorded creator, with a Replace action", async () => {
  const oldKey = {
    id: "old-key",
    created_by: null,
    last_used_at: null,
  } as Credential;
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [
      {
        ...app,
        kind: "personal",
        credentials: [oldKey, { ...oldKey, id: "second-key" }],
      },
      {
        ...app,
        id: "fresh-app",
        name: "Phone",
        kind: "personal",
        credentials: [{ ...oldKey, id: "fresh-key", created_by: "member" }],
      },
    ],
  });
  renderApps();
  const row = within(
    (await screen.findByRole("heading", { name: "My laptop" })).closest("li")!,
  );
  expect(row.getByText(/Needs a new key/)).toBeVisible();
  const fresh = within(
    screen.getByRole("heading", { name: "Phone" }).closest("li")!,
  );
  expect(fresh.queryByText(/Needs a new key/)).toBeNull();
  expect(fresh.queryByRole("button", { name: "Replace key" })).toBeNull();
  expect(screen.queryByText(/Older keys with no recorded creator/)).toBeNull();
  fireEvent.click(row.getByRole("button", { name: "Replace key" }));
  expect(
    screen.getByRole("heading", { name: "Rotate keys for My laptop?" }),
  ).toBeVisible();
});

it("does not mark household app keys that lack a creator", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [
      {
        ...app,
        credentials: [{ id: "old-key", created_by: null } as Credential],
      },
    ],
  });
  renderApps();
  await screen.findByRole("heading", { name: "My laptop" });
  expect(screen.queryByText(/Needs a new key/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Replace key" })).toBeNull();
});

it("lets a member finish a failed rotation later and keeps a notice until old keys are revoked", async () => {
  const withKey = {
    ...app,
    credentials: [{ id: "old-key", created_by: "member" } as Credential],
  };
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [withKey],
  });
  vi.mocked(accountApi.mintKey).mockResolvedValue(mintedKey);
  vi.mocked(accountApi.revokeKey)
    .mockReset()
    .mockRejectedValueOnce(new Error("The gateway did not answer."));
  renderApps();
  await screen.findByRole("heading", { name: "My laptop" });
  openMenu("My laptop");
  fireEvent.click(await screen.findByRole("menuitem", { name: "Rotate keys" }));
  fireEvent.click(
    screen.getByRole("button", { name: "Create replacement key" }),
  );
  fireEvent.click(await screen.findByRole("button", { name: /saved/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Your new key is saved. Some old keys still work. The gateway did not answer.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Finish later" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  const row = within(
    screen.getByRole("heading", { name: "My laptop" }).closest("li")!,
  );
  expect(row.getByText(/Old keys still work/)).toBeVisible();
  expect(localStorage.getItem("azizam:unfinished-key-rotations")).toBe(
    JSON.stringify({ [app.id]: ["old-key"] }),
  );
  vi.mocked(accountApi.revokeKey).mockResolvedValue({} as never);
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...withKey, credentials: [] }],
  });
  fireEvent.click(row.getByRole("button", { name: "Revoke old keys" }));
  await waitFor(() =>
    expect(row.queryByText(/Old keys still work/)).toBeNull(),
  );
  expect(accountApi.revokeKey).toHaveBeenLastCalledWith("old-key");
  expect(localStorage.getItem("azizam:unfinished-key-rotations")).toBeNull();
});

it("does not claim an existing keyless app was just created when its key fails", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, apps: [app] });
  renderApps();
  const row = within(
    (await screen.findByRole("heading", { name: "My laptop" })).closest("li")!,
  );
  fireEvent.click(row.getByRole("button", { name: "Create key" }));
  fireEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "Create key",
    }),
  );
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Key creation failed");
  expect(alert).not.toHaveTextContent("Your app was created");
});

it("says why keys cannot be created for an app", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [
      {
        ...app,
        can_mint_keys: false,
        access_plan: null,
      },
    ],
  });
  renderApps();
  const row = within(
    (await screen.findByRole("heading", { name: "My laptop" })).closest("li")!,
  );
  expect(row.getByText(/Can't create keys/)).toBeVisible();
  expect(row.queryByRole("button", { name: "Create key" })).toBeNull();
  openMenu("My laptop");
  expect(
    await screen.findByText(
      "This app has no access plan. Ask the owner to choose one.",
    ),
  ).toBeVisible();
  expect(screen.getByRole("menuitem", { name: "Create key" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
});

it("shows a row-level wait while a key is created from the menu", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [
      {
        ...app,
        credentials: [{ id: "old-key", created_by: "member" } as Credential],
      },
    ],
  });
  let finish!: (value: MintedCredential) => void;
  vi.mocked(accountApi.mintKey).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  renderApps();
  await screen.findByRole("heading", { name: "My laptop" });
  openMenu("My laptop");
  fireEvent.click(
    await screen.findByRole("menuitem", { name: "Create another key" }),
  );
  const row = within(
    screen.getByRole("heading", { name: "My laptop" }).closest("li")!,
  );
  expect(await row.findByRole("status")).toHaveTextContent("Creating key…");
  finish(mintedKey);
  expect(
    await screen.findByRole("heading", { name: "Connect My laptop" }),
  ).toBeVisible();
  expect(row.queryByRole("status")).toBeNull();
});

it("refreshes a partially created app, clears the cancelled draft, and resumes key creation", async () => {
  renderApps();
  await screen.findByText("Connect your first app");
  fireEvent.click(screen.getAllByRole("button", { name: "New app" })[0]);
  fireEvent.change(screen.getByLabelText("App name"), {
    target: { value: "My laptop" },
  });
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, apps: [app] });
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Your app was created",
  );
  expect(
    await screen.findByRole("heading", { name: "My laptop", hidden: true }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "New app" }));
  expect(screen.getByLabelText("App name")).toHaveValue("");
  expect(screen.queryByLabelText("App recipe")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  const card = within(
    screen.getByRole("heading", { name: "My laptop" }).closest("li")!,
  );
  fireEvent.click(card.getByRole("button", { name: "Create key" }));
  expect(screen.getByLabelText("App name")).toHaveValue("My laptop");
  vi.mocked(accountApi.mintKey).mockResolvedValueOnce({
    id: "new-key",
    subject_kind: "user",
    subject_id: "member",
    app_id: app.id,
      prefix: "test",
    display_name: "My laptop",
    scopes: [],
    created_at: 1800000000,
    expires_at: null,
    revoked_at: null,
    last_used_at: null,
    api_key: "test-key-only",
  });
  fireEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "Create key",
    }),
  );
  expect(
    await screen.findByRole("heading", { name: "Connect My laptop" }),
  ).toBeVisible();
  expect(accountApi.createApp).toHaveBeenCalledTimes(1);
  expect(accountApi.mintKey).toHaveBeenCalledTimes(2);
  expect(accountApi.mintKey).toHaveBeenLastCalledWith("saved-app", "My laptop");
});

it("keeps a newly created app resumable while the session refresh is still pending", async () => {
  let finishRefresh!: (value: AccountSnapshot) => void;
  const pendingRefresh = new Promise<AccountSnapshot>((resolve) => {
    finishRefresh = resolve;
  });
  const previousApp = { ...app, id: "previous-app", name: "Existing app" };
  vi.mocked(accountApi.session)
    .mockResolvedValueOnce({ ...snapshot, apps: [previousApp] })
    .mockReturnValue(pendingRefresh);
  renderApps();
  await screen.findByRole("heading", { name: "Existing app" });
  fireEvent.click(screen.getByRole("button", { name: "New app" }));
  fireEvent.change(screen.getByLabelText("App name"), {
    target: { value: "My laptop" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Your app was created",
  );
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("heading", { name: "Existing app" })).toBeVisible();
  const createdCard = within(
    screen.getByRole("heading", { name: "My laptop" }).closest("li")!,
  );
  fireEvent.click(createdCard.getByRole("button", { name: "Create key" }));
  fireEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "Create key",
    }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Key creation failed",
  );
  expect(accountApi.createApp).toHaveBeenCalledTimes(1);
  expect(accountApi.mintKey).toHaveBeenCalledTimes(2);
  expect(accountApi.mintKey).toHaveBeenLastCalledWith(app.id, app.name);
  finishRefresh({ ...snapshot, apps: [previousApp, app] });
});

it("says what a new key does with prompts, from the gateway's answer", async () => {
  renderApps();
  await screen.findByText("Connect your first app");
  fireEvent.click(screen.getAllByRole("button", { name: "New app" })[0]);
  fireEvent.change(screen.getByLabelText("App name"), {
    target: { value: "My laptop" },
  });
  vi.mocked(accountApi.mintKey).mockResolvedValueOnce({
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
    api_key: "test-key-only",
    prompt_memory: { person_id: "member" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  expect(
    await screen.findByText(
      "This key acts as you. Anyone who has it can see the prompts you saved.",
    ),
  ).toBeVisible();
});

it("names an app's access plan on its row instead of showing a bare word", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, access_plan: { id: "plan_old", name: "Old", summary: null, same_as_person: true } }],
  });
  renderApps();
  const row = within(
    (await screen.findByRole("heading", { name: "My laptop" })).closest("li")!,
  );
  expect(row.getByText(/Access plan: Old \(yours\)/)).toBeVisible();
});

const unfinishedKey = "azizam:unfinished-key-rotations";
const twoKeys = {
  ...app,
  credentials: [
    { id: "old-1", created_by: "member" } as Credential,
    { id: "old-2", created_by: "member" } as Credential,
  ],
};

async function startRotation() {
  await screen.findByRole("heading", { name: "My laptop" });
  openMenu("My laptop");
  fireEvent.click(await screen.findByRole("menuitem", { name: "Rotate keys" }));
  fireEvent.click(
    screen.getByRole("button", { name: "Create replacement key" }),
  );
  fireEvent.click(await screen.findByRole("button", { name: /saved/i }));
}

it("keeps Finish later usable when the browser refuses to store the reminder", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [twoKeys],
  });
  vi.mocked(accountApi.mintKey).mockResolvedValue(mintedKey);
  vi.mocked(accountApi.revokeKey)
    .mockReset()
    .mockRejectedValue(new Error("The gateway did not answer."));
  renderApps();
  await startRotation();
  await screen.findByRole("alert");
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  fireEvent.click(screen.getByRole("button", { name: "Finish later" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  const row = within(
    screen.getByRole("heading", { name: "My laptop" }).closest("li")!,
  );
  expect(row.getByText(/Old keys still work/)).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent(
    /could not save the reminder/,
  );
  vi.restoreAllMocks();
});

it("still revokes old keys when clearing the stored reminder fails", async () => {
  localStorage.setItem(unfinishedKey, JSON.stringify({ [app.id]: ["old-1"] }));
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, credentials: [{ id: "old-1" } as Credential] }],
  });
  vi.mocked(accountApi.revokeKey)
    .mockReset()
    .mockResolvedValue({} as never);
  renderApps();
  const row = within(
    (await screen.findByRole("heading", { name: "My laptop" })).closest("li")!,
  );
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, credentials: [] }],
  });
  fireEvent.click(row.getByRole("button", { name: "Revoke old keys" }));
  await waitFor(() =>
    expect(row.queryByText(/Old keys still work/)).toBeNull(),
  );
  expect(accountApi.revokeKey).toHaveBeenCalledWith("old-1");
  vi.restoreAllMocks();
});

it("ignores malformed stored reminders instead of crashing the page", async () => {
  localStorage.setItem(
    unfinishedKey,
    JSON.stringify({ [app.id]: [1, null, "old-1"], other: "old-2", more: 7 }),
  );
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [
      { ...app, credentials: [{ id: "old-1" } as Credential] },
      {
        ...app,
        id: "other",
        name: "Phone",
        credentials: [{ id: "old-2" } as Credential],
      },
    ],
  });
  renderApps();
  const laptop = within(
    (await screen.findByRole("heading", { name: "My laptop" })).closest("li")!,
  );
  expect(laptop.getByText(/Old keys still work/)).toBeVisible();
  const phone = within(
    screen.getByRole("heading", { name: "Phone" }).closest("li")!,
  );
  expect(phone.queryByText(/Old keys still work/)).toBeNull();
});

it("stores outstanding old key ids, never the secret, before the first revoke and across a reload", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [twoKeys],
  });
  vi.mocked(accountApi.mintKey).mockResolvedValue(mintedKey);
  let release: () => void = () => {};
  vi.mocked(accountApi.revokeKey)
    .mockReset()
    .mockImplementation(
      () => new Promise((resolve) => (release = () => resolve({} as never))),
    );
  renderApps();
  await startRotation();
  await waitFor(() => expect(accountApi.revokeKey).toHaveBeenCalledTimes(1));
  expect(localStorage.getItem(unfinishedKey)).toBe(
    JSON.stringify({ [app.id]: ["old-1", "old-2"] }),
  );
  expect(JSON.stringify({ ...localStorage })).not.toContain("test-key-only");
  cleanup(); // reload while the first revoke is pending
  renderApps();
  const row = within(
    (await screen.findByRole("heading", { name: "My laptop" })).closest("li")!,
  );
  expect(row.getByText(/Old keys still work/)).toBeVisible();
  release();
});

it("keeps only the still-active old key ids when a later revoke fails, and clears them on completion", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [twoKeys],
  });
  vi.mocked(accountApi.mintKey).mockResolvedValue(mintedKey);
  vi.mocked(accountApi.revokeKey)
    .mockReset()
    .mockResolvedValueOnce({} as never)
    .mockRejectedValueOnce(new Error("The gateway did not answer."))
    .mockResolvedValue({} as never);
  renderApps();
  await startRotation();
  await screen.findByRole("alert");
  expect(localStorage.getItem(unfinishedKey)).toBe(
    JSON.stringify({ [app.id]: ["old-2"] }),
  );
  expect(JSON.stringify({ ...localStorage })).not.toContain("test-key-only");
  fireEvent.click(
    screen.getByRole("button", { name: "Retry revoking old keys" }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(localStorage.getItem(unfinishedKey)).toBeNull();
});

it("handles each app deep link once without stealing focus on refresh", async () => {
  navigation.search = `app=${app.id}`;
  const phone = { ...app, id: "phone", name: "Phone" };
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [app, phone],
  });
  const client = renderApps();
  await screen.findByRole("heading", { name: app.name });
  await waitFor(() =>
    expect(document.getElementById(`app-${app.id}`)).toHaveFocus(),
  );
  const newApp = screen.getByRole("button", { name: "New app" });
  newApp.focus();
  act(() => {
    client.setQueryData(["account", "session"], {
      ...snapshot,
      apps: [{ ...app, name: "Updated laptop" }, phone],
    });
  });
  await screen.findByRole("heading", { name: "Updated laptop" });
  expect(newApp).toHaveFocus();
  navigation.search = "app=phone";
  act(() => {
    client.setQueryData(["account", "session"], {
      ...snapshot,
      apps: [{ ...app, name: "Updated again" }, phone],
    });
  });
  await waitFor(() =>
    expect(document.getElementById("app-phone")).toHaveFocus(),
  );
});

it("keeps focus inside a modal during refresh and returns it on cancel", async () => {
  navigation.search = `app=${app.id}`;
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, apps: [app] });
  const client = renderApps();
  await screen.findByRole("heading", { name: app.name });
  const opener = screen.getByRole("button", { name: "New app" });
  opener.focus();
  fireEvent.click(opener);
  const input = screen.getByRole("textbox", { name: "App name" });
  input.focus();
  act(() => {
    client.setQueryData(["account", "session"], {
      ...snapshot,
      apps: [{ ...app, name: "Updated laptop" }],
    });
  });
  await screen.findByRole("heading", { name: "Updated laptop", hidden: true });
  expect(input).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(opener).toHaveFocus());
});

it("hands focus to the key reveal and returns to app actions if its opener disappears", async () => {
  navigation.search = `app=${app.id}`;
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, apps: [app] });
  vi.mocked(accountApi.mintKey).mockResolvedValue(mintedKey);
  renderApps();
  await screen.findByRole("heading", { name: app.name });
  const opener = screen.getByRole("button", { name: "Create key" });
  opener.focus();
  fireEvent.click(opener);
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, credentials: [mintedKey] }],
  });
  fireEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: "Create key" }),
  );
  await screen.findByRole("heading", { name: "Connect My laptop" });
  await waitFor(() =>
    expect(screen.getByRole("dialog")).toContainElement(
      document.activeElement as HTMLElement,
    ),
  );
  const saved = screen.getByRole("button", { name: /saved/i });
  saved.focus();
  fireEvent.click(saved);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Actions for My laptop" })).toHaveFocus(),
  );
});

it("lists the person's sign-in connections apart from their keyed apps, and disconnects one", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [app],
  });
  vi.mocked(accountApi.mcpConnections).mockResolvedValue([
    {
      id: "mcg_chatgpt",
      client_name: "ChatGPT",
      client_id: "https://chatgpt.example/client.json",
      redirect_host: "chatgpt.example",
      created_at: 1800000000,
      last_used_at: null,
    },
  ]);
  renderApps();
  const section = (
    await screen.findByRole("heading", { name: "Connected with sign-in" })
  ).closest("section")!;
  const list = await within(section).findByRole("list", { name: "Connections" });
  expect(within(list).getByText("ChatGPT")).toBeVisible();
  // The keyed apps stay in their own list; a connection is not an app row.
  expect(
    within(screen.getByRole("list", { name: "Apps" })).queryByText("ChatGPT"),
  ).toBeNull();
  fireEvent.click(
    within(list).getByRole("button", { name: "Disconnect ChatGPT" }),
  );
  await waitFor(() =>
    expect(accountApi.revokeMcpConnection).toHaveBeenCalledWith("mcg_chatgpt"),
  );
});

it("says how to make a connection when there is none", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, apps: [app] });
  renderApps();
  expect(await screen.findByText(/No connections yet/)).toBeVisible();
  expect(screen.getByRole("link", { name: "connect one" })).toHaveAttribute(
    "href",
    "/connect",
  );
});

// --- What an app can use (access plan), and the setup guide in the reveal ---

const person = (role: string) => ({
  id: "person",
  email: null,
  name: "Tim",
  role,
  access_plan: familyPlan,
});
const planAccountApp: AccountApp = {
  ...app,
  id: "planned-app",
  name: "Guide app",
  access_plan: { ...familyPlan, same_as_person: true },
  credentials: [{ id: "k1", created_by: "person" } as Credential],
};

async function openNewApp(name: string) {
  await screen.findByText("Connect your first app");
  fireEvent.click(screen.getAllByRole("button", { name: "New app" })[0]);
  fireEvent.change(screen.getByLabelText("App name"), { target: { value: name } });
}
async function choosePlan(label: string, option: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option }));
}
const minted = (name: string): MintedCredential => ({ ...mintedKey, display_name: name });

it("lets an owner pick an access plan, and sets it before any key is minted", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("owner") });
  vi.mocked(accountApi.createApp).mockResolvedValue({ ...app, id: "new-app", name: "Dictation" });
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(minted("Dictation"));
  renderApps(owner);
  await openNewApp("Dictation");
  const select = await screen.findByRole("combobox", { name: "Access plan" });
  // A key has no permissions of its own: it gets its app's plan.
  expect(
    screen.getByText(/A key can use only what its app.s access plan allows/),
  ).toBeVisible();
  expect(select).toHaveTextContent("Same as you (Family)");
  expect(screen.getByText("Chat and Images.")).toBeVisible();
  await choosePlan("Access plan", "Local only");
  expect(
    screen.getByText("Chat. No paid models. It never gets more than your own plan allows."),
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  expect(await screen.findByRole("heading", { name: "Connect Dictation" })).toBeVisible();
  expect(api.apps.setAccessPlan).toHaveBeenCalledWith("new-app", "plan_local");
  const order = (fn: unknown) => vi.mocked(fn as () => void).mock.invocationCallOrder[0];
  expect(order(accountApi.createApp)).toBeLessThan(order(api.apps.setAccessPlan));
  expect(order(api.apps.setAccessPlan)).toBeLessThan(order(accountApi.mintKey));
  expect(accountApi.mintKey).toHaveBeenCalledWith("new-app", "Dictation");
});

it("does not call the plan endpoint when an owner keeps 'Same as you'", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("owner") });
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(mintedKey);
  renderApps(owner);
  await openNewApp("My laptop");
  fireEvent.click(await screen.findByRole("button", { name: "Create app and key" }));
  await screen.findByRole("heading", { name: "Connect My laptop" });
  expect(api.apps.setAccessPlan).not.toHaveBeenCalled();
});

it("mints no key when the plan cannot be saved, and finishes the app on retry", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("owner") });
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(mintedKey);
  vi.mocked(api.apps.setAccessPlan)
    .mockRejectedValueOnce(new Error("The plan could not be saved."))
    .mockResolvedValue({} as never);
  renderApps(owner);
  await openNewApp("My laptop");
  await choosePlan("Access plan", "Local only");
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Your app was created");
  expect(alert).toHaveTextContent("The plan could not be saved.");
  expect(accountApi.mintKey).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Retry key creation" }));
  expect(await screen.findByRole("heading", { name: "Connect My laptop" })).toBeVisible();
  expect(accountApi.createApp).toHaveBeenCalledTimes(1);
  expect(api.apps.setAccessPlan).toHaveBeenCalledTimes(2);
  expect(accountApi.mintKey).toHaveBeenCalledTimes(1);
});

it("shows a member their plan, read-only, and never sets one", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("member") });
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(mintedKey);
  renderApps(member);
  await openNewApp("My laptop");
  expect(
    await screen.findByText(
      "Same as yours: Family. Chat and Images. Only the owner can change an app's access plan.",
    ),
  ).toBeVisible();
  expect(screen.queryByRole("combobox", { name: "Access plan" })).toBeNull();
  expect(api.accessPlans.list).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  await screen.findByRole("heading", { name: "Connect My laptop" });
  expect(api.apps.setAccessPlan).not.toHaveBeenCalled();
});

it.each([
  ["OpenWhispr laptop", "OpenWhispr"],
  ["My laptop", "Open WebUI"],
])("opens the reveal for %s with the %s guide chosen, and the key only in its own field", async (name, appName) => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("member") });
  vi.mocked(accountApi.createApp).mockResolvedValue({ ...app, id: "guide", name });
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(minted(name));
  renderApps(member);
  await openNewApp(name);
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  const dialog = within(await screen.findByRole("dialog", { name: `Connect ${name}` }));
  expect(dialog.getByRole("combobox", { name: "Your app" })).toHaveTextContent(appName);
  expect(dialog.getByRole("heading", { name: "Set up your app" })).toBeVisible();
  expect(dialog.getByLabelText("Your key")).toHaveValue("test-key-only");
  // The key field of the guide shows a hint, never the key.
  const guide = dialog.getByRole("heading", { name: "Set up your app" }).closest("section")!;
  expect(guide.textContent).toContain("Paste the key from above.");
  const values = [...guide.querySelectorAll("input")].map((input) => input.value);
  expect(values.length).toBeGreaterThan(0);
  expect(values).not.toContain("test-key-only");
  expect(guide.textContent).not.toContain("test-key-only");
  expect(dialog.getByRole("link", { name: "Connect" })).toHaveAttribute("href", expect.stringMatching(/^\/connect\/?$/));
  expect(dialog.getByRole("link", { name: `${appName} documentation` })).toHaveAttribute(
    "href",
    expect.stringMatching(/^https:\/\//),
  );
  // The sticky action saves the key and closes the dialog.
  fireEvent.click(dialog.getByRole("button", { name: "I’ve saved the key" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("fills the guide with the person's own model names, and warns when a task has none", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("member") });
  vi.mocked(accountApi.createApp).mockResolvedValue({ ...app, id: "guide", name: "OpenWhispr laptop" });
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(minted("OpenWhispr laptop"));
  renderApps(member);
  await openNewApp("OpenWhispr laptop");
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Connect OpenWhispr laptop" }));
  await waitFor(() => {
    const models = dialog.getAllByLabelText("Model").map((el) => (el as HTMLInputElement).value);
    expect(models).toEqual(["house-whisper", "house-chat"]);
  });
  expect(dialog.queryByText(/needs a .* model, and none is available/)).toBeNull();
});

it("fills the guide only with names the new app's own plan allows", async () => {
  const narrowed: AccountApp = {
    ...app,
    id: "guide",
    name: "OpenWhispr laptop",
    access_plan: { id: "plan_local", name: "Local only", summary: null, same_as_person: false },
  };
  // No apps at first; the refresh after creating one lists it with its narrower plan.
  vi.mocked(accountApi.session)
    .mockResolvedValueOnce({ ...snapshot, user: person("owner") })
    .mockResolvedValue({ ...snapshot, user: person("owner"), apps: [narrowed] });
  vi.mocked(accountApi.createApp).mockResolvedValue(narrowed);
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(minted("OpenWhispr laptop"));
  const entry = (id: string, state: "allowed" | "blocked" | "unknown") =>
    ({ id, name: id, alias: true, group: "chat", group_label: "Chat", state, reason: null, rule: null, points_to: [], kinds: [] });
  const counts = { usable: 1, total: 2, blocked: 1, unknown: 0 };
  vi.spyOn(api.accessPlans, "preview").mockResolvedValue({
    plan_ids: ["plan_local"],
    summary: counts,
    groups: [{ group: "chat", label: "Chat", ...counts }],
    entries: [entry("house-chat", "blocked"), entry("house-whisper", "allowed")],
  });
  renderApps(owner);
  await openNewApp("OpenWhispr laptop");
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Connect OpenWhispr laptop" }));
  // The person's catalog has house-chat; the app's plan does not, so the guide must not suggest it.
  expect(await dialog.findByText(/needs a .* model, and none is available/)).toBeVisible();
  expect(api.accessPlans.preview).toHaveBeenCalledWith("plan_local");
  const models = dialog.getAllByLabelText("Model").map((el) => (el as HTMLInputElement).value);
  expect(models).toContain("house-whisper");
  expect(models).not.toContain("house-chat");
});

it("does not suggest a name the check could not tell is allowed", async () => {
  const narrowed: AccountApp = {
    ...app,
    id: "guide",
    name: "OpenWhispr laptop",
    access_plan: { id: "plan_local", name: "Local only", summary: null, same_as_person: false },
  };
  // No apps at first; the refresh after creating one lists it with its narrower plan.
  vi.mocked(accountApi.session)
    .mockResolvedValueOnce({ ...snapshot, user: person("owner") })
    .mockResolvedValue({ ...snapshot, user: person("owner"), apps: [narrowed] });
  vi.mocked(accountApi.createApp).mockResolvedValue(narrowed);
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(minted("OpenWhispr laptop"));
  const entry = (id: string, state: "allowed" | "blocked" | "unknown") =>
    ({ id, name: id, alias: true, group: "chat", group_label: "Chat", state, reason: null, rule: null, points_to: [], kinds: [] });
  const counts = { usable: 1, total: 2, blocked: 1, unknown: 0 };
  vi.spyOn(api.accessPlans, "preview").mockResolvedValue({
    plan_ids: ["plan_local"],
    summary: { ...counts, blocked: 0, unknown: 1 },
    groups: [{ group: "chat", label: "Chat", ...counts, blocked: 0, unknown: 1 }],
    entries: [entry("house-chat", "unknown"), entry("house-whisper", "allowed")],
  });
  renderApps(owner);
  await openNewApp("OpenWhispr laptop");
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Connect OpenWhispr laptop" }));
  // The person's catalog has house-chat; the app's plan does not, so the guide must not suggest it.
  expect(await dialog.findByText(/needs a .* model, and none is available/)).toBeVisible();
  expect(api.accessPlans.preview).toHaveBeenCalledWith("plan_local");
  const models = dialog.getAllByLabelText("Model").map((el) => (el as HTMLInputElement).value);
  expect(models).toContain("house-whisper");
  expect(models).not.toContain("house-chat");
});

it("says which model task is missing once the catalog has loaded", async () => {
  vi.mocked(productApi.models).mockResolvedValue({ aliases: [], models: [] } as unknown as ModelsCatalog);
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("member") });
  vi.mocked(accountApi.createApp).mockResolvedValue({ ...app, id: "guide", name: "OpenWhispr laptop" });
  vi.mocked(accountApi.mintKey).mockReset().mockResolvedValue(minted("OpenWhispr laptop"));
  renderApps(member);
  await openNewApp("OpenWhispr laptop");
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Connect OpenWhispr laptop" }));
  expect(
    await dialog.findByText(/OpenWhispr needs a transcription and a chat model, and none is available to you\./),
  ).toBeVisible();
});

it("gives an owner 'Change access plan' that sets the plan and refreshes", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    user: person("owner"),
    apps: [planAccountApp],
  });
  renderApps(owner);
  await screen.findByRole("heading", { name: "Guide app" });
  openMenu("Guide app");
  fireEvent.click(await screen.findByRole("menuitem", { name: "Change access plan" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Access plan for Guide app" }));
  expect(dialog.getByRole("combobox", { name: "Access plan" })).toHaveTextContent("Same as you (Family)");
  await choosePlan("Access plan", "Local only");
  expect(dialog.getByText("Local only. Chat. No paid models.")).toBeVisible();
  const sessionCalls = vi.mocked(accountApi.session).mock.calls.length;
  fireEvent.click(dialog.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(api.apps.setAccessPlan).toHaveBeenCalledWith("planned-app", "plan_local"));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() =>
    expect(vi.mocked(accountApi.session).mock.calls.length).toBeGreaterThan(sessionCalls),
  );
});

it("keeps the change-access dialog open with the error when saving fails", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    user: person("owner"),
    apps: [planAccountApp],
  });
  vi.mocked(api.apps.setAccessPlan).mockRejectedValue(new Error("Only the owner can do that."));
  renderApps(owner);
  await screen.findByRole("heading", { name: "Guide app" });
  openMenu("Guide app");
  fireEvent.click(await screen.findByRole("menuitem", { name: "Change access plan" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Access plan for Guide app" }));
  fireEvent.click(dialog.getByRole("button", { name: "Save" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent("Only the owner can do that.");
});

it("does not offer 'Change access plan' to a member", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    user: person("member"),
    apps: [planAccountApp],
  });
  renderApps(member);
  await screen.findByRole("heading", { name: "Guide app" });
  openMenu("Guide app");
  await screen.findByRole("menuitem", { name: "Create another key" });
  expect(screen.queryByRole("menuitem", { name: "Change access plan" })).toBeNull();
});

it("offers an administrator no way to enable or trust an address on an App, but lets them rename their own", async () => {
  // The page lists the signed-in person's own Apps. Re-enabling and trusted addresses on
  // other people's Apps (the owner's included) answer 403 for an administrator, so
  // neither may appear here. Renaming goes through the person's own routes (/me/api), which
  // accept any signed-in person for their own apps and keys.
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    user: person("admin"),
    apps: [planAccountApp],
  });
  renderApps({ ...owner, name: "Helper", role: "admin" });
  await screen.findByRole("heading", { name: "Guide app" });
  openMenu("Guide app");
  await screen.findByRole("menuitem", { name: "Create another key" });
  for (const name of [/enable/i, /trusted address/i, /undo/i]) {
    expect(screen.queryByRole("menuitem", { name })).toBeNull();
  }
  expect(screen.getByRole("menuitem", { name: "Rename app" })).toBeVisible();
});

it("retains a rejected new app's name and access plan for a safe retry", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, user: person("owner") });
  vi.mocked(accountApi.createApp).mockRejectedValueOnce(new Error("That app name is already in use."));
  vi.mocked(accountApi.mintKey).mockResolvedValue(mintedKey);
  renderApps(owner);
  await openNewApp("   ");
  expect(screen.getByRole("button", { name: "Create app and key" })).toBeDisabled();
  expect(accountApi.createApp).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("App name"), { target: { value: "  My laptop  " } });
  await choosePlan("Access plan", "Local only");
  fireEvent.click(screen.getByRole("button", { name: "Create app and key" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("That app name is already in use.");
  expect(screen.getByRole("alert")).not.toHaveTextContent("Your app was created");
  expect(screen.getByLabelText("App name")).toHaveValue("  My laptop  ");
  expect(screen.getByRole("combobox", { name: "Access plan" })).toHaveTextContent("Local only");
  expect(api.apps.setAccessPlan).not.toHaveBeenCalled();
  expect(accountApi.mintKey).not.toHaveBeenCalled();
  const retry = screen.getByRole("button", { name: "Create app and key" });
  await waitFor(() => expect(retry).toBeEnabled());
  fireEvent.click(retry);
  expect(await screen.findByRole("heading", { name: "Connect My laptop" })).toBeVisible();
  expect(vi.mocked(accountApi.createApp).mock.calls).toEqual([["My laptop"], ["My laptop"]]);
  expect(api.apps.setAccessPlan).toHaveBeenCalledWith(app.id, "plan_local");
  expect(api.apps.setAccessPlan).toHaveBeenCalledTimes(1);
  expect(accountApi.mintKey).toHaveBeenCalledTimes(1);
});

