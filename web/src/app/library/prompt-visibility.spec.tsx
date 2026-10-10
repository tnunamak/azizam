/* eslint-disable @next/next/no-img-element */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import LibraryPage from "./page";

// Prompt visibility in the viewer (docs/design/PROMPT_MEMORY.md): the
// gateway sends a prompt and the details download only to the item's maker.
// The viewer must show the maker their prompt, say "Prompt not saved" only
// to the maker, and give anyone else no sign that a prompt exists.

const state = vi.hoisted(() => ({
  viewer: { role: "member", name: "Robin", id: "usr_robin" } as Record<
    string,
    unknown
  >,
  home: { receipts: [], user: { id: "usr_robin" } } as Record<string, unknown>,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("next/image", () => ({
  default: ({ alt, src }: { alt: string; src: string }) => (
    <img alt={alt} src={src} />
  ),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => state.viewer,
}));
vi.mock("@/lib/product-api", () => ({
  productApi: {
    home: async () => state.home,
    models: async () => ({ aliases: [], models: [] }),
  },
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

const PICTURE =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E";
const base = {
  kind: "image",
  model: "image",
  created_at: 1_760_000_000,
  download_url: PICTURE,
  available: true,
};
// What the gateway sends the maker: the prompt and the details download.
const kept = {
  ...base,
  asset_id: "img_kept",
  prompt: "A ginger cat peeking from a bookshelf between paperbacks",
  workflow_download_url: "https://gw.test/v1/assets/img_kept/content?md=full",
  source: { id: "app:a1", kind: "app", label: "Robin", person_id: "usr_robin" },
};
// The same person's item made with "Don't keep this prompt".
const forgotten = {
  ...base,
  asset_id: "img_forgotten",
  prompt: null,
  workflow_download_url: null,
  source: { id: "app:a1", kind: "app", label: "Robin", person_id: "usr_robin" },
};
// What the gateway sends anyone else for Robin's item: no prompt, no
// details download, Robin as maker.
const othersView = { ...forgotten, asset_id: "img_others" };

function renderWith(assets: unknown[], promptMemory?: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/me/api/prompt-memory"))
        return promptMemory
          ? Response.json(promptMemory)
          : Response.json({ error: { code: "x" } }, { status: 500 });
      if (url.includes("/download"))
        return Response.json({ download_url: PICTURE });
      return Response.json({ assets, models: [], next_cursor: null });
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LibraryPage />
    </QueryClientProvider>,
  );
}

async function openOnly(user: ReturnType<typeof userEvent.setup>) {
  const tile = await screen.findByRole("button", { name: /^Open / });
  await user.click(tile);
  return screen.findByRole("dialog");
}

beforeEach(() => {
  state.viewer = { role: "member", name: "Robin", id: "usr_robin" };
  state.home = { receipts: [], user: { id: "usr_robin" } };
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
});

describe("the prompt in the viewer", () => {
  test("the maker sees their prompt, Copy prompt and Download with details", async () => {
    const user = userEvent.setup();
    renderWith([kept]);
    const dialog = await openOnly(user);
    expect(
      within(dialog).getByRole("heading", {
        name: "A ginger cat peeking from a bookshelf between paperbacks",
      }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Copy prompt" }),
    ).toBeEnabled();
    expect(within(dialog).queryByText("No saved prompt")).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: /^Actions for / }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Download with details" }),
    );
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(([input]) =>
            String(input).includes("/img_kept/download?scope=me&metadata=full"),
          ),
      ).toBe(true),
    );
  });

  test("the maker is told when a prompt was not saved, and offered no copy", async () => {
    const user = userEvent.setup();
    renderWith([forgotten]);
    const dialog = await openOnly(user);
    expect(within(dialog).getByText("No saved prompt")).toBeVisible();
    expect(
      within(dialog).queryByRole("button", { name: "Copy prompt" }),
    ).toBeNull();
  });

  test("the maker is told why, when their settings explain it", async () => {
    const user = userEvent.setup();
    const household = {
      default_remember: true,
      never_keep: false,
      retention_cap: "never",
    };
    renderWith([forgotten], {
      prompt_memory: {
        remember: false,
        retention: "never",
        effective_remember: false,
        effective_retention: "never",
        household,
      },
    });
    const dialog = await openOnly(user);
    expect(
      await within(dialog).findByText("No saved prompt. You turned off prompt keeping."),
    ).toBeVisible();
    cleanup();
    renderWith([forgotten], {
      prompt_memory: {
        remember: true,
        retention: "never",
        effective_remember: false,
        effective_retention: "never",
        household: { ...household, never_keep: true },
      },
    });
    const again = await openOnly(user);
    expect(
      await within(again).findByText(
        "No saved prompt. This gateway doesn’t keep prompts.",
      ),
    ).toBeVisible();
  });

  test("someone else gets no hint that a prompt exists", async () => {
    const user = userEvent.setup();
    state.viewer = { role: "member", name: "Alex", id: "usr_alex" };
    state.home = { receipts: [], user: { id: "usr_alex" } };
    renderWith([othersView]);
    const dialog = await openOnly(user);
    const text = dialog.textContent ?? "";
    expect(text).not.toMatch(/prompt/i);
    expect(text).not.toMatch(/with details/i);
    // The name is the type and time, never words from the prompt.
    expect(
      within(dialog).getByRole("heading", { name: /^Picture, / }),
    ).toBeVisible();
    // Its own App's items can still be deleted, but never downloaded with
    // details.
    await user.click(
      within(dialog).getByRole("button", { name: /^Actions for / }),
    );
    expect(
      await screen.findByRole("menuitem", { name: "Delete" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("menuitem", { name: "Download with details" }),
    ).toBeNull();
  });

  test("the owner viewing everyone's media sees another person's item the same way", async () => {
    const user = userEvent.setup();
    state.viewer = { role: "owner", name: "Sam", id: "usr_sam", admin: true };
    state.home = { receipts: [], user: { id: "usr_sam" } };
    renderWith([othersView]);
    const dialog = await openOnly(user);
    expect(dialog.textContent ?? "").not.toMatch(/prompt|with details/i);
  });
});
