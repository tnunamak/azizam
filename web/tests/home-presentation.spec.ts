// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement, Profiler } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import HomePage from "@/app/home/page";
import {
  capabilitySummary,
  capabilityTiles,
  greetingName,
  requestLabel,
} from "@/app/home/presentation";
import type { HomeAlias, HomeCapability } from "@/app/home/home-api";

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

afterEach(cleanup);

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
  let capture = false;
  const commits: Array<string | null> = [];
  render(
    createElement(QueryClientProvider, { client },
      createElement(Profiler, {
        id: "home-media",
        onRender: () => {
          if (capture) commits.push(screen.queryByRole("img", { name: "Reset Home image" })?.getAttribute("src") ?? null);
        },
      }, createElement(HomePage)),
    ),
  );
  fireEvent.error(screen.getByRole("img", { name: "Reset Home image" }));
  expect(screen.queryByRole("img", { name: "Reset Home image" })).toBeNull();
  // Let the page's other queries settle so the first captured commit is the source change.
  await waitFor(() => expect(client.isFetching()).toBe(0));
  capture = true;
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
  it("greets by first display name and never invents a name from an email", () => {
    const user = {
      id: "person",
      role: "member",
      email: "tim@example.test",
      name: "  Tim Nunamak  ",
    };
    expect(greetingName(user)).toBe("Tim");
    expect(greetingName({ ...user, name: user.email })).toBe("there");
    expect(greetingName({ ...user, name: "" })).toBe("there");
  });

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

it("labels receipts from their capability or path, never from an opaque workflow ID", () => {
  expect(
    requestLabel(
      {
        request_id: "image-receipt",
        intent: "image.analyze",
        requested_model: "image.analyze.face-likeness",
      },
      [],
    ),
  ).toBe("Image request");
  expect(
    requestLabel(
      {
        request_id: "video-receipt",
        path: "/v1/videos/generations",
        requested_model: "video.edit.bernini-r-remix",
      },
      [],
    ),
  ).toBe("Video request");
  expect(
    requestLabel({ request_id: "chat-receipt", requested_model: "household" }, [
      alias("household", "chat"),
    ]),
  ).toBe("Chat request");
  expect(
    requestLabel(
      { request_id: "unknown", requested_model: "opaque-routing-id" },
      [],
    ),
  ).toBe("Request");
});

it("keeps the decision capability from the decisions alias operation", () => {
  const tiles = capabilityTiles(
    [row("decide", "decision.systemone", "decider-internal")],
    [alias("decide", "decisions")],
  );
  expect(tiles[0].model).toBe("decide");
  expect(tiles[0].capability).toBe("decide");
});
