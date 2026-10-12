// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement, Profiler } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import HomePage from "@/app/_home/home-page";
import { homeApi } from "@/app/_home/home-api";
import { accountApi } from "@/lib/account-api";
import { chatAlias } from "@/app/_home/recipes";
import {
  capabilitySummary,
  capabilityTiles,
} from "@/app/_home/presentation";
import type { HomeAlias, HomeCapability } from "@/app/_home/home-api";

vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: "member", admin: false, name: "Alex" }),
}));
vi.mock("@/lib/use-capability-views", () => ({
  useCapabilityViews: () => ({
    views: [],
    requests: [],
    verdict: { tone: "off", text: "No tasks available", problems: [], checkedAt: null, unconfirmed: false },
  }),
}));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("resets a failed Home image on the first source-change commit", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(["account", "home"], {
    authenticated: true,
    user: { id: "alex", role: "member", name: "Alex", email: "alex@example.test" },
    apps: [],
    capabilities: [],
    aliases: [],
    receipts: [],
    api_base_url: "https://example.test/v1",
    public_base_url: "https://example.test",
  });
  const creation = {
    asset_id: "home-image",
    kind: "image",
    prompt: "Reset Home image",
    download_url: "/home-old.png",
    available: true,
  };
  client.setQueryData(["library", "home-recent-creations"], { assets: [creation] });
  const commits: Array<string | null> = [];
  render(
    createElement(QueryClientProvider, { client },
      createElement(Profiler, {
        id: "home-media",
        onRender: () => {
          // Only commits rendered after the cache holds the new source count;
          // earlier commits (pending queries, timers) legitimately show the failure.
          const cached = client.getQueryData<{ assets: Array<{ download_url: string }> }>(
            ["library", "home-recent-creations"],
          );
          if (cached?.assets[0]?.download_url !== "/home-new.png") return;
          commits.push(screen.queryByRole("img", { name: "Reset Home image" })?.getAttribute("src") ?? null);
        },
      }, createElement(HomePage)),
    ),
  );
  fireEvent.error(screen.getByRole("img", { name: "Reset Home image" }));
  expect(screen.queryByRole("img", { name: "Reset Home image" })).toBeNull();
  act(() => {
    client.setQueryData(["library", "home-recent-creations"], {
      assets: [{ ...creation, download_url: "/home-new.png" }],
    });
  });
  await waitFor(() => expect(commits.length).toBeGreaterThan(0));
  expect(commits[0]).toBe("/home-new.png");
});

const row = (
  capability: string,
  intent: string,
  model: string,
  status: HomeCapability["status"] = "ready",
): HomeCapability => ({
  capability,
  intent,
  model,
  workflow: model,
  status,
  available: status === "ready",
  reason_code: status === "ready" ? "ready" : "unavailable_disabled",
});
const alias = (
  id: string,
  operation: string,
  status: HomeAlias["status"] = "ready",
): HomeAlias => ({
  id,
  operation,
  status,
  available: status === "ready",
  reason_code: status === "ready" ? "ready" : "unavailable_disabled",
});

describe("Home presentation", () => {
  it("uses alias-map generation entries instead of ready analysis or edit workflows", () => {
    const tiles = capabilityTiles(
      [
        row("image", "image.analyze", "image.analyze.face-likeness"),
        row("image", "image.generate", "image.generate.some-workflow"),
        row("video", "video.edit", "video.edit.bernini-r-remix"),
        row("audio", "audio.generate", "audio.generate.ace-step-1.5-2b-base"),
      ],
      [
        alias("image", "images", "unavailable"),
        alias("video", "videos", "starting"),
        alias("audio", "audio_generate"),
      ],
    );
    expect(
      tiles.map((tile) => [tile.capability, tile.model, tile.status]),
    ).toEqual([
      ["image", "image", "unavailable"],
      ["video", "video", "starting"],
      ["audio", "audio", "ready"],
    ]);
    expect(tiles.every((tile) => !tile.workflow)).toBe(true);
  });

  it("uses the recommended generation alias instead of a faster alternate", () => {
    const tiles = capabilityTiles(
      [],
      [
        alias("image", "images", "unavailable"),
        alias("drawings", "images"),
        alias("inspect", "image.analyze"),
      ],
    );
    expect(tiles).toHaveLength(1);
    expect(tiles[0].model).toBe("image");
    expect(tiles[0].status).toBe("unavailable");
  });

  it("does not expose workflow IDs when no generation alias exists", () => {
    const tiles = capabilityTiles(
      [
        row("image", "image.analyze", "secret-analysis"),
        row("video", "video.generate", "opaque-workflow"),
      ],
      [],
    );
    expect(tiles).toHaveLength(1);
    expect(tiles[0].capability).toBe("video");
    expect(tiles[0].model).toBeNull();
    expect(tiles[0].workflow).toBeNull();
  });

  it("names partial readiness, starting, busy, and unavailable from the same tile states", () => {
    expect(
      capabilitySummary([
        row("chat", "chat", "fast"),
        row("image", "images", "image", "unavailable"),
        row("video", "videos", "video", "unavailable"),
      ]),
    ).toBe("Chat is ready · images and video are unavailable");
    expect(
      capabilitySummary([
        row("image", "images", "image", "starting"),
        row("voice", "speech", "tts", "busy"),
      ]),
    ).toBe("Images are starting · voice is busy");
    expect(capabilitySummary([row("image", "images", "image", "degraded")])).toBe(
      "Images are degraded",
    );
    expect(capabilitySummary([])).toBe(
      "No tasks are available to your account yet.",
    );
  });
});

it("keeps the decision capability from the decisions alias operation", () => {
  const tiles = capabilityTiles(
    [row("decide", "decision.systemone", "decider-internal")],
    [alias("decide", "decisions")],
  );
  expect(tiles[0].model).toBe("decide");
  expect(tiles[0].capability).toBe("decide");
});

describe("Home advertised choices", () => {
  it("uses the available recommendation or an available paid, renamed chat alias", () => {
    const paid = { ...alias("renamed-paid-chat", "chat.completions"), local: false };
    expect(chatAlias([paid])).toBe(paid.id);
    expect(chatAlias([alias("other-chat", "chat"), paid], paid.id)).toBe(paid.id);
    expect(chatAlias([alias("offline", "chat", "unavailable"), paid], "offline")).toBe(paid.id);
    expect(chatAlias([alias("default", "chat", "unavailable")])).toBeNull();
    expect(chatAlias([alias("image-only", "images")])).toBeNull();
    expect(chatAlias([paid], "not-allowed")).toBe(paid.id);
  });

  it("keeps renamed service-advertised capability rows", () => {
    const recommended = { ...row("chat", "chat", "renamed-chat"), title: "Conversation", use_name: "renamed-chat" };
    expect(capabilityTiles([recommended], [])[0].model).toBe("renamed-chat");
    expect(capabilityTiles([], [alias("renamed-picture", "images")])[0].model).toBe("renamed-picture");
  });
});

function renderHome(aliases: HomeAlias[] = [], assets: object[] = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(["account", "home"], {
    authenticated: true,
    user: { id: "alex", role: "member", name: "Alex", email: "alex@example.test" },
    apps: [], capabilities: [], aliases, new_app_aliases: aliases, receipts: [],
    api_base_url: "https://example.test/v1", public_base_url: "https://example.test",
  });
  client.setQueryData(["library", "home-recent-creations"], { assets });
  render(createElement(QueryClientProvider, { client }, createElement(HomePage)));
  return client;
}

it("shows empty tasks and Recent while keeping app setup available", () => {
  renderHome();
  // The status line already says who can turn something on (verdict "off").
  expect(screen.getByText("Nothing is available yet.", { exact: true })).toBeTruthy();
  expect(screen.queryByText(/Ask the gateway owner for access/)).toBeNull();
  expect(screen.getByText("Your creations will appear here.")).toBeTruthy();
  expect(screen.getByText("Add an app")).toBeTruthy();
});

it("does not show a no-chat warning for an available renamed paid model", () => {
  renderHome([{ ...alias("paid-conversation", "chat.completions"), local: false }]);
  fireEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
  expect(screen.getByRole("textbox", { name: "Model" }).getAttribute("value")).toBe("paid-conversation");
  expect(screen.queryByText(/This key has no chat models|default model/)).toBeNull();
});

it("distinguishes unavailable chat from no chat access", () => {
  renderHome([alias("offline-chat", "chat", "unavailable")]);
  fireEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
  expect(screen.getByText("Chat is unavailable right now. Try again later.")).toBeTruthy();
  expect(screen.queryByText(/This key has no chat models/)).toBeNull();
});

it.each(["rejected", "missing"] as const)("falls back to the video when the poster is %s", async (failure) => {
  const download = vi.spyOn(homeApi, "mediaDownload");
  if (failure === "rejected") download.mockRejectedValue(new Error("poster failed"));
  else download.mockResolvedValue({ download_url: "" } as Awaited<ReturnType<typeof homeApi.mediaDownload>>);
  renderHome([], [{
    asset_id: "video", kind: "video", prompt: "A video", available: true,
    poster_asset_id: "poster", download_url: "/video.mp4",
  }]);
  // The video itself is a truthful preview; a missing poster is not a failure.
  await waitFor(() => expect(document.querySelector("video")).not.toBeNull());
  expect(screen.queryByText("Preview unavailable")).toBeNull();
});

it("offers Library when neither the poster nor the video loads", async () => {
  vi.spyOn(homeApi, "mediaDownload").mockRejectedValue(new Error("poster failed"));
  renderHome([], [{
    asset_id: "video", kind: "video", prompt: "A video", available: true,
    poster_asset_id: "poster", download_url: "/video.mp4",
  }]);
  await waitFor(() => expect(document.querySelector("video")).not.toBeNull());
  fireEvent.error(document.querySelector("video")!);
  expect(screen.getByText("Open in Library")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Open Video in Library: A video" }).getAttribute("href")).toContain("asset=video");
});

it("locks audio renewal and stops when the renewed file is missing", async () => {
  let resolve!: (value: Awaited<ReturnType<typeof homeApi.mediaDownload>>) => void;
  const renew = vi.spyOn(homeApi, "mediaDownload").mockImplementation(() => new Promise((done) => { resolve = done; }));
  const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  const load = vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  renderHome([], [{
    asset_id: "audio", kind: "audio", prompt: "A song", available: true, download_url: "/old.wav",
  }]);
  fireEvent.error(document.querySelector("audio")!);
  fireEvent.click(screen.getByRole("button", { name: "Play A song" }));
  const pending = screen.getByRole("button", { name: "Loading audio…" }) as HTMLButtonElement;
  expect(pending.disabled).toBe(true);
  fireEvent.click(pending);
  expect(renew).toHaveBeenCalledTimes(1);
  await act(async () => { resolve({ download_url: "" } as Awaited<ReturnType<typeof homeApi.mediaDownload>>); });
  expect(screen.getByText("Audio isn't available. Open it in Library.")).toBeTruthy();
  expect((screen.getByRole("button", { name: "Play A song" }) as HTMLButtonElement).disabled).toBe(true);
  expect(play).not.toHaveBeenCalled();
  expect(load).not.toHaveBeenCalled();
});

it("allows retry after an audio renewal failure and plays the renewed source", async () => {
  const renew = vi.spyOn(homeApi, "mediaDownload")
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce({ download_url: "/renewed.wav" } as Awaited<ReturnType<typeof homeApi.mediaDownload>>);
  const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  renderHome([], [{
    asset_id: "audio-retry", kind: "audio", prompt: "Retry song", available: true, download_url: "/expired.wav",
  }]);
  fireEvent.error(document.querySelector("audio")!);
  fireEvent.click(screen.getByRole("button", { name: "Play Retry song" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "Play Retry song" }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.getByText("Audio preview unavailable. Play to retry.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Play Retry song" }));
  await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
  expect(renew).toHaveBeenCalledTimes(2);
  expect(document.querySelector("audio")!.getAttribute("src")).toBe("/renewed.wav");
});

it("explains a key creation failure without leaking the exception", async () => {
  vi.spyOn(accountApi, "createApp").mockRejectedValue(new Error("InternalException HTTP 503"));
  renderHome([alias("allowed-chat", "chat")]);
  fireEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
  fireEvent.click(screen.getByRole("button", { name: "Create key for Open WebUI" }));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("We couldn't tell where key creation stopped. Try again."));
  expect(screen.queryByText(/InternalException|HTTP 503/)).toBeNull();
});
