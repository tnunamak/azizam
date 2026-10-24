import { describe, expect, it } from "vitest";
import { chatDefaultChoice, type ChatCandidate } from "@/app/try/chat-default";

const chat = (id: string, order: number, extra: Partial<ChatCandidate> = {}): ChatCandidate => ({
  id,
  order,
  pictures: false,
  embedding: false,
  ...extra,
});

describe("the default chat model is an ordinary chat model, read from the catalog", () => {
  it("takes the catalog's recommendation when it is ordinary, even after a vision model", () => {
    const choice = chatDefaultChoice([
      chat("vision-first", 0, { pictures: true }),
      chat("ordinary", 1),
      chat("recommended", 2, { recommended: true }),
    ]);
    expect(choice?.id).toBe("recommended");
  });

  it("takes the first ordinary chat model in catalog order when nothing is recommended", () => {
    const choice = chatDefaultChoice([
      chat("second", 2),
      chat("first", 1),
      chat("third", 3),
    ]);
    expect(choice?.id).toBe("first");
  });

  it("never takes a vision model, even when it is first and recommended", () => {
    const choice = chatDefaultChoice([
      chat("vision", 0, { pictures: true, recommended: true }),
      chat("ordinary", 1),
    ]);
    expect(choice?.id).toBe("ordinary");
  });

  it("never takes an embedding model", () => {
    const choice = chatDefaultChoice([
      chat("embedding", 0, { embedding: true }),
      chat("ordinary", 1),
    ]);
    expect(choice?.id).toBe("ordinary");
  });

  it("still gives a choice when every chat model takes pictures: the first in catalog order", () => {
    const choice = chatDefaultChoice([
      chat("vision-b", 2, { pictures: true }),
      chat("vision-a", 1, { pictures: true }),
    ]);
    expect(choice?.id).toBe("vision-a");
  });

  it("gives nothing for an empty list", () => {
    expect(chatDefaultChoice([])).toBeUndefined();
  });

  it("never takes an embedding row that sits first, when the only other chat model takes pictures", () => {
    // The catalog's exact case: a dual-capability row that makes embeddings is
    // first in catalog order, and the second row is a vision model.
    const choice = chatDefaultChoice([
      chat("embedding-chat", 0, { embedding: true }),
      chat("vision", 1, { pictures: true }),
    ]);
    expect(choice?.id).toBe("vision");
  });

  it("gives nothing when every row makes embeddings, rather than taking the first one", () => {
    const choice = chatDefaultChoice([
      chat("embedder", 0, { embedding: true }),
      chat("embedder-b", 1, { embedding: true }),
    ]);
    expect(choice).toBeUndefined();
  });
});
