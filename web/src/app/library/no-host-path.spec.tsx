import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { GGUF, expectNoHostPath, readableText } from "../../../tests/helpers/host-path";
import LibraryPage from "./page";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("next/image", () => ({
  // eslint-disable-next-line @next/next/no-img-element
  default: ({ alt, src }: { alt: string; src: string }) => <img alt={alt} src={src} />,
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: "member", name: "Alex" }),
}));
vi.mock("@/lib/product-api", () => ({
  productApi: { home: async () => ({ receipts: [] }), models: async () => ({ aliases: [], models: [] }) },
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("Library shows no host path in a creation's details", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        assets: [
          {
            asset_id: "asset_1",
            kind: "image",
            prompt: "A lighthouse",
            model: GGUF,
            created_at: 1_760_000_000,
            download_url: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E",
          },
        ],
        models: [GGUF],
        next_cursor: null,
      }),
    ),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <LibraryPage />
    </QueryClientProvider>,
  );
  await userEvent.setup().click(
    await screen.findByRole("button", { name: /Open A lighthouse/ }),
  );
  expect(await screen.findByRole("dialog")).toBeVisible();
  expectNoHostPath(expect, "library");
  // Open the Details disclosure too: the model name lives inside it.
  const user = userEvent.setup();
  await user.click(screen.getByText("Details", { selector: "summary" }));
  expect(screen.getByText("Model name")).toBeVisible();
  expect(readableText()).toContain("Qwen3.8-27B-Uncensored-Q4_K_M.gguf");
  expectNoHostPath(expect, "library details");
});
