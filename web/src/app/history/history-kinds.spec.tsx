import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RequestContent } from "@/lib/request-content";
import { RequestBody } from "./history-kinds";

const mocks = vi.hoisted(() => ({
  libraryItemForRequest: vi.fn(),
}));

vi.mock("@/lib/account-api", () => ({
  accountApi: { libraryItemForRequest: mocks.libraryItemForRequest },
}));

function renderBody(content: RequestContent, requestId: string | null = "req_a") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RequestBody content={content} requestId={requestId} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.libraryItemForRequest.mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("a conversation", () => {
  const conversation: RequestContent = {
    shape: "conversation",
    turns: [
      { role: "user", text: "What is the capital of France?", lines: [] },
      { role: "assistant", text: "Let me check.", lines: ["Tool call: lookup_capital"] },
      { role: "tool", text: "", lines: ["Tool result"] },
    ],
    reply: { kept: true, text: "Paris.", lines: [], failure: null },
  };

  it("shows the person's words on the right, the model's on the left, and the reply last", () => {
    renderBody(conversation);
    const list = screen.getByRole("list", { name: "Conversation" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(within(items[0]).getByText("You:", { exact: false })).toBeInTheDocument();
    expect(within(items[0]).getByText("What is the capital of France?")).toBeVisible();
    expect(within(items[1]).getByText("Let me check.")).toBeVisible();
    expect(within(items[3]).getByText("Paris.")).toBeVisible();
  });

  it("names a tool call and a tool result as short labelled lines", () => {
    renderBody(conversation);
    expect(screen.getByText("Tool call: lookup_capital")).toBeVisible();
    expect(screen.getByText("Tool result")).toBeVisible();
  });

  it("says the reply was not kept, and shows no raw JSON", () => {
    const { container } = renderBody({
      shape: "conversation",
      turns: [{ role: "user", text: "Hi", lines: [] }],
      reply: { kept: false, text: "", lines: [], failure: null },
    });
    expect(screen.getByText("The reply wasn’t kept.")).toBeVisible();
    expect(container.textContent).not.toMatch(/[{}]/);
  });

  it("shows the failure the gateway gave above the reply", () => {
    renderBody({
      shape: "conversation",
      turns: [{ role: "user", text: "Hi", lines: [] }],
      reply: { kept: true, text: "", lines: [], failure: "The engine stopped." },
    });
    expect(screen.getByText("The engine stopped.")).toBeVisible();
  });
});

describe("a picture, sound or video", () => {
  const picture: RequestContent = {
    shape: "media",
    prompt: "a lighthouse at dusk",
    facts: [
      ["Prompt", "a lighthouse at dusk"],
      ["Size", "1024x1024"],
    ],
    failure: null,
  };

  it("shows the prompt and the main settings as a label and value list", () => {
    renderBody(picture);
    expect(screen.getByText("Prompt")).toBeVisible();
    expect(screen.getByText("a lighthouse at dusk")).toBeVisible();
    expect(screen.getByText("Size")).toBeVisible();
    expect(screen.getByText("1024x1024")).toBeVisible();
  });

  it("links to the Library item the request made, when there is one", async () => {
    mocks.libraryItemForRequest.mockResolvedValue("asset-1");
    renderBody(picture);
    const link = await screen.findByRole("link", { name: "Open in Library" });
    expect(link).toHaveAttribute("href", "/library?asset=asset-1");
    expect(mocks.libraryItemForRequest).toHaveBeenCalledWith("req_a", "a lighthouse at dusk");
  });

  it("offers no link when no Library item was made by the request", async () => {
    renderBody(picture);
    await waitFor(() => expect(mocks.libraryItemForRequest).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: "Open in Library" })).not.toBeInTheDocument();
  });

  it("offers no link when the request has no receipt to match", () => {
    renderBody(picture, null);
    expect(mocks.libraryItemForRequest).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Open in Library" })).not.toBeInTheDocument();
  });

  it("says the request failed, in the list", () => {
    renderBody({ ...picture, failure: "Failed (500)" });
    expect(screen.getByText("Result")).toBeVisible();
    expect(screen.getByText("Failed (500)")).toBeVisible();
  });
});

describe("a transcription", () => {
  it("shows the file's type and size, then the transcript", () => {
    renderBody({
      shape: "transcription",
      file: [["File", "audio/mpeg · 2.1 MB"]],
      transcript: "Hello from the recording.",
      failure: null,
    });
    expect(screen.getByText("audio/mpeg · 2.1 MB")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Transcript" })).toBeVisible();
    expect(screen.getByText("Hello from the recording.")).toBeVisible();
  });

  it("says no transcript was kept when there is none", () => {
    renderBody({ shape: "transcription", file: [["File", "Not kept"]], transcript: null, failure: null });
    expect(screen.getByText("No transcript was kept.")).toBeVisible();
  });
});

describe("embeddings", () => {
  it("shows the input text and how many numbers came back", () => {
    renderBody({ shape: "embeddings", inputs: ["first text", "second text"], numbers: 3072, failure: null });
    expect(screen.getByText("first text")).toBeVisible();
    expect(screen.getByText("second text")).toBeVisible();
    expect(screen.getByText("3,072 numbers returned")).toBeVisible();
  });

  it("says one number, in the singular", () => {
    renderBody({ shape: "embeddings", inputs: ["x"], numbers: 1, failure: null });
    expect(screen.getByText("1 number returned")).toBeVisible();
  });

  it("says the numbers were not kept when they are not", () => {
    renderBody({ shape: "embeddings", inputs: ["x"], numbers: null, failure: null });
    expect(screen.getByText("The numbers weren’t kept.")).toBeVisible();
  });
});

describe("a kind the screens do not know", () => {
  it("shows a plain label and value summary", () => {
    renderBody({ shape: "other", facts: [["Max tokens", "32"]], failure: null });
    expect(screen.getByText("Max tokens")).toBeVisible();
    expect(screen.getByText("32")).toBeVisible();
  });

  it("says nothing readable was kept, and points at Technical details, when there is nothing to show", () => {
    renderBody({ shape: "other", facts: [], failure: null });
    expect(screen.getByText(/Nothing readable was kept/)).toBeVisible();
  });
});
