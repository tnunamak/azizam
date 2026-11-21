/* eslint-disable @next/next/no-img-element */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { accountApi } from "@/lib/account-api";
import LibraryPage from "./page";

const state = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
  viewer: { role: "member", name: "Alex" },
}));

vi.mock("./model-preview", () => ({ default: () => null }));
vi.mock("next/navigation", () => ({ useSearchParams: () => state.searchParams }));
vi.mock("next/image", () => ({
  default: ({ alt, src }: { alt: string; src: string }) => <img alt={alt} src={src} />,
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => state.viewer }));
vi.mock("@/lib/product-api", () => ({
  productApi: {
    home: async () => ({ receipts: [] }),
    models: async () => ({ aliases: [], models: [] }),
  },
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/lib/toast", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

const adapter = {
  id: "ast_1",
  object: "gateway.asset",
  kind: "adapter",
  media_type: "adapter/safetensors",
  size_bytes: 3,
  architectures: ["flux"],
  offered: true,
};
const refusal = "Adapter uploads are off. The owner can turn them on.";

function renderLibrary(role = "member", refuse = false) {
  state.viewer = { role, name: "Alex" };
  const upload = vi.fn<(init: RequestInit | undefined) => void>();
  const list = vi.fn();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/me/api/adapters") {
      upload(init);
      return refuse
        ? Response.json({ error: { code: "adapter_uploads_off", message: refusal } }, { status: 403 })
        : Response.json(adapter);
    }
    if (url.pathname === "/me/api/media" && url.searchParams.get("folder_state") === "1") {
      return Response.json({ media_folder: null, removal_plan: null });
    }
    if (url.pathname === "/me/api/media") {
      list();
      return Response.json({ assets: [], models: [], next_cursor: null });
    }
    throw new Error("Unexpected request: " + url.pathname);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(accountApi, "session").mockResolvedValue({
    csrf_token: "t",
  } as Awaited<ReturnType<typeof accountApi.session>>);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <LibraryPage />
    </QueryClientProvider>,
  );
  return { ...rendered, list, upload, queryClient };
}

async function chooseAdapter(view: ReturnType<typeof renderLibrary>) {
  await waitFor(() => expect(view.list).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(view.queryClient.isFetching({ queryKey: ["library"] })).toBe(0));
  fireEvent.click(screen.getByRole("button", { name: "Upload your own style adapter" }));
  const input = view.container.querySelector<HTMLInputElement>("[data-adapter-file-input]");
  if (!input) throw new Error("Adapter input not rendered");
  fireEvent.change(input, { target: { files: [new File(["xyz"], "style.safetensors")] } });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Library adapter upload", () => {
  test.each(["member", "owner"])("offers upload to the signed-in %s", async (role) => {
    renderLibrary(role);
    expect(await screen.findByRole("button", { name: "Upload your own style adapter" })).toBeInTheDocument();
  });

  test("posts one multipart file and refreshes the Library after acceptance", async () => {
    const view = renderLibrary();
    await chooseAdapter(view);
    expect(await screen.findByText("Added to your Library and ready to pick.")).toBeInTheDocument();
    expect(view.upload).toHaveBeenCalledTimes(1);
    const init = view.upload.mock.calls[0]?.[0];
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeInstanceOf(FormData);
    expect(Array.from((init?.body as FormData).keys())).toEqual(["file"]);
    expect((init?.body as FormData).get("file")).toMatchObject({ name: "style.safetensors", size: 3 });
    await waitFor(() => expect(view.list).toHaveBeenCalledTimes(2));
  });

  test("shows the gateway refusal without refreshing the Library", async () => {
    const view = renderLibrary("member", true);
    await chooseAdapter(view);
    expect(await screen.findByRole("alert")).toHaveTextContent(refusal);
    expect(view.upload).toHaveBeenCalledTimes(1);
    expect(view.list).toHaveBeenCalledTimes(1);
  });
});
