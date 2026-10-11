import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { chatAlias, clients, recipeFor } from "@/app/home/recipes";

describe("Home client recipes", () => {
  it("uses the advertised model for every client without copying a secret", () => {
    expect(clients).toHaveLength(10);
    for (const client of clients) {
      const recipe = recipeFor(
        client,
        "https://gateway.test/v1",
        "https://gateway.test",
        "advertised-alias",
      );
      expect(recipe.code).toContain("advertised-alias");
      expect(recipe.docs).toMatch(/^https:\/\//);
      expect(recipe.code).not.toMatch(/opk_|oak_/);
    }
  });

  it("uses the gateway's recommended chat alias for recipes", () => {
    const chat = { operation: "chat", available: true, status: "ready" as const, reason_code: "ready" };
    expect(chatAlias([])).toBeNull();
    expect(
      chatAlias([{ ...chat, id: "utility-check" }, { ...chat, id: "house-chat" }], "house-chat"),
    ).toBe("house-chat");
    // No name is special: "default" is an ordinary alias.
    expect(chatAlias([{ ...chat, id: "utility-check" }, { ...chat, id: "default" }])).toBe("utility-check");
  });
});

describe.skipIf(process.platform === "win32")("Shell recipe escaping", () => {
  const model = "'$(printf unsafe)`printf unsafe`";
  const url = "https://gateway.test/'$(printf unsafe)`printf unsafe`";

  it("passes curl URL and JSON as literal arguments", () => {
    const recipe = recipeFor("curl", url, url, model);
    const args = execFileSync(
      "/bin/sh",
      ["-c", 'curl() { printf "%s\\n" "$@"; };\n' + recipe.code],
      {
        env: { NODE_ENV: "test", GATEWAY_API_KEY: "synthetic-key" },
        encoding: "utf8",
      },
    )
      .trim()
      .split("\n");
    expect(args[0]).toBe(`${url}/chat/completions`);
    expect(JSON.parse(args.at(-1)!).model).toBe(model);
    expect(args).toContain("Authorization: Bearer synthetic-key");
  });

  it("sets Claude Code URL and model without command substitution", () => {
    const recipe = recipeFor("Claude Code", url, url, model);
    const values = execFileSync(
      "/bin/sh",
      [
        "-c",
        'claude() { printf "%s\\n" "$ANTHROPIC_BASE_URL" "$ANTHROPIC_MODEL" "$ANTHROPIC_DEFAULT_HAIKU_MODEL"; };\n' +
          recipe.code,
      ],
      {
        env: { NODE_ENV: "test", GATEWAY_API_KEY: "synthetic-key" },
        encoding: "utf8",
      },
    )
      .trim()
      .split("\n");
    expect(values).toEqual([url, model, model]);
  });
});

describe("Home local recipe fallback", () => {
  const alias = { operation: "chat", available: true, status: "ready" as const, reason_code: "ready" };
  it("prefers a permitted local chat alias when nothing is recommended", () => {
    expect(chatAlias([{ ...alias, id: "paid", local: false }, { ...alias, id: "local-chat", local: true }])).toBe("local-chat");
  });
  it("uses an allowed paid or unclassified alias when no local one is allowed", () => {
    expect(chatAlias([{ ...alias, id: "paid", local: false }, { ...alias, id: "unknown" }])).toBe("paid");
  });
  it("skips unavailable aliases and ignores non-chat aliases", () => {
    expect(chatAlias([{ ...alias, id: "off", local: true, available: false }, { ...alias, id: "on" }])).toBe("on");
    expect(chatAlias([{ ...alias, id: "images", operation: "images", local: true }])).toBeNull();
  });
});
