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
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import LibraryPage from "@/app/library/page";
import { ViewerContext } from "@/components/shell/session";
const image = {
  asset_id: "image",
  kind: "image",
  prompt: "Saved image",
  model: "",
  created_at: null,
  download_url: "/image.png",
};
const video = {
  ...image,
  asset_id: "video",
  kind: "video",
  prompt: "Saved video",
  download_url: "/video.mp4",
  poster_asset_id: "poster",
};
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: "Alex",
          email: null,
          role: "member",
          admin: false,
        }}
      >
        <LibraryPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  return { ...result, client };
}
function mockAssets(assets: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/poster/download")
        ? Response.json({ download_url: "/poster.png" })
        : String(input).includes("/video/download")
          ? Response.json({ download_url: "/video.mp4" })
        : String(input).includes("/image/download")
          ? Response.json({ download_url: "/image.png" })
          : Response.json({ assets, models: [], next_cursor: null }),
    ),
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
describe("Library media recovery", () => {
  it("HL-01 reaches a retryable fallback after both poster and video fail", async () => {
    mockAssets([video]);
    const { container } = mount();
    fireEvent.error(await screen.findByRole("img", { name: "Saved video" }));
    await waitFor(() =>
      expect(container.querySelector("video")).not.toBeNull(),
    );
    fireEvent.error(container.querySelector("video")!);
    expect(
      await screen.findByRole("img", { name: "Video preview unavailable" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: /Retry/i })).toBeVisible();
  });
  it("HL-01 shows a failed large player even with a usable poster", async () => {
    mockAssets([video]);
    mount();
    await screen.findByRole("img", { name: "Saved video" });
    await userEvent.click(
      screen.getByRole("button", { name: "Open Saved video" }),
    );
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog.querySelector("video")).not.toBeNull());
    fireEvent.error(dialog.querySelector("video")!);
    expect(
      await within(dialog).findByRole("img", {
        name: "Video preview unavailable",
      }),
    ).toBeVisible();
  });
  it("HL-01 keeps playable video after its poster fails", async () => {
    mockAssets([video]);
    const { container } = mount();
    fireEvent.error(await screen.findByRole("img", { name: "Saved video" }));
    await waitFor(() =>
      expect(container.querySelector("video")).not.toBeNull(),
    );
    fireEvent.loadedMetadata(container.querySelector("video")!);
    expect(
      screen.queryByRole("img", { name: "Video preview unavailable" }),
    ).toBeNull();
  });
  it("HL-04 retries a failed image with an unchanged signed URL", async () => {
    mockAssets([image]);
    mount();
    fireEvent.error(await screen.findByRole("img", { name: "Saved image" }));
    await userEvent.click(
      await screen.findByRole("button", { name: /Retry/i }),
    );
    expect(
      await screen.findByRole("img", { name: "Saved image" }),
    ).toHaveAttribute("src", expect.stringContaining("/image.png"));
  });
  it("HL-04 offers refresh for one unavailable item", async () => {
    mockAssets([{ ...image, available: false }]);
    mount();
    expect(
      await screen.findByRole("button", { name: "Refresh library" }),
    ).toBeVisible();
  });
  it("HL-04 clears an old URL error after fresh signed metadata arrives", async () => {
    let renew!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/image/download")
          ? new Promise<Response>((resolve) => {
              renew = resolve;
            })
          : Response.json({ assets: [image], next_cursor: null }),
      ),
    );
    mount();
    fireEvent.error(await screen.findByRole("img", { name: "Saved image" }));
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    fireEvent.error(await screen.findByRole("img", { name: "Saved image" }));
    renew(Response.json({ download_url: "/renewed.png" }));
    expect(
      await screen.findByRole("img", { name: "Saved image" }),
    ).toHaveAttribute("src", expect.stringContaining("/renewed.png"));
  });
  it("HL-04 recovers an unavailable item through signed metadata", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/image/download")
        ? Response.json({ download_url: "/image.png" })
        : Response.json({
            assets: [{ ...image, available: false }],
            next_cursor: null,
          }),
    );
    vi.stubGlobal("fetch", fetchMock);
    mount();
    await userEvent.click(
      await screen.findByRole("button", { name: /^Retry$/ }),
    );
    expect(
      await screen.findByRole("img", { name: "Saved image" }),
    ).toHaveAttribute("src", expect.stringContaining("/image.png"));
    expect(
      fetchMock.mock.calls.some(
        ([input]) => String(input) === "/me/api/media/image/download?scope=me",
      ),
    ).toBe(true);
  });
  it("HL-05 retains page one and retries only failed page two", async () => {
    let attempts = 0;
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        requests.push(path);
        if (path.includes("cursor=next")) {
          attempts++;
          return attempts === 1
            ? Response.json(
                { error: { message: "Failed page" } },
                { status: 503 },
              )
            : Response.json({
                assets: [
                  { ...image, asset_id: "second", prompt: "Second image" },
                ],
                next_cursor: null,
              });
        }
        return Response.json({ assets: [image], next_cursor: "next" });
      }),
    );
    mount();
    await userEvent.click(
      await screen.findByRole("button", { name: "Load more" }),
    );
    await screen.findByRole("alert");
    expect(
      screen.getByRole("button", { name: "Open Saved image" }),
    ).toBeVisible();
    const count = requests.filter(
      (path) => path.startsWith("/me/api/media?") && !path.includes("cursor="),
    ).length;
    await userEvent.click(
      screen.getByRole("button", { name: /Load more|Retry/i }),
    );
    expect(
      await screen.findByRole("button", { name: "Open Second image" }),
    ).toBeVisible();
    expect(
      requests.filter(
        (path) =>
          path.startsWith("/me/api/media?") && !path.includes("cursor="),
      ).length,
    ).toBe(count);
  });
  it("HL-05 keeps cached tiles after a background failure and refetches them", async () => {
    let fail = false;
    const fetchMock = vi.fn(async () =>
      fail
        ? Response.json({}, { status: 503 })
        : Response.json({ assets: [image], next_cursor: null }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { client } = mount();
    await screen.findByRole("button", { name: "Open Saved image" });
    fail = true;
    await act(async () => {
      await client.refetchQueries({ queryKey: ["library"] });
    });
    await screen.findByRole("alert");
    expect(
      screen.getByRole("button", { name: "Open Saved image" }),
    ).toBeVisible();
    fail = false;
    await userEvent.click(
      screen.getByRole("button", { name: /Load more|Retry/i }),
    );
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
  it("HL-06 waits for poster resolution before mounting video", async () => {
    let resolvePoster!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/poster/download")
          ? new Promise<Response>((resolve) => {
              resolvePoster = resolve;
            })
          : Response.json({ assets: [video], next_cursor: null }),
      ),
    );
    const { container } = mount();
    await screen.findByRole("button", { name: "Open Saved video" });
    expect(container.querySelector("video")).toBeNull();
    resolvePoster(Response.json({ download_url: "/poster.png" }));
    expect(
      await screen.findByRole("img", { name: "Saved video" }),
    ).toHaveAttribute("src", expect.stringContaining("/poster.png"));
    // The tile video exists for keyboard play but loads no media until played.
    expect(container.querySelector("video")).not.toHaveAttribute("src");
  });
  it("HL-05 retains previous results when a replacement search fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("q=")
          ? Response.json({}, { status: 503 })
          : Response.json({ assets: [image], next_cursor: null }),
      ),
    );
    mount();
    await screen.findByRole("button", { name: "Open Saved image" });
    fireEvent.change(screen.getByPlaceholderText("Search your creations"), {
      target: { value: "unavailable search" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The gateway couldn’t refresh your library. Try again.",
    );
    expect(
      screen.getByRole("button", { name: "Open Saved image" }),
    ).toBeVisible();
  });
  it("HL-06 defers offscreen posterless video sources", async () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    let enter!: IntersectionObserverCallback;
    let observed!: Element;
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(callback: IntersectionObserverCallback) {
          enter = callback;
        }
        observe(element: Element) {
          observed = element;
        }
        disconnect() {}
        unobserve() {}
      },
    );
    mockAssets([{ ...video, poster_asset_id: null }]);
    const { container } = mount();
    await screen.findByRole("button", { name: "Open Saved video" });
    expect(container.querySelector("video[src]")).toBeNull();
    act(() =>
      enter(
        [
          {
            isIntersecting: true,
            target: observed,
          } as IntersectionObserverEntry,
        ],
        {} as IntersectionObserver,
      ),
    );
    // A posterless tile loads metadata only and shows the frame at 0.1 s on
    // the media stage; it never plays.
    await waitFor(() =>
      expect(container.querySelector("video")).toHaveAttribute(
        "src",
        "/video.mp4#t=0.1",
      ),
    );
    const preview = container.querySelector("video")!;
    expect(preview).toHaveAttribute("preload", "metadata");
    expect(preview).not.toHaveAttribute("autoplay");
    expect(preview).toHaveClass("bg-media-stage");
    Object.defineProperty(preview, "duration", {
      configurable: true,
      value: 6,
    });
    fireEvent.loadedMetadata(preview);
    expect(preview.currentTime).toBeCloseTo(0.1);
    expect(pause).not.toHaveBeenCalled();
  });
  it("HL-06 uses no preload for mounted audio", async () => {
    mockAssets([{ ...image, kind: "audio", duration_s: 12 }]);
    const { container } = mount();
    await screen.findByRole("button", { name: "Play Saved image" });
    expect(container.querySelector("audio")).toHaveAttribute("preload", "none");
  });
  it("HL-06 observes the same card after a pending poster fails offscreen", async () => {
    let enter!: IntersectionObserverCallback;
    let observed!: Element;
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(callback: IntersectionObserverCallback) {
          enter = callback;
        }
        observe(element: Element) {
          observed = element;
        }
        disconnect() {}
      },
    );
    let rejectPoster!: (error: Error) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/poster/download")
          ? new Promise<Response>((_resolve, reject) => {
              rejectPoster = reject;
            })
          : Response.json({ assets: [video], next_cursor: null }),
      ),
    );
    const { container } = mount();
    await screen.findByRole("status", { name: "Loading video poster" });
    const cardWrapper = observed;
    await act(async () => rejectPoster(new Error("Poster unavailable")));
    await waitFor(() =>
      expect(container.querySelector("video")).not.toHaveAttribute("src"),
    );
    expect(cardWrapper.isConnected).toBe(true);
    act(() =>
      enter(
        [
          {
            isIntersecting: true,
            target: cardWrapper,
          } as IntersectionObserverEntry,
        ],
        {} as IntersectionObserver,
      ),
    );
    await waitFor(() =>
      expect(container.querySelector("video")).toHaveAttribute(
        "src",
        "/video.mp4#t=0.1",
      ),
    );
  });
  it("HL-08 debounces and aborts stale searches while retaining tiles", async () => {
    const requests: { path: string; signal?: AbortSignal | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        requests.push({ path, signal: init?.signal });
        if (path.includes("q=")) return new Promise<Response>(() => {});
        return Response.json({ assets: [image], next_cursor: null });
      }),
    );
    mount();
    await screen.findByRole("button", { name: "Open Saved image" });
    const input = screen.getByPlaceholderText("Search your creations");
    for (const value of ["a", "ab", "abc"])
      fireEvent.change(input, { target: { value } });
    expect(requests.filter((row) => row.path.includes("q="))).toHaveLength(0);
    await waitFor(() =>
      expect(requests.filter((row) => row.path.includes("q="))).toHaveLength(1),
    );
    expect(
      screen.getByRole("button", { name: "Open Saved image" }),
    ).toBeVisible();
    const stale = requests.find((row) => row.path.includes("q=abc"))!;
    fireEvent.change(input, { target: { value: "abcd" } });
    await waitFor(() => expect(stale.signal?.aborted).toBe(true));
  });
  it("HL-09 wraps long prompts in tiles and details", async () => {
    const prompt = "x".repeat(500);
    const audioPrompt = "y".repeat(500);
    mockAssets([
      { ...image, prompt },
      { ...image, asset_id: "audio", kind: "audio", prompt: audioPrompt },
    ]);
    mount();
    // A picture's hover title is a short whole-word title on up to two
    // lines; a playable audio tile carries no words of its own (PF-070).
    expect(
      await screen.findByRole("button", { name: `Open ${prompt}` }),
    ).toBeVisible();
    expect(screen.getByText(`X${"x".repeat(55)}…`)).toHaveClass("line-clamp-2");
    expect(screen.queryByText(`Y${"y".repeat(55)}…`)).toBeNull();
    expect(screen.getByRole("button", { name: `Open ${audioPrompt}` })).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: `Open ${prompt}` }),
    );
    for (const text of within(screen.getByRole("dialog")).getAllByText(prompt))
      expect(text).toHaveClass("break-words");
  });
});
