// The static /showcase/ page (showcase/index.html + core.js + app.js) mounted in jsdom.
// The scripts are plain browser scripts, so each test loads them into a fresh document
// and answers fetch like the gateway did in the browser spec: discovery needs a key
// (401), the sections and the recorded-example index come from the case.
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../showcase");
const read = (name: string) => readFileSync(path.join(dir, name), "utf8");
const html = read("index.html");
const coreJs = read("core.js");
const appJs = read("app.js");

const sections = {
  sections: [
    { id: "chat", title: "Chat and vision", source: "aliases:chat" },
    { id: "speech", title: "Speech", source: "aliases:speech" },
    { id: "images", title: "Image generation", match: { intent: "image.generate" } },
    { id: "more", title: "More workflows", fallback: true },
  ],
};

type Example = Record<string, unknown>;

const recorded = (id: string, extra: Example): Example => ({
  id,
  base: `/showcase/examples/${id}/`,
  card: "chat",
  ...extra,
});

const answer = recorded("chat-recorded", {
  title: "A recorded answer",
  model: "local-chat",
  seconds: 1.2,
  inputs: [{ kind: "text", value: "Why is the sky blue?" }],
  outputs: [{ kind: "text", text: "Air scatters blue light." }],
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** Mount the page. `data` returning null answers the examples index with 503. */
async function mountShowcase(data: () => Example[] | null) {
  // testing-library's `screen` is bound to the original <body>; keep that element.
  document.body.innerHTML = /<body>([\s\S]*)<\/body>/.exec(html)![1].replace(
    /<script[^>]*><\/script>/g,
    "",
  );
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url.endsWith("/showcase/static/sections.json")) return json(sections);
      if (url.endsWith("/showcase/examples/index.json")) {
        const list = data();
        return list
          ? json({ object: "list", data: list })
          : json({ error: { code: "unavailable" } }, 503);
      }
      return json({ error: { code: "unauthorized" } }, 401);
    }),
  );
  // The status poll repeats every 10 s; one pass is enough here.
  vi.spyOn(globalThis, "setInterval").mockImplementation(() => 0 as never);
  const win = window as unknown as Record<string, unknown>;
  delete win.ShowcaseCore;
  (0, eval)(coreJs.replace("typeof module === \"object\" && module.exports", "false"));
  (0, eval)(appJs);
  return { requested };
}

const failedText = () => screen.queryAllByText("This recording could not be loaded.");
const tryAgain = (root: HTMLElement) => within(root).getByRole("button", { name: "Try again" });

afterEach(() => {
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("showcase page", () => {
  it("renders the page shell and a recorded answer without app navigation", async () => {
    await mountShowcase(() => [answer]);
    expect(await screen.findByText("Air scatters blue light.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main navigation" })).toBeNull();
  });

  it("retries a failed recorded-example request from the banner", async () => {
    let unavailable = true;
    await mountShowcase(() => (unavailable ? null : [answer]));
    const banner = document.querySelector<HTMLElement>("#banner")!;
    await waitFor(() =>
      expect(banner).toHaveTextContent("Recorded examples could not be loaded from the gateway."),
    );
    unavailable = false;
    fireEvent.click(tryAgain(banner));
    expect(await screen.findByText("Air scatters blue light.")).toBeInTheDocument();
    await waitFor(() => expect(banner).not.toHaveTextContent("Recorded examples could not"));
  });

  it("marks broken recordings, shows recorded falsy inputs and repairs on retry", async () => {
    let repaired = false;
    const { requested } = await mountShowcase(() => [
      recorded("invalid", {
        title: "Unavailable recording",
        inputs: [{ value: null }, { value: "   " }],
        outputs: repaired
          ? [{ kind: "text", text: "Recovered recording" }]
          : [null, { kind: "image", file: "../private.png" }, { kind: "text", text: " " }],
      }),
      recorded("recorded", {
        title: "Recorded values",
        inputs: [{ value: 0 }, { value: false }],
        outputs: [{ kind: "text", text: "Saved answer" }],
      }),
    ]);
    await waitFor(() => expect(failedText()).toHaveLength(3));
    expect(screen.getAllByText("Input was not recorded.")).toHaveLength(1);
    expect(Array.from(document.querySelectorAll("pre.in-text"), (el) => el.textContent)).toEqual([
      "0",
      "false",
    ]);
    expect(requested.filter((url) => url.includes("private.png"))).toEqual([]);
    expect(document.querySelector("img")).toBeNull();
    repaired = true;
    fireEvent.click(tryAgain(document.querySelector<HTMLElement>(".load-failed")!));
    expect(await screen.findByText("Recovered recording")).toBeInTheDocument();
    await waitFor(() => expect(failedText()).toHaveLength(0));
  });

  it("shows a structured recorded request as JSON", async () => {
    await mountShowcase(() => [
      recorded("structured", {
        title: "Recorded structured request",
        inputs: [
          {
            value: {
              model: "fast",
              max_tokens: 32,
              messages: [{ role: "user", content: "Hello" }],
            },
          },
        ],
        outputs: [{ kind: "text", text: "Recorded answer" }],
      }),
    ]);
    await screen.findByText("Recorded answer");
    const request = document.querySelector("pre.in-text")!;
    expect(request).toHaveTextContent('"model": "fast"');
    expect(request).toHaveTextContent('"messages": [');
  });

  it("retries an image that failed to load", async () => {
    await mountShowcase(() => [
      recorded("image-recorded", {
        outputs: [{ kind: "image", file: "output.png", label: "Recorded picture" }],
      }),
    ]);
    const first = await screen.findByRole("img", { name: "Generated image" });
    expect(first).toHaveAttribute("src", "/showcase/examples/image-recorded/output.png");
    fireEvent.error(first);
    expect(failedText()).toHaveLength(1);
    expect(screen.queryByRole("img", { name: "Generated image" })).toBeNull();
    fireEvent.click(tryAgain(document.body));
    const second = await screen.findByRole("img", { name: "Generated image" });
    expect(second).not.toBe(first);
    expect(second).toHaveAttribute("src", "/showcase/examples/image-recorded/output.png");
    expect(failedText()).toHaveLength(0);
  });

  it.each(["audio", "video"] as const)("can retry repeated %s playback errors", async (kind) => {
    await mountShowcase(() => [
      recorded("media-recorded", {
        outputs: [{ kind, file: "output.media", label: "Recorded media" }],
      }),
    ]);
    for (let attempt = 0; attempt < 2; attempt++) {
      await waitFor(() => expect(document.querySelectorAll(`.recorded-media ${kind}`)).toHaveLength(1));
      fireEvent.error(document.querySelector(`.recorded-media ${kind}`)!);
      expect(failedText()).toHaveLength(1);
      fireEvent.click(tryAgain(document.body));
      expect(document.querySelectorAll(`.recorded-media ${kind}`)).toHaveLength(1);
      expect(failedText()).toHaveLength(0);
    }
  });
});
