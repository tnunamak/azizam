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
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AppsPage from "./page";
import type { Credential, MintedCredential } from "@/lib/api";
import {
  accountApi,
  type AccountApp,
  type AccountSnapshot,
} from "@/lib/account-api";

const navigation = vi.hoisted(() => ({ search: "" }));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(navigation.search),
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

beforeEach(() => {
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
});

function renderApps() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AppsPage />
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
  expect(screen.getByLabelText("App recipe")).toHaveTextContent("Other app");
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
