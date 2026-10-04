import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import HomePage from "@/app/home/page";
import LibraryPage from "@/app/library/page";
import { ViewerContext } from "@/components/shell/session";
import { PRODUCT_NAME } from "@/lib/product";

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
const home = {
  authenticated: true,
  user: { id: "alex", name: "Alex", email: null, role: "member" },
  apps: [],
  public_base_url: "https://gateway.example",
  api_base_url: "https://gateway.example/openai/v1",
  capabilities: [
    {
      capability: "image",
      intent: "image.generate",
      model: "image",
      available: true,
      status: "ready",
      reason: "Images are ready.",
    },
  ],
  aliases: [],
  credential_aliases: {},
  new_app_aliases: [],
  status: { state: "ready", reason: "Your models are ready." },
  receipts: [],
};
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
describe("Home and Library polish", () => {
  it("uses the configured instance name and real recent image URL", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        switch (String(input)) {
          case "/.well-known/ai-gateway":
            return Response.json({ service: { name: "Family gateway" } });
          case "/me/api/home":
            return Response.json(home);
          case "/me/api/media?limit=5":
            return Response.json({ assets: [assets[0]] });
          default:
            throw new Error(`Unexpected request: ${input}`);
        }
      }),
    );
    const { container } = renderPage(<HomePage />);
    expect(
      await screen.findByText(`Welcome to ${PRODUCT_NAME}. Family gateway`),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { level: 1, name: "Hello, Alex." }),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Home" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
    expect(screen.getByText("App setup: not verified")).toBeVisible();
    expect(screen.getByRole("button", { name: "Gateway check" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Test connection" }),
    ).toBeNull();
    expect(await screen.findByText("An ocean")).toBeVisible();
    expect(
      container.querySelector('img[src="/signed/image1"]'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Images" }).closest("article"),
    ).toHaveClass("bg-capability-image/5");
  });
  it("does not infer client setup from a used key or a completed gateway checklist", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        switch (String(input)) {
          case "/.well-known/ai-gateway":
            return Response.json({ service: { name: "Family gateway" } });
          case "/me/api/home":
            return Response.json({
              ...home,
              user: { ...home.user, role: "owner" },
              receipts: [
                {
                  request_id: "real-request",
                  status: "succeeded",
                  requested_model: "image",
                  intent: "image.generate",
                },
              ],
            });
          case "/me/api/media?limit=5":
            return Response.json({ assets: [assets[0]] });
          case "/admin/api/setup-progress":
            return Response.json({
              complete: true,
              steps: [
                {
                  id: "connect_service",
                  done: true,
                  reason: "A connected service is healthy.",
                  href: "",
                  optional: false,
                },
                {
                  id: "test_request",
                  done: true,
                  reason: "A request has succeeded.",
                  href: "",
                  optional: false,
                },
                {
                  id: "connect_app",
                  done: true,
                  reason: "An app key has been used.",
                  href: "",
                  optional: false,
                },
              ],
            });
          default:
            throw new Error(`Unexpected request: ${input}`);
        }
      }),
    );
    renderPage(<HomePage />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Open WebUI" }),
    );
    const summary = await screen.findByText("App setup: not verified");
    expect(
      await screen.findByRole("heading", { name: "What you can use" }),
    ).toBeVisible();
    expect(await screen.findByText("An ocean")).toBeVisible();
    await userEvent.click(summary);
    expect(
      screen.getByText(
        /Copy these settings into Open WebUI\. The gateway check verifies the route and key/,
      ),
    ).toBeVisible();
    expect(screen.queryByText("An app key has been used.")).toBeNull();
    expect(
      screen.getByRole("link", { name: /Image request.*Succeeded/ }),
    ).toHaveAttribute("href", "/r/real-request");
  });
  it("shows unknown and ready-to-start capability states without calling them unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        switch (String(input)) {
          case "/.well-known/ai-gateway":
            return Response.json({ service: { name: "Family gateway" } });
          case "/me/api/home":
            return Response.json({
              ...home,
              capabilities: [
                {
                  capability: "image",
                  intent: "image.generate",
                  model: "image",
                  available: true,
                  status: "unknown",
                  reason: "No health endpoint is configured.",
                },
                {
                  capability: "decide",
                  intent: "decision.systemone",
                  model: "decide",
                  available: true,
                  status: "standby_stopped",
                  reason: "Starts when you send a request.",
                },
              ],
            });
          case "/me/api/media?limit=5":
            return Response.json({ assets: [] });
          default:
            throw new Error(`Unexpected request: ${input}`);
        }
      }),
    );
    renderPage(<HomePage />);
    await screen.findByRole("heading", { name: "Images" });
    expect(screen.queryByText("Unknown")).toBeNull();
    await userEvent.click(screen.getAllByText("Details")[0]);
    expect(screen.getByText("No health endpoint is configured.")).toBeVisible();
    expect(screen.getByText("Ready to start")).toBeVisible();
    await userEvent.click(screen.getAllByText("Details")[1]);
    expect(screen.getByText("Starts when you send a request.")).toBeVisible();
    expect(screen.queryByText("Unavailable right now")).toBeNull();
  });
  it("keeps audio controls outside the open action and uses a duration fallback for video", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ assets, models: [] })),
    );
    const { container } = renderPage(<LibraryPage />);
    await screen.findByRole("button", { name: "Open Ocean sounds" });
    const audio = container.querySelector("audio");
    expect(audio).not.toHaveAttribute("controls");
    expect(audio).toHaveAttribute("preload", "metadata");
    const play = screen.getByRole("button", { name: "Play Ocean sounds" });
    expect(
      screen.getByRole("button", { name: "Open Ocean sounds" }).contains(play),
    ).toBe(false);
    expect(audio).toHaveAttribute("src", "/signed/audio1");
    expect(audio?.closest("button")).toBeNull();
    expect(container.querySelector("video")).toBeNull();
    expect(screen.getByText("0:06")).toBeVisible();
    expect(screen.getByText("0:12")).toBeVisible();
    const playAudio = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue();
    await userEvent.click(play);
    expect(playAudio).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "Open Ocean sounds" }),
    );
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(screen.getByLabelText("Generated audio")).toHaveAttribute(
      "controls",
    );
  });
  it("resolves a supplied poster through the scoped signed-download API", async () => {
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
    const { container } = renderPage(<LibraryPage />, true);
    await screen.findByRole("button", { name: "Open Swimming fish" });
    await waitFor(() =>
      expect(
        container.querySelector('img[src="/signed/poster1"]'),
      ).toBeInTheDocument(),
    );
    expect(requests).toContain("/me/api/media/poster1/download?scope=everyone");
    screen.getByRole("combobox", { name: "Owner" }).focus();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: () => {},
    });
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("option", { name: "Mine" }));
    await waitFor(() =>
      expect(requests).toContain("/me/api/media/poster1/download?scope=me"),
    );
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
    await screen.findByRole("button", { name: "Open Swimming fish" });
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
