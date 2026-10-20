import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
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
import { api, ApiError, type Credential } from "@/lib/api";
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
    createApp: vi.fn(),
    mintKey: vi.fn(),
    revokeKey: vi.fn(),
    renameApp: vi.fn(),
    renameKey: vi.fn(),
    mcpConnections: vi.fn(),
    revokeMcpConnection: vi.fn(),
  },
}));

const NOW = Math.floor(Date.now() / 1000);
const keyOne: Credential = {
  id: "key-1",
  subject_kind: "app",
  subject_id: "saved-app",
  app_id: "saved-app",
  prefix: "opk_AbCdEfGhIjKlMnOp",
  display_name: "Desk key",
  scopes: [],
  created_at: NOW - 86400,
  created_by: "member",
  expires_at: null,
  revoked_at: null,
  last_used_at: NOW - 3600,
};
const keyTwo: Credential = {
  ...keyOne,
  id: "key-2",
  prefix: "opk_QrStUvWxYzAbCdEf",
  display_name: "Travel key",
  last_used_at: null,
};
const app: AccountApp = {
  id: "saved-app",
  name: "My laptop",
  kind: "app",
  disabled_at: null,
  created_at: NOW - 86400,
  access_plan: null,
  can_mint_keys: true,
  credentials: [keyOne, keyTwo],
};
const snapshot: AccountSnapshot = {
  authenticated: true,
  csrf_token: "test",
  apps: [app],
};
const member: Viewer = {
  authenticated: true,
  name: "Sam",
  email: null,
  role: "member",
  admin: false,
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
  vi.mocked(accountApi.session).mockReset().mockResolvedValue(snapshot);
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  vi.mocked(accountApi.mcpConnections).mockReset().mockResolvedValue([]);
  vi.mocked(accountApi.renameApp).mockReset();
  vi.mocked(accountApi.renameKey).mockReset();
  vi.mocked(toast.success).mockClear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

function renderApps() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ViewerContext.Provider value={member}>
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
  return trigger;
}

/** After a save the page asks the gateway again, which is slow here: the list
 * must already show the answer to the save. */
function holdRefetch() {
  vi.mocked(accountApi.session).mockReturnValue(new Promise(() => {}));
}

const refusals = [
  ["app_name_taken", 409, "You already have an app called Kitchen."],
  ["app_name_too_long", 400, "An app name can have at most 80 characters."],
  ["app_name_not_allowed", 400, "An app name cannot have control characters."],
  ["app_name_reserved", 409, "The name Try belongs to the Try page."],
] as const;

it("puts Rename app first in the actions menu", async () => {
  renderApps();
  const trigger = await screen.findByRole("button", {
    name: "Actions for My laptop",
  });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "Enter" });
  const items = await screen.findAllByRole("menuitem");
  expect(items.map((item) => item.textContent).slice(0, 3)).toEqual([
    "Rename app",
    "Rename keys",
    "Create another key",
  ]);
});

it("renames an app with the trimmed name and shows the name the gateway saved", async () => {
  renderApps();
  vi.mocked(accountApi.renameApp).mockResolvedValue({
    ...app,
    name: "Kitchen tablet (saved)",
  });
  const trigger = await chooseFromMenu("My laptop", "Rename app");
  expect(
    await screen.findByRole("heading", { name: "Rename My laptop" }),
  ).toBeVisible();
  const input = screen.getByRole("textbox", { name: "App name" }) as HTMLInputElement;
  expect(input).toHaveValue("My laptop");
  expect(input).toHaveFocus();
  expect(input.selectionStart).toBe(0);
  expect(input.selectionEnd).toBe("My laptop".length);
  expect(input).toHaveAttribute("maxlength", "80");
  expect(input).toBeRequired();
  expect(screen.getByText("Up to 80 characters.")).toBeVisible();
  expect(input).toHaveAccessibleDescription("Up to 80 characters.");
  expect(document.body.textContent).not.toMatch(/receipt/i);

  await userEvent.clear(input);
  await userEvent.type(input, "  Kitchen tablet  ");
  holdRefetch();
  await userEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() =>
    expect(accountApi.renameApp).toHaveBeenCalledWith("saved-app", "Kitchen tablet"),
  );
  expect(
    await screen.findByRole("heading", { name: "Kitchen tablet (saved)" }),
  ).toBeVisible();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(toast.success).toHaveBeenCalledWith("App renamed");
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Actions for Kitchen tablet (saved)" }),
    ).toHaveFocus(),
  );
  expect(trigger).toBe(
    screen.getByRole("button", { name: "Actions for Kitchen tablet (saved)" }),
  );
});

it("submits the new app name with Enter", async () => {
  renderApps();
  vi.mocked(accountApi.renameApp).mockResolvedValue({ ...app, name: "Den" });
  await chooseFromMenu("My laptop", "Rename app");
  const input = await screen.findByRole("textbox", { name: "App name" });
  await userEvent.clear(input);
  await userEvent.type(input, "Den{Enter}");
  await waitFor(() =>
    expect(accountApi.renameApp).toHaveBeenCalledWith("saved-app", "Den"),
  );
});

it("closes the app dialog with Esc or Cancel without sending, and returns focus to the actions button", async () => {
  renderApps();
  await chooseFromMenu("My laptop", "Rename app");
  const input = await screen.findByRole("textbox", { name: "App name" });
  await userEvent.type(input, " extra");
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    screen.getByRole("button", { name: "Actions for My laptop" }),
  ).toHaveFocus();
  await chooseFromMenu("My laptop", "Rename app");
  await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    screen.getByRole("button", { name: "Actions for My laptop" }),
  ).toHaveFocus();
  expect(accountApi.renameApp).not.toHaveBeenCalled();
});

it("keeps Save off while the app name is unchanged, empty or only spaces", async () => {
  renderApps();
  await chooseFromMenu("My laptop", "Rename app");
  const input = await screen.findByRole("textbox", { name: "App name" });
  const save = screen.getByRole("button", { name: "Save" });
  expect(save).toBeDisabled();
  await userEvent.type(input, "{Backspace}");
  expect(save).toBeEnabled();
  await userEvent.type(input, "p");
  expect(save).toBeDisabled();
  await userEvent.clear(input);
  expect(save).toBeDisabled();
  await userEvent.type(input, "    ");
  expect(save).toBeDisabled();
  await userEvent.type(input, "x");
  expect(save).toBeEnabled();
  await userEvent.keyboard("{Enter}");
  await userEvent.clear(input);
  await userEvent.keyboard("{Enter}");
  expect(accountApi.renameApp).toHaveBeenCalledTimes(1);
});

it.each(refusals)(
  "shows the gateway's words for %s, keeps the dialog open and the name editable",
  async (code, status, message) => {
    renderApps();
    vi.mocked(accountApi.renameApp).mockRejectedValue(
      new ApiError(code, message, status),
    );
    await chooseFromMenu("My laptop", "Rename app");
    const input = await screen.findByRole("textbox", { name: "App name" });
    await userEvent.type(input, " 2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(input).toHaveAccessibleDescription(new RegExp(message));
    expect(input).toHaveAttribute("aria-invalid", "true");
    await waitFor(() => expect(input).toHaveFocus());
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(toast.success).not.toHaveBeenCalled();
    // The list still shows the old name.
    expect(
      screen.getByRole("heading", { name: "My laptop", hidden: true }),
    ).toBeInTheDocument();
    // Editing clears the refusal.
    await userEvent.type(input, "3");
    expect(screen.queryByRole("alert")).toBeNull();
  },
);

it("edits the stored name of a legacy personal app, not the shortened display name", async () => {
  const legacy = { ...app, name: "sam@example.test personal access", kind: "personal" };
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    user: {
      id: "u1",
      email: "sam@example.test",
      name: "Sam",
      role: "member",
    },
    apps: [legacy],
  });
  renderApps();
  await chooseFromMenu("Personal access", "Rename app");
  expect(
    await screen.findByRole("heading", { name: "Rename Personal access" }),
  ).toBeVisible();
  expect(screen.getByRole("textbox", { name: "App name" })).toHaveValue(
    "sam@example.test personal access",
  );
});

it("lists an app's keys with a masked start and when each was last used", async () => {
  renderApps();
  await chooseFromMenu("My laptop", "Rename keys");
  const dialog = await screen.findByRole("dialog", { name: "Keys for My laptop" });
  const rows = within(dialog).getAllByRole("listitem");
  expect(rows).toHaveLength(2);
  expect(within(rows[0]).getByText("Desk key")).toBeVisible();
  expect(within(rows[0]).getByText("opk_AbCd…")).toBeVisible();
  expect(rows[0]).toHaveTextContent(/Last used/);
  expect(within(rows[1]).getByText("Travel key")).toBeVisible();
  expect(rows[1]).toHaveTextContent("Never used");
  expect(dialog.textContent).not.toContain("EfGhIjKlMnOp");
  expect(dialog.textContent?.toLowerCase()).not.toContain("credential");
  expect(
    within(rows[0]).getByRole("button", { name: "Rename key Desk key" }),
  ).toBeVisible();
});

it("disables Rename keys for an app with no keys", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, credentials: [] }],
  });
  renderApps();
  const trigger = await screen.findByRole("button", {
    name: "Actions for My laptop",
  });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "Enter" });
  expect(
    await screen.findByRole("menuitem", { name: "Rename keys" }),
  ).toHaveAttribute("aria-disabled", "true");
});

it("renames a key inline, shows the label the gateway saved and returns focus to its Rename button", async () => {
  renderApps();
  vi.mocked(accountApi.renameKey).mockResolvedValue({
    ...keyOne,
    display_name: "Office key (saved)",
  });
  await chooseFromMenu("My laptop", "Rename keys");
  const dialog = await screen.findByRole("dialog", { name: "Keys for My laptop" });
  await userEvent.click(
    within(dialog).getByRole("button", { name: "Rename key Desk key" }),
  );
  const input = within(dialog).getByRole("textbox", { name: "Key name" });
  expect(input).toHaveValue("Desk key");
  expect(input).toHaveFocus();
  expect(input).toHaveAttribute("maxlength", "80");
  // One key is edited at a time.
  expect(
    within(dialog).getByRole("button", { name: "Rename key Travel key" }),
  ).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
  await userEvent.clear(input);
  holdRefetch();
  await userEvent.type(input, "  Office key{Enter}");
  await waitFor(() =>
    expect(accountApi.renameKey).toHaveBeenCalledWith("key-1", "Office key"),
  );
  expect(await within(dialog).findByText("Office key (saved)")).toBeVisible();
  expect(within(dialog).queryByRole("textbox")).toBeNull();
  expect(toast.success).toHaveBeenCalledWith("Key renamed");
  await waitFor(() =>
    expect(
      within(dialog).getByRole("button", { name: "Rename key Office key (saved)" }),
    ).toHaveFocus(),
  );
});

it("shows a key's refusal under that row and keeps the edit open", async () => {
  renderApps();
  vi.mocked(accountApi.renameKey).mockRejectedValue(
    new ApiError(
      "key_label_taken",
      "Another key on this app is already called Travel key.",
      409,
    ),
  );
  await chooseFromMenu("My laptop", "Rename keys");
  const dialog = await screen.findByRole("dialog", { name: "Keys for My laptop" });
  await userEvent.click(
    within(dialog).getByRole("button", { name: "Rename key Desk key" }),
  );
  const input = within(dialog).getByRole("textbox", { name: "Key name" });
  await userEvent.clear(input);
  await userEvent.type(input, "Travel key{Enter}");
  const row = within(dialog).getAllByRole("listitem")[0];
  const alert = await within(row).findByRole("alert");
  expect(alert).toHaveTextContent("Another key on this app is already called Travel key.");
  expect(input).toHaveAccessibleDescription(/already called Travel key/);
  expect(within(dialog).getAllByRole("alert")).toHaveLength(1);
  await waitFor(() => expect(input).toHaveFocus());
  expect(toast.success).not.toHaveBeenCalled();
  expect(within(dialog).getByRole("button", { name: "Save" })).toBeEnabled();
});

it("Esc leaves the inline edit first, a second Esc closes the keys dialog", async () => {
  renderApps();
  await chooseFromMenu("My laptop", "Rename keys");
  const dialog = await screen.findByRole("dialog", { name: "Keys for My laptop" });
  await userEvent.click(
    within(dialog).getByRole("button", { name: "Rename key Desk key" }),
  );
  await userEvent.type(
    within(dialog).getByRole("textbox", { name: "Key name" }),
    " changed",
  );
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: "Keys for My laptop" })).toBeVisible();
  expect(within(dialog).queryByRole("textbox")).toBeNull();
  await waitFor(() =>
    expect(
      within(dialog).getByRole("button", { name: "Rename key Desk key" }),
    ).toHaveFocus(),
  );
  expect(accountApi.renameKey).not.toHaveBeenCalled();
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    screen.getByRole("button", { name: "Actions for My laptop" }),
  ).toHaveFocus();
});

it("wraps a very long key name and gives it a title", async () => {
  const long = "k".repeat(80);
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, credentials: [{ ...keyOne, display_name: long }] }],
  });
  renderApps();
  await chooseFromMenu("My laptop", "Rename keys");
  const label = await screen.findByText(long);
  expect(label).toHaveAttribute("title", long);
  expect(label.className).toContain("break-words");
});
