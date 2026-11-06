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
import { libraryEvidenceSentence } from "@/lib/media-folder";

const STALE = "Your Library changed since you opened this. Review it again.";
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
let confirmInit: RequestInit | undefined;
function serve(media_folder: unknown) {
  calls = [];
  confirmInit = undefined;
  confirmReply = () => Response.json({ confirmed: true });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/me/api/session") return Response.json({ csrf_token: "csrf-1" });
      if (path === "/me/api/media/confirm-folder") {
        calls.push(`confirm ${JSON.stringify(init?.headers)}`);
        confirmInit = init;
        return confirmReply();
      }
      calls.push(path);
      return Response.json({ assets: [], next_cursor: null, media_folder });
    }),
  );
}
function renderSection() {
  const queryClient = createQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <MediaFolderSection />
    </QueryClientProvider>,
  );
  return queryClient;
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
  it("uses missing-count agreement and omits zero missing evidence", () => {
    expect(libraryEvidenceSentence("cache/images", 1, 0)).toContain(
      "1 of 1 checked item is missing",
    );
    expect(libraryEvidenceSentence("cache/images", 2, 1)).toContain(
      "1 of 2 checked items is missing",
    );
    expect(libraryEvidenceSentence("cache/images", 5, 4)).toContain("1 of 5 checked items is missing");
    expect(libraryEvidenceSentence("cache/images", 5, 3)).toContain("2 of 5 checked items are missing");
    expect(libraryEvidenceSentence("cache/images", 4, 4)).toBeNull();
  });

  it("explains with whole-Library counts and confirms through the owner route", async () => {
    serve(folder());
    const queryClient = renderSection();
    queryClient.setQueryData(["library", "removal"], { media_folder: folder(), removal_plan: { missing_items: 3 } });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const button = await screen.findByRole("button", { name: "Confirm the media folder" });
    expect(
      screen.getByText(
        "Across the whole Library, 243 of 845 checked items are missing from cache/images, so only do this if your media disk is connected and cache/images is where your photos, videos and audio are kept.",
      ),
    ).toBeVisible();
    await userEvent.click(button);
    await waitFor(() => expect(mocks.success).toHaveBeenCalledWith("Media folder confirmed."));
    expect(calls[0]).toBe("/me/api/media?folder_state=1");
    expect(confirmInit?.headers).toEqual({
      "content-type": "application/json",
      "x-azizam-user-action": "1",
      "x-azizam-csrf": "csrf-1",
    });
    expect(JSON.parse(String(confirmInit?.body))).toEqual({ items_here: 602, library_items: 845 });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["library"] });
    expect(queryClient.getQueryState(["library", "removal"])?.isInvalidated).toBe(true);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["settings", "media-folder"] });
    expect(calls).toContain(
      'confirm {"content-type":"application/json","x-azizam-user-action":"1","x-azizam-csrf":"csrf-1"}',
    );
  });

  it("shows the change warning for a folder that looks different", async () => {
    serve(folder({ state: "mismatch" }));
    renderSection();
    expect(
      await screen.findByText(/^The media folder looks different from last time \(cache\/images\)\./),
    ).toBeVisible();
  });

  it("omits whole-Library evidence when no checked items are missing", async () => {
    serve(folder({ library_items: 4, items_here: 4 }));
    renderSection();
    expect(await screen.findByRole("button", { name: "Confirm the media folder" })).toBeEnabled();
    expect(screen.queryByText(/Across the whole Library/)).toBeNull();
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
    const queryClient = renderSection();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const button = await screen.findByRole("button", { name: "Confirm the media folder" });
    confirmReply = () =>
      Response.json(
        { error: { code: "media_folder_changed", message: STALE } },
        { status: 409 },
      );
    await userEvent.click(button);
    await waitFor(() => expect(mocks.error).toHaveBeenCalledWith(STALE));
    await waitFor(() => expect(calls.filter((path) => path === "/me/api/media?folder_state=1")).toHaveLength(2));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["settings", "media-folder"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["library"] });
  });

  it("warns that uncheckable entries cannot identify the folder while allowing explicit confirmation", async () => {
    serve(folder({ library_items: 0, items_here: 0, uncheckable_items: 3 }));
    renderSection();
    const button = await screen.findByRole("button", { name: "Confirm the media folder" });
    expect(screen.getByText("Azizam can't check the existing Library entries against cache/images, so it can't tell whether this is the media folder. Only confirm if your media disk is connected and this is where your photos, videos and audio are kept.")).toBeVisible();
    expect(screen.queryByText(/0 of 0/)).toBeNull();
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(mocks.success).toHaveBeenCalledWith("Media folder confirmed."));
  });

  it("shows nothing to confirm for an empty Library", async () => {
    serve(folder({ library_items: 0, items_here: 0, uncheckable_items: 0 }));
    renderSection();
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(screen.queryByText("Media folder")).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm the media folder" })).toBeNull();
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
