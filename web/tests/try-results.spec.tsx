import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";

// WP-9: a Try result says honestly what came back, what failed and where,
// and keeps raw answers under Technical details.

const account = vi.hoisted(() => ({
  params: new URLSearchParams(),
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => account.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));

const models = {
  aliases: [],
  models: [
    { id: "chat", name: "Chat model", capabilities: ["chat"], locality: "local" },
    { id: "decide", name: "Decision model", capabilities: ["decide"], locality: "local" },
  ],
};
const workflows = {
  workflows: [
    {
      id: "wf",
      title: "Picture workflow",
      inputs: [],
      outputs: [{ kind: "image" }],
      example_request: { method: "POST", path: "/v1/workflows/wf", content_type: "application/json", body: {} },
    },
  ],
};
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
const fetchMock = vi.fn();
type Answer = (url: string, init?: RequestInit) => Response | undefined;
let answer: Answer = () => undefined;

beforeEach(() => {
  account.params = new URLSearchParams();
  account.session.mockResolvedValue({ authenticated: true, apps: [{ id: "try", name: "Try" }] });
  account.tryKey.mockResolvedValue({ id: "try-key", api_key: "test-key", reused: false, app_name: "Try" });
  answer = () => undefined;
  fetchMock.mockReset().mockImplementation(async (url: string, init?: RequestInit) =>
    answer(url, init) ?? json(url.includes("workflows") ? workflows : models),
  );
  vi.stubGlobal("fetch", fetchMock);
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:result" });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function run(capability = "chat", ready = "Chat model") {
  account.params = new URLSearchParams({ capability });
  const user = userEvent.setup();
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent(ready),
  );
  await user.click(screen.getByRole("button", { name: "Run" }));
  return user;
}

describe("PF-006 a failure names its layer, not an HTTP code", () => {
  it("names the layer the gateway gave", async () => {
    answer = (_url, init) =>
      init?.method === "POST"
        ? json({ error: { code: "model_not_deployed", message: "No engine is running this model right now.", details: { layer: "engine" } } }, 503)
        : undefined;
    await run();
    expect(await screen.findByText("The engine stopped this request")).toBeVisible();
    expect(screen.getByText("No engine is running this model right now.")).toBeVisible();
    expect(screen.queryByText(/HTTP 503/)).not.toBeVisible();
  });

  it("says so when nothing shows where it stopped", async () => {
    answer = (_url, init) => (init?.method === "POST" ? json({}, 502) : undefined);
    await run();
    expect(await screen.findByText("We couldn’t tell where this request stopped.")).toBeVisible();
    expect(screen.getByText("Chat request failed")).toBeVisible();
    expect(screen.queryByText("Request failed with HTTP 502.")).toBeNull();
  });

  it("names a model list that did not load in plain words, with one Retry", async () => {
    answer = (url) => (url.includes("models") ? json({}, 500) : undefined);
    account.params = new URLSearchParams();
    render(<TryPage />);
    expect(await screen.findByText("Models couldn’t be loaded")).toBeVisible();
    expect(screen.getByText("We couldn’t tell why the model list didn’t load.")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  });
});

describe("PF-007 an answer with nothing in it says what is missing", () => {
  it("a chat with no reply says so and keeps the payload under Technical details", async () => {
    answer = (_url, init) =>
      init?.method === "POST" ? json({ id: "x", choices: [] }, 200, { "x-azizam-request-id": "req-1" }) : undefined;
    const user = await run();
    expect(
      await screen.findByText("The gateway returned no reply. Open the receipt for details."),
    ).toBeVisible();
    const raw = screen.getByText(/"choices": \[\]/);
    expect(raw).not.toBeVisible();
    await user.click(screen.getByText("Technical details"));
    expect(raw).toBeVisible();
  });

  it("a decision with no probabilities says no answer was returned", async () => {
    answer = (_url, init) => (init?.method === "POST" ? json({ answer: { probabilities: {} } }) : undefined);
    const user = userEvent.setup();
    account.params = new URLSearchParams({ capability: "decide" });
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Decision model"),
    );
    await user.type(screen.getByRole("textbox", { name: /Options/ }), "yes{enter}no");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("No answer was returned.")).toBeVisible();
  });
});

describe("PF-008 and PF-010 the receipt", () => {
  it("is a plain View receipt link, with no id or copy button", async () => {
    answer = (url, init) =>
      init?.method === "POST"
        ? json({ choices: [{ message: { content: "Hi" } }] }, 200, { "x-azizam-request-id": "req-123456789" })
        : url.includes("/me/api/receipts/")
          ? json({})
          : undefined;
    await run();
    const link = await screen.findByRole("link", { name: "View receipt" });
    expect(link).toHaveAttribute("href", "/usage/?receipt=req-123456789");
    expect(screen.queryByText(/req-1234/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy receipt ID" })).toBeNull();
  });

  it("tells a missing receipt from a failed check, and retries only the check", async () => {
    let receipt = 500;
    answer = (url, init) =>
      init?.method === "POST"
        ? json({ choices: [{ message: { content: "Hi" } }] }, 200, { "x-azizam-request-id": "req-1" })
        : url.includes("/me/api/receipts/")
          ? json({}, receipt)
          : undefined;
    const user = await run();
    expect(await screen.findByText("Receipt couldn’t be checked.")).toBeVisible();
    expect(screen.queryByText("Not available for this session")).toBeNull();
    receipt = 200;
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").length;
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByRole("link", { name: "View receipt" })).toBeVisible();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(posts);
  });

  it("says no receipt was returned when there is none", async () => {
    answer = (url, init) =>
      init?.method === "POST"
        ? json({ choices: [{ message: { content: "Hi" } }] })
        : undefined;
    await run();
    expect(await screen.findByText("No receipt was returned.")).toBeVisible();
  });
});

describe("PF-009 media and jobs have their own states", () => {
  it("a picture that fails to load says so and can retry", async () => {
    answer = (_url, init) =>
      init?.method === "POST"
        ? new Response(new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" }), {
            headers: { "content-type": "image/png" },
          })
        : undefined;
    await run("workflow", "Picture workflow");
    const image = await screen.findByRole("img", { name: "Generated result" });
    expect(screen.getByText("Loading the picture…")).toBeVisible();
    fireEvent.error(image);
    expect(await screen.findByText("The picture couldn’t be loaded.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    fireEvent.load(await screen.findByRole("img", { name: "Generated result" }));
    expect(screen.queryByText("Loading the picture…")).toBeNull();
  });

  it("a job says its state in plain words and a finished empty job says so", async () => {
    answer = (url, init) => {
      if (init?.method === "POST")
        return json({ object: "media.job", id: "job-1", status: "queued" });
      if (url.endsWith("/job-1/content")) return json({});
      if (url.endsWith("/job-1")) return json({ object: "media.job", id: "job-1", status: "succeeded" });
      return undefined;
    };
    const user = await run("workflow", "Picture workflow");
    expect(await screen.findByText("Waiting to start")).toBeVisible();
    expect(screen.queryByText(/Job status/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText(/The workflow finished but returned no creation/)).toBeVisible();
    expect(screen.getByText("Ready")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
  });
});
