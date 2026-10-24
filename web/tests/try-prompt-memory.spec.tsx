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
  requestHistory: vi.fn(),
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
  account.requestHistory.mockResolvedValue({
    keep: true,
    retention: "7_days",
    parts: ["accepted"],
    choices: ["1_day", "7_days", "30_days"],
    allowed_parts: ["accepted", "received"],
    notice_due: false,
  });
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

describe("Try: keep request history and prompt keeping", () => {
  it("shows what happens to the prompt before Run, and keeps it by default", async () => {
    const user = userEvent.setup();
    renderTry();
    await ready();
    const toggle = await screen.findByRole("switch", {
      name: "Keep request history",
    });
    expect(toggle).toBeChecked();
    expect(
      screen.getByText(
        "Azizam tries to keep the prompt with the picture.",
      ),
    ).toBeVisible();
    const example = screen.queryByRole("button", { name: "Try an example" });
    const promptBox = screen.queryByRole("textbox", { name: /^(Prompt|Question)$/ }) as HTMLTextAreaElement | null;
    if (example && promptBox?.value === "") await user.click(example);
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByAltText("Generated result", {}, { timeout: 15000 });
    expect(submittedBody().extensions).toBeUndefined();
    expect(screen.queryByText("Prompt not kept")).toBeNull();
  });

  it("sends the forget control, says so on the result and remembers it for the session", async () => {
    const user = userEvent.setup();
    renderTry();
    await ready();
    await user.click(
      await screen.findByRole("switch", { name: "Keep request history" }),
    );
    expect(
      screen.getByText("The prompt isn’t kept. The picture still goes to Library."),
    ).toBeVisible();
    expect(sessionStorage.getItem("azizam.try.forget")).toBe("1");
    const example = screen.queryByRole("button", { name: "Try an example" });
    const promptBox = screen.queryByRole("textbox", { name: /^(Prompt|Question)$/ }) as HTMLTextAreaElement | null;
    if (example && promptBox?.value === "") await user.click(example);
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByAltText("Generated result", {}, { timeout: 15000 });
    expect(submittedBody().extensions).toEqual({
      "com.azizam.prompts": { mode: "forget" },
    });
    expect(screen.getByText("Not saved in your History")).toBeVisible();
    expect(screen.queryByText("Prompt not kept")).toBeNull();

    cleanup();
    renderTry();
    await ready();
    expect(
      await screen.findByRole("switch", { name: "Keep request history" }),
    ).not.toBeChecked();
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
    const example = screen.queryByRole("button", { name: "Try an example" });
    const promptBox = screen.queryByRole("textbox", { name: /^(Prompt|Question)$/ }) as HTMLTextAreaElement | null;
    if (example && promptBox?.value === "") await user.click(example);
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByAltText("Generated result", {}, { timeout: 15000 });
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

  it("is offered on a chat, which History keeps, and not to someone signed out", async () => {
    account.params = new URLSearchParams("capability=chat");
    renderTry();
    expect(await screen.findByRole("switch", { name: "Keep request history" })).toBeVisible();
    cleanup();
    account.promptMemory.mockClear();
    account.params = new URLSearchParams("capability=image");
    renderTry({ ...member, authenticated: false });
    await ready();
    expect(account.promptMemory).not.toHaveBeenCalled();
    expect(screen.queryByRole("switch", { name: "Keep request history" })).toBeNull();
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
  const example = screen.queryByRole("button", { name: "Try an example" });
  const promptBox = screen.queryByRole("textbox", { name: /^(Prompt|Question)$/ }) as HTMLTextAreaElement | null;
  if (example && promptBox?.value === "") await user.click(example);
  await user.click(screen.getByRole("button", { name: "Run" }));
  await screen.findByAltText("Generated result", {}, { timeout: 15000 });
  expect(
    screen.getByText("The prompt isn’t kept. The picture still goes to Library."),
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
    screen.getByRole("switch", { name: "Keep request history" }),
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

it.each(["sign-out", "unmount"] as const)(
  "does not dispatch prepared speech after %s during the privacy refresh",
  async (ending) => {
    const { act } = await import("@testing-library/react");
    let finishPrivacy!: (value: ReturnType<typeof memory>) => void;
    const pendingPrivacy = new Promise<ReturnType<typeof memory>>((resolve) => {
      finishPrivacy = resolve;
    });
    account.params = new URLSearchParams("capability=speech");
    account.promptMemory
      .mockReset()
      .mockResolvedValueOnce(memory())
      .mockImplementation(() => pendingPrivacy);
    fetchMock.mockReset().mockImplementation(async (url: string) => {
      if (url === "/me/api/models/catalog") {
        return json({
          aliases: [],
          models: [{
            id: "fixture-speech",
            name: "Fixture speech",
            capabilities: ["speech"],
            locality: "local",
          }],
        });
      }
      if (url === "/me/api/workflows") return json({ workflows: [] });
      // A fake empty result is sufficient: this endpoint must not be reached.
      if (url === "/openai/v1/audio/speech") return json({});
      throw new Error("Unexpected request: " + url);
    });
    const view = renderTry();
    try {
      await waitFor(() =>
        expect(screen.getByRole("combobox", { name: "Model" }))
          .toHaveTextContent("Fixture speech"),
      );
      await screen.findByRole("switch", { name: "Keep request history" });
      const example = screen.queryByRole("button", { name: "Try an example" });
      const promptBox = screen.queryByRole("textbox", { name: /^(Prompt|Question)$/ }) as HTMLTextAreaElement | null;
      if (example && promptBox?.value === "") await userEvent.setup().click(example);
      await userEvent.setup().click(screen.getByRole("button", { name: "Run" }));
      await waitFor(() => expect(account.promptMemory).toHaveBeenCalledTimes(2));
      expect(account.tryKey).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"))
        .toHaveLength(0);

      if (ending === "unmount") {
        view.unmount();
      } else {
        view.rerender(
          <ViewerContext.Provider value={{ ...member, authenticated: false }}>
            <TryPage />
          </ViewerContext.Provider>,
        );
      }
      await act(async () => {
        finishPrivacy(memory());
        await pendingPrivacy;
      });
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"))
        .toHaveLength(0);
      expect(screen.queryByText("Key details")).toBeNull();
    } finally {
      view.unmount();
    }
  },
);
