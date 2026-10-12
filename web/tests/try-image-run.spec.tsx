import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";

const account = vi.hoisted(() => ({
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  params: new URLSearchParams(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => account.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));

const models = {
  aliases: [],
  models: [
    {
      id: "image-model",
      name: "Picture model",
      capabilities: [{ intent: "image.generate", input_modalities: ["text"] }],
      capability: "image",
      locality: "local",
    },
  ],
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const dropped = () => new TypeError("Failed to fetch");
const PICTURE =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";
const SUBMIT = "/gateway/v1/media/jobs/image-generations";
const POLL = "/gateway/v1/media/jobs/job_1";
const CONTENT = "/gateway/v1/media/jobs/job_1/content";

const fetchMock = vi.fn();
type Route = (url: string, init?: RequestInit) => Response | Promise<Response>;
let routes: Record<string, Route>;

function calls(path: string) {
  return fetchMock.mock.calls.filter(([url]) => url === path);
}

beforeEach(() => {
  account.params = new URLSearchParams("capability=image");
  account.session.mockResolvedValue({
    authenticated: true,
    apps: [{ id: "try", name: "Try" }],
  });
  account.tryKey.mockResolvedValue({ id: "try-key", api_key: "test-key", reused: false, app_name: "Try", expires_at: 0 });
  routes = {
    [SUBMIT]: () =>
      json({ object: "media.job", id: "job_1", status: "queued" }, 202),
    [POLL]: () => json({ object: "media.job", id: "job_1", status: "succeeded" }),
    [CONTENT]: () => json({ data: [{ url: PICTURE }] }),
  };
  fetchMock.mockReset().mockImplementation(async (url: string, init?: RequestInit) => {
    const route = routes[url];
    if (route) return route(url, init);
    if (url.startsWith("/me/api/media?")) return json({ assets: [] });
    if (url.includes("/me/api/receipts/")) return json({}, 404);
    return json(url.includes("workflows") ? { workflows: [] } : models);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function startRun(user: ReturnType<typeof userEvent.setup>) {
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent("Picture model"),
  );
  await user.click(screen.getByRole("button", { name: "Run" }));
}

describe("Make a picture", () => {
  it("runs as a media job with an idempotency key and shows the picture", async () => {
    const user = userEvent.setup();
    await startRun(user);
    expect(
      await screen.findByAltText("Generated result", {}, { timeout: 5000 }),
    ).toHaveAttribute("src", PICTURE);
    const submits = calls(SUBMIT);
    expect(submits).toHaveLength(1);
    const init = submits[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["idempotency-key"]).toMatch(
      /^try-.+/,
    );
    expect(JSON.parse(init.body as string)).toMatchObject({
      model: "image-model",
    });
    // The slow synchronous route is no longer used for pictures.
    expect(calls("/openai/v1/images/generations")).toHaveLength(0);
  });

  it("keeps polling when the connection drops mid-run and never says it failed", async () => {
    const user = userEvent.setup();
    let polls = 0;
    routes[POLL] = () => {
      polls += 1;
      if (polls === 1) throw dropped();
      return json({ object: "media.job", id: "job_1", status: "succeeded" });
    };
    await startRun(user);
    expect(
      await screen.findByAltText("Generated result", {}, { timeout: 8000 }),
    ).toBeVisible();
    expect(polls).toBe(2);
    expect(calls(SUBMIT)).toHaveLength(1);
    expect(screen.queryByText(/Failed to fetch/i)).toBeNull();
    expect(screen.queryByText(/request failed/i)).toBeNull();
  }, 15000);

  it("polls again the moment the page is shown again", async () => {
    const user = userEvent.setup();
    let polls = 0;
    routes[POLL] = () => {
      polls += 1;
      return json({
        object: "media.job",
        id: "job_1",
        status: polls < 2 ? "running" : "succeeded",
      });
    };
    // The wake-up only works while the poller sits in its 2 s wait, and that
    // wait is the only 2000 ms timer on the page. Wait for the poller to be
    // there instead of guessing from the submit request.
    const timer = vi.spyOn(window, "setTimeout");
    const pollerWaits = () => timer.mock.calls.filter(([, ms]) => ms === 2000).length;
    await startRun(user);
    await waitFor(() => expect(pollerWaits()).toBe(1));
    // The poll interval is 2 s; the wake-up must not wait for it.
    expect(polls).toBe(0);
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(polls).toBe(1), { timeout: 1000 });
    await waitFor(() => expect(pollerWaits()).toBe(2));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(await screen.findByAltText("Generated result", {}, { timeout: 1500 })).toBeVisible();
    expect(polls).toBe(2);
  });

  describe("when the connection never comes back", () => {
    it("says the picture may still be finishing and points to the Library", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      routes[SUBMIT] = () => {
        throw dropped();
      };
      await startRun(user);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(
        await screen.findByText("Your picture may still be finishing"),
      ).toBeVisible();
      expect(screen.getByRole("link", { name: "Open Library" })).toHaveAttribute(
        "href",
        "/library/",
      );
      expect(screen.queryByText(/Failed to fetch/i)).toBeNull();
      expect(screen.queryByText("Make a picture request failed")).toBeNull();
      // The same key was sent each time, so no second picture was made.
      const keys = new Set(
        calls(SUBMIT).map(
          ([, init]) =>
            ((init as RequestInit).headers as Record<string, string>)[
              "idempotency-key"
            ],
        ),
      );
      expect(calls(SUBMIT).length).toBeGreaterThan(1);
      expect(keys.size).toBe(1);
    });

    it("shows the picture itself when the Library already has it", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      routes[SUBMIT] = () => {
        throw dropped();
      };
      const assetId = `img_${"e".repeat(64)}`;
      fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
        if (routes[url]) return routes[url](url, init);
        if (url.startsWith("/me/api/media?"))
          return json({
            assets: [
              {
                asset_id: assetId,
                kind: "image",
                prompt: "A calm teal fish mark on a white card.",
                created_at: Date.now() / 1000,
                available: true,
              },
            ],
          });
        if (url.includes("/me/api/receipts/")) return json({}, 404);
        return json(url.includes("workflows") ? { workflows: [] } : models);
      });
      await startRun(user);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(await screen.findByAltText("Generated result")).toHaveAttribute(
        "src",
        `/me/api/media/${assetId}/download?scope=me`,
      );
      expect(screen.queryByText(/may still be finishing/)).toBeNull();
    });
  });

  it("reports a job the gateway failed as a failure, with its own words", async () => {
    const user = userEvent.setup();
    routes[POLL] = () =>
      json({
        object: "media.job",
        id: "job_1",
        status: "failed",
        error: { message: "The engine ran out of memory." },
      });
    await startRun(user);
    expect(
      await screen.findByText("The engine ran out of memory.", {}, { timeout: 5000 }),
    ).toBeVisible();
    expect(screen.getByText("Make a picture request failed")).toBeVisible();
  });
});

describe("a dropped connection on any tab", () => {
  it("never shows the browser's own words", async () => {
    account.params = new URLSearchParams();
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") throw dropped();
      // Try picks only catalog models, so the default Chat tab needs one.
      return json(url.includes("workflows") ? { workflows: [] } : {
        ...models,
        models: [...models.models, {
          id: "chat-model",
          name: "Chat model",
          capabilities: [{ intent: "chat.completions", input_modalities: ["text"] }],
          capability: "chat",
          locality: "local",
        }],
      });
    });
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveTextContent(/model/i),
    );
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("The connection dropped")).toBeVisible();
    expect(document.body.textContent).not.toMatch(/Failed to fetch/i);
    expect(screen.getByRole("button", { name: "Edit and run again" })).toBeVisible();
  });
});
