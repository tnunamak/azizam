import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { metadata } from "./layout";
import { siteMetadata } from "@/lib/site-metadata";
import registry from "../../../gateway/ui_pages.json";

const robotsFile = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../public/robots.txt",
);

describe("robots.txt (web/public/robots.txt)", () => {
  it("asks every crawler to stay away from everything, in the plain two-line form", () => {
    expect(readFileSync(robotsFile, "utf8")).toBe("User-agent: *\nDisallow: /\n");
  });

  it("is a root file the gateway serves to anyone", () => {
    expect(registry.files).toContain("robots.txt");
  });

  it("tells search engines in the page itself, with and without a public address", () => {
    expect(metadata.robots).toEqual({ index: false, follow: false });
    expect(siteMetadata(undefined).robots).toEqual({ index: false, follow: false });
    expect(siteMetadata(new URL("https://ai.example")).robots).toEqual({ index: false, follow: false });
  });
});
