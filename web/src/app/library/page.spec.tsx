/* eslint-disable @next/next/no-img-element */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import LibraryPage from "./page";

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
  useViewer: () => ({ role: "member" }),
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const assetWithReceipt = {
  asset_id: "asset_image_1",
  request_id: "req_image_123",
  kind: "image",
  prompt: "",
  model: "image",
  created_at: 1_760_000_000,
  download_url:
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E",
  width: 64,
  height: 64,
};

const assetWithoutReceipt = {
  ...assetWithReceipt,
  asset_id: "asset_no_receipt",
  request_id: null,
  prompt: "A saved image with no request receipt",
};

function renderLibrary() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <LibraryPage />
    </QueryClientProvider>,
  );
}

describe("LibraryPage batch J provenance", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          assets: [assetWithReceipt],
          models: ["image"],
          next_cursor: null,
        }),
      ),
    );
  });

  afterEach(() => {
    cleanup();
  });

  test("links the request receipt and explains absent prompt provenance honestly", async () => {
    renderLibrary();

    (await screen.findByRole("button", { name: "Open Image" })).click();

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "View receipt req_image_123" }),
      ).toHaveAttribute("href", "/r/req_image_123"),
    );
    expect(
      screen.getByText(
        "Not recorded. The gateway does not report whether the prompt was omitted by policy or unavailable from the backend.",
      ),
    ).toBeVisible();
  });

  test("encodes receipt ids", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [{ ...assetWithReceipt, request_id: "req/image 123" }],
        models: ["image"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    (await screen.findByRole("button", { name: "Open Image" })).click();

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "View receipt req/image 123" }),
      ).toHaveAttribute("href", "/r/req%2Fimage%20123"),
    );
  });

  test("omits the receipt link when no request id is available", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [assetWithoutReceipt],
        models: ["image"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    (
      await screen.findByRole("button", {
        name: "Open A saved image with no request receipt",
      })
    ).click();

    await waitFor(() =>
      expect(
        screen.getAllByText("Request receipt").at(-1)?.nextSibling,
      ).toHaveTextContent("Not recorded"),
    );
    expect(
      screen.queryByRole("link", { name: /View receipt/ }),
    ).not.toBeInTheDocument();
  });

  test("does not create its own main landmark inside the app shell", async () => {
    renderLibrary();

    await waitFor(() => {
      expect(screen.queryByRole("status", { name: "Loading library" })).toBeNull();
    });
    expect(document.querySelectorAll("main")).toHaveLength(0);
  });
});
