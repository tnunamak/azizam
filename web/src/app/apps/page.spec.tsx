import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AppsPage from "./page";
import type { Credential } from "@/lib/api";
import {
  accountApi,
  type AccountApp,
  type AccountSnapshot,
} from "@/lib/account-api";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    session: vi.fn(),
    discovery: vi.fn(),
    createApp: vi.fn(),
    mintKey: vi.fn(),
  },
}));

const app: AccountApp = {
  id: "saved-app",
  name: "My laptop",
  kind: "app",
  disabled_at: null,
  created_at: 1800000000,
  profile: null,
  can_mint_keys: true,
  credentials: [],
};
const snapshot: AccountSnapshot = {
  authenticated: true,
  csrf_token: "test",
  apps: [],
};

beforeEach(() => {
  vi.mocked(accountApi.session).mockReset().mockResolvedValue(snapshot);
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  vi.mocked(accountApi.createApp).mockReset().mockResolvedValue(app);
  vi.mocked(accountApi.mintKey)
    .mockReset()
    .mockRejectedValue(new Error("Key creation failed"));
});
afterEach(cleanup);

function renderApps() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AppsPage />
    </QueryClientProvider>,
  );
}

it("shows one prompt memory notice for legacy keys without a creator", async () => {
  const oldKey = {
    id: "old-key",
    created_by: null,
    last_used_at: null,
  } as Credential;
  vi.mocked(accountApi.session).mockResolvedValue({
    ...snapshot,
    apps: [{ ...app, credentials: [oldKey, { ...oldKey, id: "second-key" }] }],
  });
  renderApps();
  expect(
    await screen.findAllByText(/Older keys with no recorded creator/),
  ).toHaveLength(1);
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
    screen.getByRole("heading", { name: "My laptop" }).closest("article")!,
  );
  fireEvent.click(card.getByRole("button", { name: "Create key" }));
  expect(screen.getByLabelText("App name")).toHaveValue("My laptop");
  vi.mocked(accountApi.mintKey).mockResolvedValueOnce({
    id: "new-key",
    subject_kind: "user",
    subject_id: "member",
    app_id: app.id,
    profile_id: null,
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
    screen.getByRole("heading", { name: "My laptop" }).closest("article")!,
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
    profile_id: null,
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
