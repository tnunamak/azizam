import { describe, expect, it } from "vitest";
import { contentOf, hasRemovedDetails } from "./request-content";
import {
  hasRemovedPart,
  requestKind,
  sizeWords,
  withoutRemovedParts,
  type RequestHistoryRecord,
} from "./request-history";

function record(
  route: string,
  parts: RequestHistoryRecord["parts"],
  status: number | null = 200,
  extra: Partial<RequestHistoryRecord> = {},
): RequestHistoryRecord {
  return {
    record_id: "kr_a",
    kind: "request_history",
    created_at: 1_800_000_000,
    kept_until: null,
    origin_kind: "app_key",
    request_id: "req_a",
    parts,
    omissions: [],
    summary: { route, model: "chat-a", status, latency_ms: 40 },
    ...extra,
  };
}

describe("the kind of a route", () => {
  it.each([
    ["/openai/v1/chat/completions?stream=false", "conversation", "Chat"],
    ["/v1/responses", "conversation", "Chat"],
    ["/anthropic/v1/messages", "conversation", "Chat"],
    ["/v1/embeddings", "embeddings", "Embeddings"],
    ["/v1/audio/transcriptions", "transcription", "Transcription"],
    ["/v1/images/generations", "media", "Picture"],
    ["/v1/images/edits", "media", "Picture"],
    ["/v1/videos", "media", "Video"],
    ["/v1/audio/speech", "media", "Audio"],
    ["/v1/something/new", "other", "Request"],
  ])("%s is a %s request, labelled %s", (route, shape, label) => {
    expect(requestKind(route)).toEqual({ shape, label });
  });

  it("is unknown when there is no route", () => {
    expect(requestKind(null)).toEqual({ shape: "other", label: "Request" });
  });
});

describe("removed parts", () => {
  it("finds a removed part at any depth, and not in a value that merely looks similar", () => {
    expect(hasRemovedPart({ headers: {}, body: { messages: [{ content: "key [removed]" }] } })).toBe(true);
    expect(hasRemovedPart(["see [url removed]"])).toBe(true);
    expect(hasRemovedPart({ body: { content: "removed by the owner" } })).toBe(false);
  });

  it("takes the markers out of a sentence, and keeps its line breaks", () => {
    expect(withoutRemovedParts("Use my key [removed] to call [url removed] now")).toBe("Use my key to call now");
    expect(withoutRemovedParts("first [removed]\nsecond")).toBe("first\nsecond");
  });
});

describe("sizes in words", () => {
  it.each([
    [512, "512 bytes"],
    [2048, "2.0 KB"],
    [2_200_000, "2.1 MB"],
    [15 * 1024 * 1024, "15 MB"],
  ])("%i bytes reads %s", (bytes, words) => {
    expect(sizeWords(bytes)).toBe(words);
  });
});

describe("a conversation", () => {
  it("reads the person's and the model's messages as bubbles, without the system prompt", () => {
    const content = contentOf(
      record("/openai/v1/chat/completions", {
        accepted: {
          headers: { "content-type": "application/json" },
          body: {
            model: "chat-a",
            messages: [
              { role: "system", content: "You are terse." },
              { role: "user", content: "What is the capital of France?" },
            ],
          },
        },
        received: { status: 200, headers: {}, body: { choices: [{ message: { role: "assistant", content: "Paris." } }] } },
      }),
    );
    expect(content).toEqual({
      shape: "conversation",
      turns: [{ role: "user", text: "What is the capital of France?", lines: [] }],
      reply: { kept: true, text: "Paris.", lines: [], failure: null },
    });
  });

  it("names each tool a message called as a labelled line, and a tool's result as its own line", () => {
    const content = contentOf(
      record("/v1/chat/completions", {
        accepted: {
          headers: {},
          body: {
            messages: [
              { role: "user", content: "Weather in Oslo?" },
              { role: "assistant", content: null, tool_calls: [{ function: { name: "get_weather" } }] },
              { role: "tool", content: "12 C" },
            ],
          },
        },
        received: { status: 200, headers: {}, body: { choices: [{ message: { content: "It is 12 C." } }] } },
      }),
    );
    expect(content.shape === "conversation" && content.turns).toEqual([
      { role: "user", text: "Weather in Oslo?", lines: [] },
      { role: "assistant", text: "", lines: ["Tool call: get_weather"] },
      { role: "tool", text: "", lines: ["Tool result"] },
    ]);
  });

  it("reads a Responses request's input and reply, and an Anthropic message's text blocks", () => {
    const responses = contentOf(
      record("/v1/responses", {
        accepted: { headers: {}, body: { model: "m", input: [{ role: "user", content: [{ type: "input_text", text: "Plan my week" }] }] } },
        received: { status: 200, headers: {}, body: { output: [{ type: "message", content: [{ type: "output_text", text: "Monday: rest." }] }] } },
      }),
    );
    expect(responses).toMatchObject({
      turns: [{ role: "user", text: "Plan my week" }],
      reply: { kept: true, text: "Monday: rest." },
    });

    const messages = contentOf(
      record("/anthropic/v1/messages", {
        accepted: { headers: {}, body: { messages: [{ role: "user", content: [{ type: "text", text: "Summarise" }] }] } },
        received: { status: 200, headers: {}, body: { content: [{ type: "text", text: "Done." }] } },
      }),
    );
    expect(messages).toMatchObject({ turns: [{ text: "Summarise" }], reply: { text: "Done." } });
  });

  it("says the reply was not kept when the gateway did not keep it", () => {
    const content = contentOf(
      record("/v1/chat/completions", {
        accepted: { headers: {}, body: { messages: [{ role: "user", content: "Hi" }] } },
      }),
    );
    expect(content.shape === "conversation" && content.reply).toEqual({ kept: false, text: "", lines: [], failure: null });
  });

  it("gives the gateway's own failure message, or the status, when the reply failed", () => {
    const withMessage = contentOf(
      record("/v1/chat/completions", {
        accepted: { headers: {}, body: { messages: [{ role: "user", content: "Hi" }] } },
        received: { status: 500, headers: {}, body: { error: { message: "The engine stopped." } } },
      }, 500),
    );
    expect(withMessage.shape === "conversation" && withMessage.reply.failure).toBe("The engine stopped.");

    const withoutMessage = contentOf(
      record("/v1/chat/completions", {
        accepted: { headers: {}, body: { messages: [{ role: "user", content: "Hi" }] } },
        received: { status: 502, headers: {}, body: { omitted: "unsupported_container" } },
      }, 502),
    );
    expect(withoutMessage.shape === "conversation" && withoutMessage.reply.failure).toBe("Failed (502)");
  });

  it("keeps removed markers out of the words it reads", () => {
    const content = contentOf(
      record("/v1/chat/completions", {
        accepted: { headers: {}, body: { messages: [{ role: "user", content: "Use key [removed] to ask" }] } },
      }),
    );
    expect(content.shape === "conversation" && content.turns[0].text).toBe("Use key to ask");
  });
});

describe("a picture, sound or video", () => {
  it("reads the prompt and the main settings, and leaves out the rest", () => {
    const content = contentOf(
      record("/v1/images/generations", {
        accepted: {
          headers: { "content-type": "application/json" },
          body: { model: "img", prompt: "a lighthouse at dusk", size: "1024x1024", quality: "high", n: 1, user_id: "nope", nested: { a: 1 } },
        },
        received: { status: 200, headers: {}, body: { data: [{ b64_json: "AAAA" }] } },
      }),
    );
    expect(content).toEqual({
      shape: "media",
      prompt: "a lighthouse at dusk",
      facts: [
        ["Prompt", "a lighthouse at dusk"],
        ["Size", "1024x1024"],
        ["Quality", "high"],
        ["Count", "1"],
      ],
      failure: null,
    });
  });

  it("reads an image edit's prompt from its form", () => {
    const content = contentOf(
      record("/v1/images/edits", {
        accepted: { headers: {}, body: { fields: { model: "edit", prompt: "make the sky green" }, files: [{ field: "image", media_type: "image/png", bytes: 512 }] } },
      }),
    );
    expect(content).toMatchObject({ shape: "media", prompt: "make the sky green", facts: [["Prompt", "make the sky green"]] });
  });

  it("gives the failure for a request that failed", () => {
    const content = contentOf(
      record("/v1/videos", { accepted: { headers: {}, body: { prompt: "a fox" } } }, 500),
    );
    expect(content).toMatchObject({ shape: "media", failure: "Failed (500)" });
  });
});

describe("a transcription", () => {
  it("reads the file's type and size, then the transcript", () => {
    const content = contentOf(
      record("/v1/audio/transcriptions", {
        accepted: { headers: {}, body: { fields: { model: "whisper-1" }, files: [{ field: "file", media_type: "audio/mpeg", bytes: 2_200_000 }] } },
        received: { status: 200, headers: {}, body: { text: "Hello from the recording." } },
      }),
    );
    expect(content).toEqual({
      shape: "transcription",
      file: [["File", "audio/mpeg · 2.1 MB"]],
      transcript: "Hello from the recording.",
      failure: null,
    });
  });

  it("says there is no transcript when the reply was not kept", () => {
    const content = contentOf(
      record("/v1/audio/transcriptions", {
        accepted: { headers: {}, body: { fields: { model: "whisper-1" }, files: [] } },
      }),
    );
    expect(content).toMatchObject({ file: [["File", "Not kept"]], transcript: null });
  });
});

describe("embeddings", () => {
  it("reads the input and counts the numbers that came back", () => {
    const content = contentOf(
      record("/v1/embeddings", {
        accepted: { headers: {}, body: { model: "emb", input: ["first text", "second text"] } },
        received: { status: 200, headers: {}, body: { data: [{ embedding: [0.1, 0.2, 0.3] }, { embedding: [0.4, 0.5, 0.6] }] } },
      }),
    );
    expect(content).toEqual({ shape: "embeddings", inputs: ["first text", "second text"], numbers: 6, failure: null });
  });

  it("has no count when the numbers were not kept", () => {
    const content = contentOf(
      record("/v1/embeddings", {
        accepted: { headers: {}, body: { input: "one text" } },
        received: { status: 200, headers: {}, body: { omitted: "record_size", bytes: 2_000_000 } },
      }),
    );
    expect(content).toMatchObject({ inputs: ["one text"], numbers: null });
  });
});

describe("a kind the screens do not know", () => {
  it("reads the scalar fields as facts, with headers and omissions left out", () => {
    const content = contentOf(
      record("/v1/something/new", {
        accepted: { headers: { "content-type": "application/json" }, body: { model: "x", max_tokens: 32, stream: false, options: { deep: true } } },
      }),
    );
    expect(content).toEqual({
      shape: "other",
      facts: [
        ["Model", "x"],
        ["Max tokens", "32"],
        ["Stream", "false"],
      ],
      failure: null,
    });
  });

  it("says what was left out when the body was too large to keep", () => {
    const content = contentOf(
      record("/v1/something/new", { accepted: { headers: {}, body: { omitted: "record_size", bytes: 3_000_000 } } }),
    );
    expect(content).toEqual({
      shape: "other",
      facts: [["Body", "Not kept, because it was too large or couldn’t be read"]],
      failure: null,
    });
  });
});

describe("the quiet line for removed details", () => {
  it("is on when a kept value was removed, at any depth", () => {
    expect(
      hasRemovedDetails(record("/v1/chat/completions", { accepted: { headers: {}, body: { api_key: "[removed]", messages: [] } } })),
    ).toBe(true);
  });

  it("is off when nothing was removed", () => {
    expect(hasRemovedDetails(record("/v1/chat/completions", { accepted: { headers: {}, body: { messages: [] } } }))).toBe(false);
  });
});
