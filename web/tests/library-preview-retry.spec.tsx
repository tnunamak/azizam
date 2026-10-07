import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LibraryPage from "@/app/library/page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/product-api", () => ({
  productApi: { models: async () => ({ models: [], aliases: [] }) },
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each(["rejected", "missing URL", "JSON null", "invalid JSON"])(
  "keeps the failed image preview visible after %s retry metadata",
  async (failure) => {
    let finish!: () => void;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/image/download")) {
        return new Promise<Response>((resolve, reject) => {
          finish = () =>
            failure === "rejected"
              ? reject(new Error("Metadata failed"))
              : failure === "missing URL"
                ? resolve(Response.json({}))
                : failure === "JSON null"
                  ? resolve(Response.json(null))
                  : resolve(new Response("not JSON"));
        });
      }
      return Response.json({
        assets: [
          {
            asset_id: "image",
            kind: "image",
            prompt: "Saved image",
            model: "",
            download_url: "/image.png",
          },
        ],
        next_cursor: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <LibraryPage />
      </QueryClientProvider>,
    );
    fireEvent.error(await screen.findByRole("img", { name: "Saved image" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    finish();
    expect(
      await screen.findByRole("img", { name: "Image preview unavailable" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    expect(screen.queryByRole("img", { name: "Saved image" })).toBeNull();
    client.clear();
  },
);
