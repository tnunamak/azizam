import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LibraryPage from "@/app/library/page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/product-api", () => ({
  productApi: {
    home: async () => ({ receipts: [] }),
    models: async () => ({ models: [], aliases: [] }),
  },
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

it("names an uncatalogued model readably and keeps the raw id only in Details", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({
    assets: [{
      asset_id: "retired-model",
      kind: "image",
      prompt: "Retired model image",
      model: "vendor/retired-model:v1",
      download_url: "/retired.png",
    }],
    next_cursor: null,
  })));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const { container } = render(
    <QueryClientProvider client={client}>
      <LibraryPage />
    </QueryClientProvider>,
  );
  await screen.findByRole("button", { name: "Open Retired model image" });
  // A tile's caption names the picture and its day, never the model; the
  // viewer's facts carry the model.
  const tile = container.querySelector("article")!;
  // (D1.6: the first tile of a day leads with its day chip.)
  expect(tile).toHaveTextContent(/^\w+.*Retired model image/);
  expect(tile).not.toHaveTextContent(/model\b.*v1|vendor/i);
  fireEvent.click(screen.getByRole("button", { name: "Open Retired model image" }));
  const dialog = await screen.findByRole("dialog");
  const modelFact = Array.from(dialog.querySelectorAll("dt")).find(
    (term) => term.textContent === "Model",
  );
  // Facts show a display name, never the raw id.
  expect(modelFact?.nextElementSibling).toHaveTextContent(
    /^Vendor Retired Model V1$/,
  );
  fireEvent.click(screen.getByText("Details", { selector: "summary" }));
  const modelDetail = Array.from(dialog.querySelectorAll("dl")).find(
    (detail) => detail.querySelector("dt")?.textContent === "Model name",
  );
  expect(modelDetail?.querySelector("dd")).toHaveTextContent("vendor/retired-model:v1");
  expect(
    within(modelDetail as HTMLElement).getByText("vendor/retired-model:v1"),
  ).toHaveClass("font-mono");
  client.clear();
});

it("labels the ComfyUI workflow link as a download", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({
    assets: [{
      asset_id: "workflow",
      kind: "image",
      prompt: "Workflow image",
      model: "",
      download_url: "/workflow.png",
      workflow_download_url: "/workflow.json",
    }],
    next_cursor: null,
  })));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <LibraryPage />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Open Workflow image" }));
  const link = await screen.findByRole("link", { name: "Download ComfyUI workflow" });
  expect(link).toHaveAttribute("href", "/workflow.json");
  expect(link).toHaveAttribute("download");
  expect(screen.queryByRole("link", { name: "Open in ComfyUI" })).toBeNull();
  client.clear();
});

it("pauses every other audio player when a card or drawer starts playing", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({
    assets: ["First clip", "Second clip"].map((prompt, index) => ({
      asset_id: `audio-${index}`,
      kind: "audio",
      prompt,
      model: "",
      download_url: `/audio-${index}.mp3`,
    })),
    next_cursor: null,
  })));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const { container } = render(
    <QueryClientProvider client={client}>
      <LibraryPage />
    </QueryClientProvider>,
  );
  await screen.findByRole("button", { name: "Play First clip" });
  const [first, second] = Array.from(container.querySelectorAll("audio"));
  const firstPause = vi.spyOn(first, "pause").mockImplementation(() => {
    fireEvent.pause(first);
  });
  const secondPause = vi.spyOn(second, "pause").mockImplementation(() => {
    fireEvent.pause(second);
  });
  try {
    fireEvent.play(first);
    expect(firstPause).not.toHaveBeenCalled();
    expect(secondPause).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Pause First clip" })).toBeVisible();
    fireEvent.play(second);
    expect(firstPause).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Play First clip" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Pause Second clip" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Open First clip" }));
    const drawer = await screen.findByLabelText("Generated audio");
    const drawerPause = vi.spyOn(drawer as HTMLAudioElement, "pause").mockImplementation(() => {
      fireEvent.pause(drawer);
    });
    try {
      firstPause.mockClear();
      secondPause.mockClear();
      fireEvent.play(drawer);
      expect(firstPause).toHaveBeenCalledTimes(1);
      expect(secondPause).toHaveBeenCalledTimes(1);
      expect(drawerPause).not.toHaveBeenCalled();
      firstPause.mockClear();
      secondPause.mockClear();
      fireEvent.play(first);
      expect(firstPause).not.toHaveBeenCalled();
      expect(secondPause).toHaveBeenCalledTimes(1);
      expect(drawerPause).toHaveBeenCalledTimes(1);
    } finally {
      drawerPause.mockRestore();
    }
  } finally {
    firstPause.mockRestore();
    secondPause.mockRestore();
    client.clear();
  }
});

it.each([
  // Only a confirmed missing or expired file is "no longer stored".
  ["missing_or_expired", "File no longer stored"],
  ["source_unavailable", "This file is unavailable."],
  [null, "This file is unavailable."],
  [undefined, "This file is unavailable."],
  ["   ", "This file is unavailable."],
])("shows unavailable reason %s with a working retry", async (reason, message) => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).includes("/unavailable/download")) {
      return Response.json({ download_url: "/recovered.png" });
    }
    return Response.json({
      assets: [{
        asset_id: "unavailable",
        kind: "image",
        prompt: "Unavailable image",
        model: "",
        available: false,
        unavailable_reason: reason,
      }],
      next_cursor: null,
    });
  }));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <LibraryPage />
    </QueryClientProvider>,
  );
  // Files the gateway no longer stores fold under one summary; open it first.
  fireEvent.click(await screen.findByText("Show them"));
  expect(await screen.findByText(message!)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(
    new URL(
      (await screen.findByRole("img", { name: "Unavailable image" })).getAttribute("src")!,
      window.location.href,
    ).pathname,
  ).toBe("/recovered.png");
  expect(screen.queryByText(message!)).toBeNull();
  client.clear();
});

it("marks retained results as updating until changed filter results arrive", async () => {
  let finish!: () => void;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const filtered = new URL(String(input), "http://localhost").searchParams.get("kind") === "image";
    const payload = {
      assets: [{
        asset_id: filtered ? "new" : "old",
        kind: "image",
        prompt: filtered ? "New result" : "Old result",
        model: "",
        download_url: filtered ? "/new.png" : "/old.png",
      }],
      next_cursor: null,
    };
    if (filtered) {
      return new Promise<Response>((resolve) => {
        finish = () => resolve(Response.json(payload));
      });
    }
    return Response.json(payload);
  }));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const { container } = render(
    <QueryClientProvider client={client}>
      <LibraryPage />
    </QueryClientProvider>,
  );
  await screen.findByRole("button", { name: "Open Old result" });
  expect(screen.queryByText("Updating")).toBeNull();
  expect(container.querySelector("article")?.closest("[aria-busy]")).toHaveAttribute("aria-busy", "false");
  fireEvent.click(within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: "Images" }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  expect(screen.getByRole("button", { name: "Open Old result" })).toBeVisible();
  expect(container.querySelector("article")?.closest("[aria-busy]")).toHaveAttribute("aria-busy", "true");
  expect(screen.getByText("Updating")).toBeVisible();
  expect(screen.getByText("Updating")).toHaveAttribute("role", "status");
  finish();
  await screen.findByRole("button", { name: "Open New result" });
  expect(screen.queryByRole("button", { name: "Open Old result" })).toBeNull();
  expect(container.querySelector("article")?.closest("[aria-busy]")).toHaveAttribute("aria-busy", "false");
  expect(screen.queryByText("Updating")).toBeNull();
  client.clear();
});
