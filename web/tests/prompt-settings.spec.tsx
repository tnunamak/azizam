import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type {
  HouseholdPromptMemory,
  MemberPromptMemoryResponse,
} from "@/lib/prompt-memory";

const mocks = vi.hoisted(() => ({
  viewer: { authenticated: true, role: "member", admin: false, name: "Robin" },
  promptMemory: vi.fn(),
  savePromptMemory: vi.fn(),
  deleteSavedPrompts: vi.fn(),
  householdGet: vi.fn(),
  householdPut: vi.fn(),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => mocks.viewer,
}));
vi.mock("@/lib/use-owner-name", () => ({ useOwnerName: () => "Sam" }));
vi.mock("@/lib/account-api", () => ({
  accountApi: {
    promptMemory: mocks.promptMemory,
    savePromptMemory: mocks.savePromptMemory,
    deleteSavedPrompts: mocks.deleteSavedPrompts,
  },
}));
vi.mock("@/lib/prompt-memory", async (original) => ({
  ...(await original<typeof import("@/lib/prompt-memory")>()),
  householdPromptMemoryApi: {
    get: mocks.householdGet,
    put: mocks.householdPut,
  },
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import AccountPage from "@/app/account/page";
import { HouseholdPromptsSection } from "@/app/settings/household-prompts";

const household: HouseholdPromptMemory = {
  default_remember: true,
  never_keep: false,
  retention_cap: "never",
};
function member(
  overrides: Partial<MemberPromptMemoryResponse["prompt_memory"]> = {},
  house: Partial<HouseholdPromptMemory> = {},
  extra: Partial<MemberPromptMemoryResponse> = {},
): MemberPromptMemoryResponse {
  return {
    prompt_memory: {
      remember: true,
      retention: "never",
      effective_remember: true,
      effective_retention: "never",
      household: { ...household, ...house },
      ...overrides,
    },
    ...extra,
  };
}

function wrap(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{node}</QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.viewer = {
    authenticated: true,
    role: "member",
    admin: false,
    name: "Robin",
  };
  // Radix Select needs these, which jsdom lacks.
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
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

describe("Your settings: prompts", () => {
  it("asks before turning memory off, because that deletes saved prompts", async () => {
    const user = userEvent.setup();
    mocks.promptMemory.mockResolvedValue(member());
    mocks.savePromptMemory.mockResolvedValue(
      member({ remember: false, effective_remember: false }),
    );
    wrap(<AccountPage />);
    const toggle = await screen.findByRole("switch", {
      name: "Remember what I type",
    });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Your saved prompts are deleted now");
    expect(dialog).toHaveTextContent(
      "Your pictures, audio and videos stay in Library.",
    );
    expect(mocks.savePromptMemory).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() =>
      expect(mocks.savePromptMemory.mock.calls[0]?.[0]).toEqual({
        remember: false,
        retention: "never",
      }),
    );
    expect(
      await screen.findByRole("switch", { name: "Remember what I type" }),
    ).not.toBeChecked();
  });

  it("deletes saved prompts after asking, and says how many", async () => {
    const user = userEvent.setup();
    const { toast } = await import("sonner");
    mocks.promptMemory.mockResolvedValue(member());
    mocks.deleteSavedPrompts.mockResolvedValue({ deleted_prompts: 3 });
    wrap(<AccountPage />);
    await user.click(await screen.findByRole("button", { name: "Delete…" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Delete prompts",
      }),
    );
    await waitFor(() =>
      expect(mocks.deleteSavedPrompts).toHaveBeenCalledTimes(1),
    );
    expect(toast.success).toHaveBeenCalledWith("Deleted 3 saved prompts.");
  });

  it("shows file cleanup that is still running, honestly", async () => {
    mocks.promptMemory.mockResolvedValue(
      member(
        {},
        {},
        {
          prompt_purge: {
            pending: true,
            metadata_stripping: { status: "queued", files_pending: 4 },
          },
        },
      ),
    );
    wrap(<AccountPage />);
    const status = (
      await screen.findByText("Saved prompts are deleted")
    ).closest("[role=status]");
    expect(status).not.toBeNull();
    expect(status).toHaveTextContent(
      "Removing copies from older files in the background. 4 files left.",
    );
  });

  it("holds members to the household's cap", async () => {
    const user = userEvent.setup();
    mocks.promptMemory.mockResolvedValue(
      member(
        { effective_retention: "3_months" },
        { retention_cap: "3_months" },
      ),
    );
    wrap(<AccountPage />);
    expect(
      await screen.findByText("Up to 3 months on this gateway."),
    ).toBeVisible();
    const select = screen.getByRole("combobox", {
      name: "Forget prompts after",
    });
    expect(select).toHaveTextContent("3 months");
    await user.click(select);
    expect(
      await screen.findByRole("option", { name: "Never" }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("option", { name: "1 year" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("says who turned prompts off for everyone, and offers nothing to change", async () => {
    mocks.promptMemory.mockResolvedValue(
      member({ effective_remember: false }, { never_keep: true }),
    );
    wrap(<AccountPage />);
    expect(
      await screen.findByText("Sam turned this off for everyone."),
    ).toBeVisible();
    expect(
      screen.getByRole("switch", { name: "Remember what I type" }),
    ).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Delete…" })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("Settings: the household's prompt rules", () => {
  it("asks before the kill switch, then saves with the revision", async () => {
    const user = userEvent.setup();
    mocks.viewer = {
      authenticated: true,
      role: "owner",
      admin: true,
      name: "Sam",
    };
    mocks.householdGet.mockResolvedValue({
      prompt_memory: household,
      revision: "r1",
    });
    mocks.householdPut.mockResolvedValue({
      prompt_memory: { ...household, never_keep: true },
      revision: "r2",
    });
    wrap(<HouseholdPromptsSection />);
    await user.click(
      await screen.findByRole("switch", { name: "Never keep prompts" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(
      "Turning this off later can’t bring them back.",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Never keep prompts" }),
    );
    await waitFor(() =>
      expect(mocks.householdPut).toHaveBeenCalledWith(
        { ...household, never_keep: true },
        "r1",
      ),
    );
    expect(
      await screen.findByRole("switch", { name: "Never keep prompts" }),
    ).toBeChecked();
    expect(
      screen.getByRole("switch", { name: "Remember prompts for new people" }),
    ).toBeDisabled();
  });

  it("saves a longer cap at once and asks before a shorter one", async () => {
    const user = userEvent.setup();
    mocks.householdGet.mockResolvedValue({
      prompt_memory: { ...household, retention_cap: "1_year" },
      revision: "r1",
    });
    mocks.householdPut.mockImplementation(
      async (value: HouseholdPromptMemory) => ({
        prompt_memory: value,
        revision: "r2",
      }),
    );
    wrap(<HouseholdPromptsSection />);
    const select = await screen.findByRole("combobox", {
      name: "Keep prompts for",
    });
    await user.click(select);
    await user.click(await screen.findByRole("option", { name: "No limit" }));
    await waitFor(() =>
      expect(mocks.householdPut).toHaveBeenCalledWith(
        { ...household, retention_cap: "never" },
        "r1",
      ),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(
      screen.getByRole("combobox", { name: "Keep prompts for" }),
    );
    await user.click(await screen.findByRole("option", { name: "3 months" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "Everyone’s prompts older than 3 months are deleted now.",
    );
  });
});
