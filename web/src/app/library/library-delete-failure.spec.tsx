/* eslint-disable @next/next/no-img-element */
import { Profiler } from "react";
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


const navigation = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
}));

// The signed-in person and their own home payload; owner tests change both.
const session = vi.hoisted(() => ({
  viewer: { role: "member", name: "Alex" } as { role: string; name: string; id?: string },
  home: { receipts: [] } as Record<string, unknown>,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => navigation.searchParams,
}));

vi.mock("next/image", () => ({
  default: (props: {
    alt: string;
    src: string;
    unoptimized?: boolean;
    [key: string]: unknown;
  }) => {
    const { alt, src, ...imageProps } = props;
    delete imageProps.unoptimized;
    return <img alt={alt} src={src} {...imageProps} />;
  },
}));

vi.mock("@/components/shell/session", () => ({
  useViewer: () => session.viewer,
}));

vi.mock("@/lib/product-api", () => ({
  productApi: {
    home: async () => session.home,
    models: async () => ({
      aliases: [],
      models: [
        { id: "first-music", title: "First music model", workflow: "audio.generate.first-music" },
        { id: "yue2-song", title: "YuE2 Song", workflow: "audio.generate.yue2-song" },
        { id: "image", title: "Image model", display_name: "Image model" },
        {
          id: "audio.generate.test-model",
          title: "Sound model",
          display_name: "Sound model",
        },
      ],
    }),
  },
}));

vi.mock("@/lib/toast", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));




function renderLibrary(onCommit: () => void = () => {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <Profiler id="library" onRender={onCommit}>
          <LibraryPage />
        </Profiler>
      </QueryClientProvider>,
    ),
  };
}

function list(assets: unknown[], extra: Record<string, unknown> = {}) {
  return Response.json({ assets, models: [], next_cursor: null, ...extra });
}

const picture = (id: string, prompt: string, extra: object = {}) => ({
  asset_id: id,
  request_id: null,
  kind: "image",
  prompt,
  model: "image",
  created_at: Math.floor(Date.now() / 1000),
  download_url: `/signed/${id}.png`,
  ...extra,
});

describe("Library delete failure boundaries", () => {
  beforeEach(() => {
    navigation.searchParams = new URLSearchParams();
    session.viewer = { role: "member", name: "Alex", id: "alex" };
    session.home = { receipts: [], user: { id: "alex" }, apps: [] };
    window.history.replaceState(null, "", "/library/");
    for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"]) {
      Object.defineProperty(HTMLElement.prototype, method, {
        configurable: true,
        value: () => false,
      });
    }
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test.each(["session-503", "session-unreadable", "delete-403"] as const)(
    "retains the creation after %s and retries the exact mutation",
    async (failure) => {
      const { toast } = await import("@/lib/toast");
      vi.mocked(toast.error).mockClear();
      const asset = picture("kept-picture", "Kept picture");
      let sessionReads = 0;
      let deleted = false;
      const deletes: { path: string; init: RequestInit }[] = [];
      vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/me/api/session") {
          sessionReads += 1;
          if (sessionReads === 1 && failure === "session-503") {
            return Response.json({}, { status: 503 });
          }
          if (sessionReads === 1 && failure === "session-unreadable") {
            return Response.json({});
          }
          return Response.json({ csrf_token: `fixture-csrf-${sessionReads}` });
        }
        if (init?.method === "DELETE") {
          deletes.push({ path, init });
          if (deletes.length === 1 && failure === "delete-403") {
            return Response.json({}, { status: 403 });
          }
          deleted = true;
          return new Response(null, { status: 204 });
        }
        if (path.startsWith("/me/api/media?")) return list(deleted ? [] : [asset]);
        if (path.includes("/download?")) return Response.json({ download_url: asset.download_url });
        return Response.json({});
      }));
      const view = renderLibrary();
      const user = userEvent.setup();
      const tile = await screen.findByRole("button", { name: "Open Kept picture" });
      await user.click(tile);
      await user.click(screen.getByRole("button", { name: "Actions for Kept picture" }));
      await user.click(screen.getByRole("menuitem", { name: "Delete" }));
      const confirmation = await screen.findByRole("dialog", { name: "Delete Kept picture?" });
      const confirm = within(confirmation).getByRole("button", { name: "Delete" });
      expect(sessionReads).toBe(0);
      expect(deletes).toHaveLength(0);

      await user.click(confirm);
      await waitFor(() => expect(toast.error).toHaveBeenCalled());
      await waitFor(() => expect(confirm).toBeEnabled());
      expect(confirmation).toBeVisible();
      expect(deleted).toBe(false);
      expect(new URLSearchParams(window.location.search).get("asset")).toBe("kept-picture");
      expect(tile.isConnected).toBe(true);
      expect(deletes).toHaveLength(failure === "delete-403" ? 1 : 0);

      await user.click(confirm);
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "Delete Kept picture?" })).toBeNull());
      await waitFor(() => expect(screen.queryByRole("button", { name: "Open Kept picture" })).toBeNull());
      expect(sessionReads).toBe(2);
      expect(deleted).toBe(true);
      const last = deletes[deletes.length - 1];
      expect(last.path).toBe("/me/api/media/kept-picture");
      expect(last.init.credentials).toBe("same-origin");
      expect(new Headers(last.init.headers).get("x-azizam-user-action")).toBe("1");
      expect(new Headers(last.init.headers).get("x-azizam-csrf")).toBe("fixture-csrf-2");
      expect(new URLSearchParams(window.location.search).has("asset")).toBe(false);
      view.queryClient.clear();
    },
  );
});
