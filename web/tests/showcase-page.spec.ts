// The static Home and Examples pages mounted in jsdom.
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
const homeHtml = read("index.html");
const html = read("examples/index.html");
const coreJs = read("core.js");
const appJs = read("app.js");

// Preserve the body to which testing-library's screen is bound.
function mountMarkup(markup: string) {
  const parsed = new DOMParser().parseFromString(markup, "text/html");
  parsed.querySelectorAll("script").forEach((script) => script.remove());
  document.body.className = parsed.body.className;
  document.body.innerHTML = parsed.body.innerHTML;
}

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
async function mountExamples(data: () => Example[] | null, routes: Record<string, unknown> = {}) {
  mountMarkup(html);
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url in routes) return json(routes[url]);
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
  document.body.className = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Examples page", () => {
  it("pairs request and reply with quiet details below the output", async () => {
    await mountExamples(() => [{ ...answer, chips: ["42 tokens"] }]);
    await screen.findByText("Air scatters blue light.");
    const example = document.querySelector(".example")!;
    const row = example.querySelector(".io-row")!;
    expect(example.firstElementChild).toHaveClass("ex-title");
    expect(Array.from(row.children, (el) => el.className)).toEqual(["io-col in", "io-col out"]);
    expect(Array.from(row.querySelectorAll(".col-head"), (el) => el.textContent)).toEqual(["Request", "Reply"]);
    const meta = row.querySelector(".out")!.lastElementChild!;
    expect(meta).toHaveClass("ex-meta");
    expect(meta).toHaveTextContent("Model name: local-chat");
    expect(meta).toHaveTextContent("42 tokens");
    expect(meta).toHaveTextContent("Ran in 1.2 s");
    expect(screen.queryByRole("button", { name: "Show all" })).toBeNull();
  });

  it("repeatedly expands long Markdown and JSON without changing recorded text", async () => {
    const text = Array.from({ length: 10 }, (_, i) => "Line " + i).join("\n");
    await mountExamples(() => [recorded("long", {
      outputs: [{ kind: "text", text }, { kind: "json", text }],
    })]);
    const buttons = await screen.findAllByRole("button", { name: "Show all" });
    expect(buttons).toHaveLength(2);
    expect(new Set(buttons.map((b) => b.getAttribute("aria-controls"))).size).toBe(2);
    for (const button of buttons) {
      const body = document.getElementById(button.getAttribute("aria-controls")!)!;
      expect(body).toHaveClass("is-clamped");
      expect(body.textContent).toContain("Line 9");
      for (let repeat = 0; repeat < 2; repeat++) {
        fireEvent.click(button);
        expect(button).toHaveAttribute("aria-expanded", "true");
        expect(button).toHaveTextContent("Show less");
        expect(body).not.toHaveClass("is-clamped");
        fireEvent.click(button);
        expect(button).toHaveAttribute("aria-expanded", "false");
        expect(button).toHaveTextContent("Show all");
        expect(body).toHaveClass("is-clamped");
      }
    }
  });

  it("renders the page shell and a recorded answer without app navigation", async () => {
    await mountExamples(() => [answer]);
    expect(await screen.findByText("Air scatters blue light.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "See it work" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Site" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main navigation" })).toBeNull();
  });

  it("introduces each card before its examples and does not repeat capabilities in status", async () => {
    await mountExamples(() => [answer]);
    await screen.findByText("Air scatters blue light.");
    const card = document.querySelector(".card")!;
    const header = card.querySelector("header")!;
    const description = card.querySelector(".desc")!;
    expect(description.textContent?.trim()).toBeTruthy();
    expect(card.querySelector(".chip.model")).toHaveTextContent("Model name: local-chat");
    const examples = card.querySelector(".examples")!;
    expect(header.compareDocumentPosition(description) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(description.compareDocumentPosition(examples) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(document.querySelector("#status .eng, #engines")).toBeNull();
    expect(within(document.querySelector<HTMLElement>("#status")!).getByRole("button", { name: "API key" })).toBeInTheDocument();
  });

  it("renders only safe Markdown nodes while preserving hostile API text literally", async () => {
    const hostile = '<img src=x onerror="window.pwned=1"><script>window.pwned=2</script>';
    const link = "[click](javascript:alert(1))";
    await mountExamples(() => [recorded("markdown", {
      title: hostile,
      inputs: [{ kind: "text", value: hostile }],
      outputs: [{ kind: "text", text: [
        "### A **bold** heading", "Normal **strong** text", "",
        "- First **item**", "- " + hostile, "3. Ordered item",
        "**<svg onload=alert(1)>**", link, "&lt;iframe src=x&gt;",
        '<a href="javascript:alert(1)">unsafe</a>', "**unclosed",
      ].join("\n") }],
    })]);
    await waitFor(() => expect(document.querySelector(".recorded-text")).not.toBeNull());
    const output = document.querySelector<HTMLElement>(".recorded-text")!;
    expect(output.querySelector(".recorded-heading strong")).toHaveTextContent("A bold heading");
    expect(Array.from(output.querySelectorAll("strong"), (el) => el.textContent)).toContain("strong");
    expect(output.querySelectorAll("ul li")).toHaveLength(2);
    expect(output.querySelector("ol")).toHaveAttribute("start", "3");
    expect(output.querySelector("ol li")).toHaveTextContent("Ordered item");
    expect(output.querySelector("br")).not.toBeNull();
    for (const text of [hostile, link, "&lt;iframe src=x&gt;", "**unclosed", "<svg onload=alert(1)>"]) {
      expect(output.textContent).toContain(text);
    }
    expect(output.querySelector("img, script, svg, iframe, a")).toBeNull();
    expect(document.querySelector(".ex-title img, .in-text img")).toBeNull();
    expect(Array.from(output.querySelectorAll("*")).every((el) =>
      ["DIV", "STRONG", "BR", "UL", "OL", "LI"].includes(el.tagName))).toBe(true);
    expect(appJs).not.toMatch(/innerHTML|insertAdjacentHTML/);
  });

  it.each([["default"], ["default", "fast"]])("labels model names from discovery without Alias jargon: %s", async (...names) => {
    await mountExamples(() => [answer], {
      "/gateway/v1/workflows": { workflows: [] },
      "/openapi.json": {},
      "/gateway/v1/capabilities": { capabilities: [] },
      "/gateway/v1/aliases": { operations: { chat: { stable: names.map((alias) => ({ alias, resolves_to: "local" })) } } },
    });
    await screen.findByText("Air scatters blue light.");
    expect(document.querySelector(".alias")).toHaveTextContent(names.length === 1 ? "Model name: default" : "Model names: default, fast");
    expect(screen.getByLabelText("Model name")).toBeInTheDocument();
    expect(document.querySelector(".card")!.textContent).not.toMatch(/Alias|Model alias/);
  });

  it("keeps recorded JSON literal instead of treating it as Markdown", async () => {
    await mountExamples(() => [recorded("json", {
      outputs: [{ kind: "json", text: '{"text":"**literal** <img src=x>"}' }],
    })]);
    await screen.findByText('{"text":"**literal** <img src=x>"}');
    const output = document.querySelector("pre.out-text")!;
    expect(output.querySelector("strong, img")).toBeNull();
  });

  it("retries a failed recorded-example request from the banner", async () => {
    let unavailable = true;
    await mountExamples(() => (unavailable ? null : [answer]));
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
    const { requested } = await mountExamples(() => [
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
    await mountExamples(() => [
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
    await mountExamples(() => [
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
    await mountExamples(() => [
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

describe("Home page", () => {
  it("has Site navigation and exactly two primary destination links", () => {
    mountMarkup(homeHtml);
    expect(screen.getByRole("navigation", { name: "Site" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main navigation" })).toBeNull();
    const ctas = Array.from(document.querySelectorAll("a[data-cta]"));
    expect(ctas).toHaveLength(2);
    expect(ctas.map((link) => link.getAttribute("href"))).toEqual([
      "/showcase/docs/start/install/",
      "/showcase/examples/",
    ]);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Your own AI gateway");
    expect(document.querySelector("#sections, #status, #apikey")).toBeNull();
    expect(document.querySelectorAll(".mini-visual[aria-label]")).toHaveLength(4);
    expect(document.querySelector("#proof-toggle")).toHaveAttribute("hidden");
    expect(document.querySelector("#proof-reply")).not.toHaveClass("is-clamped");
  });

  it("runs its declared script without gateway requests, including menu and copy interactions", async () => {
    const parsed = new DOMParser().parseFromString(homeHtml, "text/html");
    const scripts = Array.from(parsed.querySelectorAll("script"));
    expect(scripts.map((script) => script.getAttribute("src"))).toEqual(["/showcase/static/site.js"]);
    mountMarkup(homeHtml);
    const fetchSpy = vi.fn();
    const xhrSpy = vi.fn();
    const socketSpy = vi.fn();
    const beaconSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    vi.stubGlobal("XMLHttpRequest", xhrSpy);
    vi.stubGlobal("WebSocket", socketSpy);
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn() })));
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) }, sendBeacon: beaconSpy });
    vi.useFakeTimers();
    try {
      for (const script of scripts) (0, eval)(read(script.getAttribute("src")!.split("/").pop()!));
      const toggle = screen.getByRole("button", { name: "Show full reply" });
      const reply = document.getElementById("proof-reply")!;
      expect(reply).toHaveClass("is-clamped");
      for (let repeat = 0; repeat < 2; repeat++) {
        fireEvent.click(toggle);
        expect(toggle).toHaveAttribute("aria-expanded", "true");
        expect(toggle).toHaveTextContent("Show less");
        expect(reply).not.toHaveClass("is-clamped");
        fireEvent.click(toggle);
        expect(toggle).toHaveAttribute("aria-expanded", "false");
        expect(reply).toHaveClass("is-clamped");
      }
      fireEvent.click(screen.getByRole("button", { name: "Menu" }));
      expect(screen.getByRole("navigation", { name: "Site" })).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Copy" }));
      await Promise.resolve();
      expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(30_000);
      for (const spy of [fetchSpy, xhrSpy, socketSpy, beaconSpy]) expect(spy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
