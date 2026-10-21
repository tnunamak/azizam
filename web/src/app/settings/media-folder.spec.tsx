import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  role: "owner",
  error: vi.fn(),
  success: vi.fn(),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: mocks.role }),
}));
vi.mock("@/lib/toast", () => ({
  toast: { success: mocks.success, error: mocks.error },
}));

import { createQueryClient } from "@/lib/api";
import { MediaFolderSection } from "./media-folder";

const NO_MATCHES =
  "None of the Library's items have their files in this folder, so Azizam can't confirm it. Check that the media disk is mounted, then try again.";
const folder = (extra: Record<string, unknown> = {}) => ({
  state: "unconfirmed",
  path: "cache/images",
  library_items: 845,
  items_here: 602,
  uncheckable_items: 0,
  ...extra,
});

let calls: string[];
let confirmReply: () => Response;
function serve(media_folder: unknown) {
  calls = [];
  confirmReply = () => Response.json({ confirmed: true });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/me/api/session") return Response.json({ csrf_token: "csrf-1" });
      if (path === "/me/api/media/confirm-folder") {
        calls.push(`confirm ${JSON.stringify(init?.headers)}`);
        return confirmReply();
      }
      calls.push(path);
      return Response.json({ assets: [], next_cursor: null, media_folder });
    }),
  );
}
function renderSection() {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MediaFolderSection />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.role = "owner";
  mocks.error.mockClear();
  mocks.success.mockClear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("owner's Confirm the media folder action", () => {
  it("explains with whole-Library counts and confirms through the owner route", async () => {
    serve(folder());
    renderSection();
    const button = await screen.findByRole("button", { name: "Confirm the media folder" });
    expect(
      screen.getByText(
        "Across the whole Library, 243 of 845 checked items are missing from cache/images, so only do this if your media disk is connected and cache/images is where your photos, videos and audio are kept.",
      ),
    ).toBeVisible();
    await userEvent.click(button);
    await waitFor(() => expect(mocks.success).toHaveBeenCalledWith("Media folder confirmed."));
    expect(calls).toContain(
      'confirm {"x-azizam-user-action":"1","x-azizam-csrf":"csrf-1"}',
    );
  });

  it("shows the change warning for a folder that looks different", async () => {
    serve(folder({ state: "mismatch" }));
    renderSection();
    expect(
      await screen.findByText(/^The media folder looks different from last time \(cache\/images\)\./),
    ).toBeVisible();
  });

  it("cannot be pressed when the folder holds none of the items, and says to check the disk", async () => {
    serve(folder({ items_here: 0 }));
    renderSection();
    expect(await screen.findByRole("button", { name: "Confirm the media folder" })).toBeDisabled();
    expect(
      screen.getByText(
        "Azizam can't find any of the Library's 845 checked items in cache/images. Check that your media disk is connected.",
      ),
    ).toBeVisible();
  });

  it("shows the gateway's refusal as it is", async () => {
    serve(folder());
    renderSection();
    const button = await screen.findByRole("button", { name: "Confirm the media folder" });
    confirmReply = () =>
      Response.json(
        { error: { code: "media_folder_no_matches", message: NO_MATCHES } },
        { status: 409 },
      );
    await userEvent.click(button);
    await waitFor(() => expect(mocks.error).toHaveBeenCalledWith(NO_MATCHES));
  });

  it("is absent once the folder is confirmed", async () => {
    serve({ state: "confirmed" });
    renderSection();
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(screen.queryByText("Media folder")).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm the media folder" })).toBeNull();
  });

  it("is absent for a member, who is not asked and sends nothing", async () => {
    mocks.role = "member";
    serve(folder());
    renderSection();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(calls).toEqual([]);
    expect(screen.queryByText("Media folder")).toBeNull();
  });
});
