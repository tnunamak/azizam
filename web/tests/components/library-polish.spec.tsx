import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import LibraryPage from "@/app/library/page";
import { ViewerContext } from "@/components/shell/session";

function renderPage(page: React.ReactElement, owner = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: "Alex",
          email: null,
          role: owner ? "owner" : "member",
          admin: owner,
        }}
      >
        {page}
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
const assets = [
  {
    asset_id: "image1",
    kind: "image",
    prompt: "An ocean",
    model: "image",
    created_at: 1700000000,
    download_url: "/signed/image1",
  },
  {
    asset_id: "video1",
    kind: "video",
    prompt: "Swimming fish",
    model: "video",
    created_at: 1700000000,
    download_url: "/signed/video1",
    duration_s: 6,
  },
  {
    asset_id: "audio1",
    kind: "audio",
    prompt: "Ocean sounds",
    model: "voice",
    created_at: 1700000000,
    download_url: "/signed/audio1",
    duration_s: 12,
  },
];
describe("Library polish", () => {
  it("keeps audio controls outside the open action and uses a duration fallback for video", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => String(input).includes("/download")
        ? Response.json({ download_url: "/signed/audio1" })
        : Response.json({ assets, models: [] })),
    );
    const { container } = renderPage(<LibraryPage />);
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Ocean sounds, ") });
    const audio = container.querySelector("audio");
    expect(audio).not.toHaveAttribute("controls");
    expect(audio).toHaveAttribute("preload", "none");
    const play = screen.getByRole("button", { name: "Play Ocean sounds" });
    expect(
      screen.getByRole("button", { name: (name) => name.startsWith("Open Ocean sounds, ") }).contains(play),
    ).toBe(false);
    expect(audio).not.toHaveAttribute("src");
    expect(audio?.closest("button")).toBeNull();
    expect(container.querySelector("video")).not.toBeNull();
    expect(screen.getByText("0:06")).toBeVisible();
    // Craft round 9: one persistent control carries play, type and length.
    expect(screen.getByText("0:12")).toBeVisible();
    expect(play).toHaveTextContent("0:12");
    const playAudio = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockImplementation(function (this: HTMLMediaElement) {
        expect(this).toHaveAttribute(
          "src",
          expect.stringContaining("/signed/audio1"),
        );
        return Promise.resolve();
      });
    await userEvent.click(play);
    expect(playAudio).toHaveBeenCalledOnce();
    expect(audio).toHaveAttribute(
      "src",
      expect.stringContaining("/signed/audio1"),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: (name) => name.startsWith("Open Ocean sounds, ") }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeVisible();
    // The viewer draws its own player around the labelled audio element.
    expect(await screen.findByLabelText("Generated audio")).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Play Ocean sounds" }),
    ).toBeVisible();
    expect(within(dialog).getByRole("slider", { name: "Position" })).toBeVisible();
  });
  it("resolves a supplied poster through the signed-download API", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        requests.push(path);
        if (path.includes("/poster1/download"))
          return Response.json({ download_url: "/signed/poster1" });
        return Response.json({
          assets: [{ ...assets[1], poster_asset_id: "poster1" }],
          models: [],
        });
      }),
    );
    renderPage(<LibraryPage />, true);
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Swimming fish, ") });
    expect(requests).toContain("/me/api/media/poster1/download");
    // OWNER_VISIBILITY: the owner's Library has no person choice, and no request names a scope.
    expect(screen.queryByRole("combobox", { name: "Person" })).toBeNull();
    expect(requests.filter((path) => path.includes("scope="))).toEqual([]);
  });
  it("does not invent an unknown duration and preserves kind filter requests", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requests.push(String(input));
        return Response.json({
          assets: [{ ...assets[1], duration_s: null }],
          models: [],
        });
      }),
    );
    renderPage(<LibraryPage />);
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Swimming fish, ") });
    expect(screen.queryByText(/^\d+ s$/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Video" }));
    expect(screen.getByRole("button", { name: "Video" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() =>
      expect(requests).toContain("/me/api/media?limit=40&kind=video"),
    );
  });
});
