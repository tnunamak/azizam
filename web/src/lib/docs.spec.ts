import { describe, expect, it } from "vitest";
import { clientDocsUrl, DOCS_BASE, MCP_DOCS_URL } from "./docs";
import { appGuides } from "./app-guides";

describe("public docs links", () => {
  it("points at the public guides by default", () => {
    expect(DOCS_BASE).toBe("https://azizam.dev/docs");
    expect(MCP_DOCS_URL).toBe("https://azizam.dev/docs/api/mcp/");
  });

  it("gives each client its own page under connect, with the id from the app data", () => {
    expect(clientDocsUrl("open-webui")).toBe(
      "https://azizam.dev/docs/connect/open-webui/",
    );
    for (const app of appGuides.apps) {
      expect(clientDocsUrl(app.id)).toMatch(
        new RegExp(`^https://azizam\\.dev/docs/connect/${app.id}/$`),
      );
    }
  });
});
