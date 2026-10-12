import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import type { MemberPromptMemory } from "@/lib/prompt-memory";

const account = vi.hoisted(() => ({
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  promptMemory: vi.fn(),
  params: new URLSearchParams(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => account.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));

const models = {
  aliases: [],
  models: [
    {
      id: "image-model",
      name: "Picture model",
      capabilities: [{ intent: "image.generate", input_modalities: ["text"] }],
      capability: "image",
      locality: "local",
    },
  ],
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const PICTURE =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";
const SUBMIT = "/gateway/v1/media/jobs/image-generations";
const fetchMock = vi.fn();

const member: Viewer = {
  authenticated: true,
  id: "usr_robin",
  name: "Robin",
  email: "robin@example.test",
  role: "member",
  admin: false,
};

function memory(
  overrides: Partial<MemberPromptMemory> = {},
  household: Partial<MemberPromptMemory["household"]> = {},
): { prompt_memory: MemberPromptMemory } {
  return {
    prompt_memory: {
      remember: true,
      retention: "never",
      effective_remember: true,
      effective_retention: "never",
      ...overrides,
      household: {
        default_remember: true,
        never_keep: false,
        retention_cap: "never",
        ...household,
      },
    },
  };
}

function submittedBody() {
  const submit = fetchMock.mock.calls.find(([url]) => url === SUBMIT);
  return JSON.parse((submit?.[1] as RequestInit).body as string);
}

function renderTry(viewer: Viewer = member) {
  return render(
    <ViewerContext.Provider value={viewer}>
      <TryPage />
    </ViewerContext.Provider>,
  );
}

async function ready() {
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Picture model"),
  );
}

beforeEach(() => {
  sessionStorage.clear();
  account.params = new URLSearchParams("capability=image");
  account.session.mockResolvedValue({
    authenticated: true,
    apps: [{ id: "try", name: "Try" }],
  });
  account.tryKey.mockResolvedValue({ id: "try-key", api_key: "test-key", reused: false, app_name: "Try", expires_at: 0 });
  account.promptMemory.mockResolvedValue(memory());
  fetchMock.mockReset().mockImplementation(async (url: string) => {
    if (url === SUBMIT)
      return json({ object: "media.job", id: "job_1", status: "queued" }, 202);
    if (url === "/gateway/v1/media/jobs/job_1")
      return json({ object: "media.job", id: "job_1", status: "succeeded" });
    if (url === "/gateway/v1/media/jobs/job_1/content")
      return json({ data: [{ url: PICTURE }] });
    if (url.startsWith("/me/api/media?")) return json({ assets: [] });
    if (url.includes("/me/api/receipts/")) return json({}, 404);
    return json(url.includes("workflows") ? { workflows: [] } : models);
  });
  vi.stubGlobal("fetch", fetchMock);
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

describe("Try: Don't keep this prompt", () => {
  it("shows what happens to the prompt before Run, and keeps it by default", async () => {
    const user = userEvent.setup();
    renderTry();
    await ready();
    const toggle = await screen.findByRole("switch", {
      name: "Don’t keep this prompt",
    });
    expect(toggle).not.toBeChecked();
    expect(
      screen.getByText(
        "Azizam tries to keep the prompt with the picture.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByAltText("Generated result", {}, { timeout: 5000 });
    expect(submittedBody().extensions).toBeUndefined();
    expect(screen.queryByText("Prompt not kept")).toBeNull();
  });

  it("sends the forget control, says so on the result and remembers it for the session", async () => {
    const user = userEvent.setup();
    renderTry();
    await ready();
    await user.click(
      await screen.findByRole("switch", { name: "Don’t keep this prompt" }),
    );
    expect(
      screen.getByText("The picture still goes to Library."),
    ).toBeVisible();
    expect(sessionStorage.getItem("azizam.try.forget")).toBe("1");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByAltText("Generated result", {}, { timeout: 5000 });
    expect(submittedBody().extensions).toEqual({
      "com.azizam.prompts": { mode: "forget" },
    });
    expect(screen.getByText("Prompt not kept")).toBeVisible();

    cleanup();
    renderTry();
    await ready();
    expect(
      await screen.findByRole("switch", { name: "Don’t keep this prompt" }),
    ).toBeChecked();
  });

  it("offers no switch when the person turned memory off", async () => {
    const user = userEvent.setup();
    account.promptMemory.mockResolvedValue(
      memory({ remember: false, effective_remember: false }),
    );
    renderTry();
    await ready();
    const row = (await screen.findByText("Prompts aren’t kept")).closest(
      "[data-slot=prompt-keep]",
    ) as HTMLElement;
    expect(within(row).getByText(/You turned off prompt keeping/)).toBeVisible();
    expect(within(row).getByRole("link", { name: "Change" })).toHaveAttribute(
      "href",
      "/account",
    );
    expect(screen.queryByRole("switch")).toBeNull();
    expect(
      screen.getByText(
        "Jobs whose prompts aren’t kept cannot resume after a gateway restart.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByAltText("Generated result", {}, { timeout: 5000 });
    expect(submittedBody().extensions).toEqual({
      "com.azizam.prompts": { mode: "forget" },
    });
    expect(screen.getByText("Prompt not kept")).toBeVisible();
  });

  it("names the household's switch-off for a member, and links the owner to Settings", async () => {
    account.promptMemory.mockResolvedValue(
      memory({ effective_remember: false }, { never_keep: true }),
    );
    renderTry();
    await ready();
    expect(
      await screen.findByText("Turned off for everyone on this gateway."),
    ).toBeVisible();
    cleanup();
    renderTry({ ...member, role: "owner", admin: true });
    await ready();
    expect(
      await screen.findByRole("link", { name: "Settings" }),
    ).toHaveAttribute("href", "/settings#prompts");
  });

  it("is not offered where nothing is kept, or to someone signed out", async () => {
    account.params = new URLSearchParams("capability=chat");
    renderTry();
    await waitFor(() => expect(account.promptMemory).toHaveBeenCalled());
    expect(screen.queryByText("Don’t keep this prompt")).toBeNull();
    cleanup();
    account.promptMemory.mockClear();
    account.params = new URLSearchParams("capability=image");
    renderTry({ ...member, authenticated: false });
    await ready();
    expect(account.promptMemory).not.toHaveBeenCalled();
    expect(screen.queryByText("Don’t keep this prompt")).toBeNull();
  });
});

it("honors the displayed off snapshot when Run refresh finds memory on", async () => {
  const user = userEvent.setup();
  account.promptMemory
    .mockResolvedValueOnce(
      memory({ remember: false, effective_remember: false }),
    )
    .mockResolvedValue(memory());
  renderTry();
  await ready();
  await screen.findByText("Prompts aren’t kept");
  await user.click(screen.getByRole("button", { name: "Run" }));
  await screen.findByAltText("Generated result", {}, { timeout: 5000 });
  expect(
    screen.getByText("The picture still goes to Library."),
  ).toBeVisible();
  expect(account.promptMemory).toHaveBeenCalledTimes(2);
  expect(submittedBody().extensions).toEqual({
    "com.azizam.prompts": { mode: "forget" },
  });
  expect(screen.getByText("Prompt not kept")).toBeVisible();
});

it("refreshes prompt settings on window focus", async () => {
  renderTry();
  await ready();
  await screen.findByText(
    "Azizam tries to keep the prompt with the picture.",
  );
  account.promptMemory.mockResolvedValue(
    memory({ effective_remember: false }, { never_keep: true }),
  );
  window.dispatchEvent(new Event("focus"));
  expect(await screen.findByText("Prompts aren’t kept")).toBeVisible();
});

it("explains unreadable prompt settings while preserving the privacy fact", async () => {
  account.promptMemory.mockRejectedValue(new Error("Unavailable"));
  renderTry();
  await ready();
  expect(
    await screen.findByText(
      "Couldn't read your prompt settings. Saved prompts are visible only to you.",
    ),
  ).toBeVisible();
  expect(
    screen.getByRole("switch", { name: "Don’t keep this prompt" }),
  ).toBeVisible();
});

it("puts keeping and privacy facts on separate short lines", async () => {
  renderTry();
  await ready();
  const keeping = await screen.findByText(
    "Azizam tries to keep the prompt with the picture.",
  );
  const privacy = screen.getByText("Only you can see kept prompts.");
  expect(keeping).toBeVisible();
  expect(privacy).toBeVisible();
  expect(keeping).toHaveClass("block");
  expect(privacy).toHaveClass("block");
  expect(keeping.parentElement).toBe(privacy.parentElement);
});

it("names prompt keeping when the owner turned it off for everyone", async () => {
  account.promptMemory.mockResolvedValue(
    memory({ effective_remember: false }, { never_keep: true }),
  );
  renderTry({ ...member, role: "owner", admin: true });
  await ready();
  expect(
    await screen.findByText(/You turned off prompt keeping for everyone\./),
  ).toBeVisible();
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
    "href",
    "/settings#prompts",
  );
});
