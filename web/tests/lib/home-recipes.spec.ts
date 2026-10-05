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

  it("uses only the recommended default chat alias for recipes", () => {
    expect(chatAlias([])).toBeNull();
    expect(
      chatAlias([
        {
          id: "utility-check",
          operation: "chat",
          available: true,
          status: "ready",
          reason: "Utility check is ready.",
        },
        {
          id: "default",
          operation: "chat",
          available: true,
          status: "ready",
          reason: "Recommended chat is ready.",
        },
      ]),
    ).toBe("default");
    expect(
      chatAlias([
        {
          id: "utility-check",
          operation: "chat",
          available: true,
          status: "ready",
          reason: "Utility check is ready.",
        },
      ]),
    ).toBeNull();
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
