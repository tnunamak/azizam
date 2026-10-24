import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";

const account = vi.hoisted(() => ({
  session: vi.fn(),
  tryKey: vi.fn(),
  revokeKey: vi.fn(),
  requestHistory: vi.fn(),
  params: new URLSearchParams(),
  viewer: { authenticated: true, serviceAdmin: true, admin: false },
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => account.params }));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));
vi.mock("@/components/shell/session", () => ({ useViewer: () => account.viewer }));

// Chat rows shaped like gateway/models_catalog.py: the chat intent with its input modalities. A vision model
// takes "image" as well as "text".
const chatRow = (
  id: string,
  name: string,
  input: string[],
  extra: Record<string, unknown> = {},
) => ({
  id,
  name,
  display_name: name,
  capability: "chat",
  alias: false,
  use_names: [id],
  locality: "local",
  capabilities: [{ intent: "chat.completions", capability: "chat", input_modalities: input, output_modalities: ["text"] }],
  ...extra,
});
const catalog = {
  aliases: [
    // The catalog recommends a vision model for chat. The default must not start there.
    chatRow("default", "General vision chat", ["text", "image"], { alias: true, recommended: true, use_names: ["default"] }),
  ],
  models: [
    chatRow("vision-b", "Vision model B", ["text", "image"]),
    chatRow("writer", "Writing model", ["text"]),
    {
      id: "embedder",
      name: "Embedding model",
      display_name: "Embedding model",
      capability: "embed",
      alias: false,
      use_names: ["embedder"],
      locality: "local",
      capabilities: [{ intent: "text.embeddings", capability: "embed", input_modalities: ["text"], output_modalities: ["embedding"] }],
    },
  ],
};
const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

beforeEach(() => {
  account.params = new URLSearchParams();
  account.requestHistory.mockResolvedValue({
    keep: true,
    retention: "7_days",
    parts: ["accepted"],
    choices: ["1_day", "7_days", "30_days"],
    allowed_parts: ["accepted", "received"],
    notice_due: false,
  });
  account.session.mockResolvedValue({ authenticated: true, apps: [] });
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => json(url.includes("workflows") ? { workflows: [] } : catalog)),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("Chat starts on an ordinary chat model, with an empty prompt", () => {
  it("selects the ordinary chat model, not the recommended vision model", async () => {
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Writing model"),
    );
    expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("vision");
  });

  it("starts with an empty prompt that says what goes there", async () => {
    render(<TryPage />);
    const prompt = await screen.findByRole("textbox", { name: "Prompt" });
    expect(prompt).toHaveValue("");
    expect(prompt).toHaveAttribute("placeholder", "Ask a question or give a task");
  });

  it("fills a sample prompt with Try an example", async () => {
    const user = userEvent.setup();
    render(<TryPage />);
    await user.click(await screen.findByRole("button", { name: "Try an example" }));
    expect(screen.getByRole("textbox", { name: "Prompt" })).toHaveValue("Reply with one short sentence.");
  });

  it("says what is missing instead of sending an empty prompt, and mints no key", async () => {
    const user = userEvent.setup();
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Writing model"),
    );
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Enter a prompt before running chat.")).toBeVisible();
    expect(account.tryKey).not.toHaveBeenCalled();
  });
});

// A row that both chats and makes embeddings. It sits first in the catalog.
const dualRow = {
  id: "dual-embed",
  name: "Dual embedding model",
  display_name: "Dual embedding model",
  capability: "chat",
  alias: false,
  use_names: ["dual-embed"],
  locality: "local",
  capabilities: [
    { intent: "chat.completions", capability: "chat", input_modalities: ["text"], output_modalities: ["text"] },
    { intent: "text.embeddings", capability: "embed", input_modalities: ["text"], output_modalities: ["embedding"] },
  ],
};

describe("Chat takes a row that also makes embeddings, and never a row that only makes embeddings", () => {
  it("takes the chat option of a row that also makes embeddings, ahead of a vision model", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        json(
          url.includes("workflows")
            ? { workflows: [] }
            : { aliases: [], models: [dualRow, chatRow("vision-b", "Vision model B", ["text", "image"])] },
        ),
      ),
    );
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Dual embedding model"),
    );
    expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("Vision model B");
  });

  it("starts on the only chat row when that row also makes embeddings", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        json(url.includes("workflows") ? { workflows: [] } : { aliases: [], models: [dualRow] }),
      ),
    );
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Dual embedding model"),
    );
    expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("Choose a model");
  });

  it("never starts on a row that only makes embeddings, and so offers no chat model", async () => {
    const embedderRow = {
      id: "embedder",
      name: "Embedding model",
      display_name: "Embedding model",
      capability: "embed",
      alias: false,
      use_names: ["embedder"],
      locality: "local",
      capabilities: [
        { intent: "text.embeddings", capability: "embed", input_modalities: ["text"], output_modalities: ["embedding"] },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        json(url.includes("workflows") ? { workflows: [] } : { aliases: [], models: [embedderRow] }),
      ),
    );
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("No options available"),
    );
    expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("Embedding model");
  });

  it("never starts on a chat option whose own capability lists an embedding output", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        json(
          url.includes("workflows")
            ? { workflows: [] }
            : {
                aliases: [],
                models: [
                  bareChat("odd", "Odd output model", {
                    capabilities: [
                      { intent: "chat.completions", capability: "chat", input_modalities: ["text"], output_modalities: ["embedding"] },
                    ],
                  }),
                ],
              },
        ),
      ),
    );
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Choose a model"),
    );
    expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("Odd output model");
  });
});

// A chat row whose capability fields are unknown: it names no modalities, or no capabilities at all.
const bareChat = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  display_name: name,
  capability: "chat",
  alias: false,
  use_names: [id],
  locality: "local",
  ...extra,
});

describe("a chat model with unknown capability fields is an ordinary chat choice", () => {
  it("preselects the only chat model when its row names no input or output modalities", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        json(
          url.includes("workflows")
            ? { workflows: [] }
            : {
                aliases: [],
                models: [bareChat("bare", "Bare chat model", { capabilities: [{ intent: "chat.completions", capability: "chat" }] })],
              },
        ),
      ),
    );
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Bare chat model"),
    );
  });

  it("preselects the only chat model when its row has only the chat kind and no capability summaries", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        json(url.includes("workflows") ? { workflows: [] } : { aliases: [], models: [bareChat("bare", "Bare chat model")] }),
      ),
    );
    render(<TryPage />);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Bare chat model"),
    );
  });
});
