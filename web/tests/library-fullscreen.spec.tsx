import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
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
};

function mockAssets(assets: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/video/download")
        ? Response.json({ download_url: "/video.mp4" })
        : String(input).includes("/image/download")
          ? Response.json({ download_url: "/image.png" })
          : Response.json({ assets, models: [], next_cursor: null }),
    ),
  );
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{ authenticated: true, name: "Alex", email: null, role: "member", admin: false }}
      >
        <LibraryPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

/** Opens the viewer for one tile and returns its dialog and the media stage inside it. */
async function openViewer(name: string) {
  const tile = await screen.findByRole("button", { name: (label) => label.startsWith(`Open ${name}, `) });
  await userEvent.click(tile);
  const dialog = await screen.findByRole("dialog");
  await waitFor(() => expect(dialog.querySelector("img, video")).not.toBeNull());
  const stage = dialog.querySelector<HTMLElement>("[data-fullscreen]");
  expect(stage).not.toBeNull();
  return { dialog, stage: stage! };
}

/**
 * The browser's Fullscreen API, as far as the viewer uses it: `requestFullscreen` on an element makes it the
 * full-screen element and fires `fullscreenchange`; the browser can also leave on its own (Escape).
 */
function stubFullscreen() {
  const requested: Element[] = [];
  let current: Element | null = null;
  const change = () => document.dispatchEvent(new Event("fullscreenchange"));
  Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: true });
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => current });
  Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
    configurable: true,
    value: function requestFullscreen(this: Element) {
      requested.push(this);
      current = this;
      change();
      return Promise.resolve();
    },
  });
  Object.defineProperty(document, "exitFullscreen", {
    configurable: true,
    value: () => {
      current = null;
      change();
      return Promise.resolve();
    },
  });
  return {
    requested,
    leaveNatively: () => {
      current = null;
      change();
    },
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  for (const [target, name] of [
    [document, "fullscreenEnabled"],
    [document, "fullscreenElement"],
    [document, "exitFullscreen"],
    [HTMLElement.prototype, "requestFullscreen"],
  ] as const) {
    Reflect.deleteProperty(target, name);
  }
});

it("puts a picture full screen with the Fullscreen API, and the browser's own Escape leaves it with the viewer open", async () => {
  const fullscreen = stubFullscreen();
  mockAssets([image]);
  mount();
  const { dialog, stage } = await openViewer("Saved image");

  await userEvent.click(within(dialog).getByRole("button", { name: "Full screen" }));

  expect(fullscreen.requested).toEqual([stage.querySelector("img")]);
  expect(stage).toHaveAttribute("data-fullscreen", "browser");
  expect(within(dialog).getByRole("button", { name: "Exit full screen" })).toBeVisible();

  act(() => fullscreen.leaveNatively());

  expect(stage).toHaveAttribute("data-fullscreen", "off");
  expect(within(dialog).getByRole("button", { name: "Full screen" })).toBeVisible();
  expect(screen.getByRole("dialog")).toBeVisible();
});

it("gives a video its native controls while it fills the screen, and takes them away on leaving", async () => {
  const fullscreen = stubFullscreen();
  mockAssets([video]);
  mount();
  const { dialog, stage } = await openViewer("Saved video");
  const player = stage.querySelector("video");
  expect(player).not.toBeNull();
  expect(player!.controls).toBe(false);

  await userEvent.click(within(dialog).getByRole("button", { name: "Full screen" }));

  expect(fullscreen.requested).toEqual([player]);
  expect(player!.controls).toBe(true);

  act(() => fullscreen.leaveNatively());

  expect(player!.controls).toBe(false);
});

it("is reached by the keyboard: Enter on the button starts full screen", async () => {
  const fullscreen = stubFullscreen();
  mockAssets([image]);
  mount();
  const { dialog, stage } = await openViewer("Saved image");

  within(dialog).getByRole("button", { name: "Full screen" }).focus();
  await userEvent.keyboard("{Enter}");

  expect(fullscreen.requested).toEqual([stage.querySelector("img")]);
  expect(stage).toHaveAttribute("data-fullscreen", "browser");
});

it("where the browser has no Fullscreen API, the stage fills the window and Escape leaves it without closing the viewer", async () => {
  mockAssets([image]);
  mount();
  const { dialog, stage } = await openViewer("Saved image");

  await userEvent.click(within(dialog).getByRole("button", { name: "Full screen" }));

  expect(stage).toHaveAttribute("data-fullscreen", "fallback");
  expect(stage.className).toContain("fixed");
  expect(within(dialog).getByRole("button", { name: "Exit full screen" })).toBeVisible();

  fireEvent.keyDown(dialog, { key: "Escape" });

  expect(stage).toHaveAttribute("data-fullscreen", "off");
  expect(screen.getByRole("dialog")).toBeVisible();
});

it("an audio clip has no full-screen control", async () => {
  mockAssets([{ ...image, asset_id: "clip", kind: "audio", prompt: "Saved clip", download_url: "/clip.mp3" }]);
  mount();
  await screen.findByRole("button", { name: (label) => label.startsWith("Open Saved clip, ") });
  await userEvent.click(screen.getByRole("button", { name: (label) => label.startsWith("Open Saved clip, ") }));
  const dialog = await screen.findByRole("dialog");

  expect(within(dialog).queryByRole("button", { name: "Full screen" })).toBeNull();
});
