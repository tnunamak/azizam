import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import * as ts from "typescript";
import { expect, it } from "vitest";

// axe aria-prohibited-attr: a div, span or p with no role cannot take an
// aria-label. Screen readers drop the label, and an empty labelled loader
// fails axe outright (the setup page did, in the r113 gate).
const sourceRoot = join(process.cwd(), "src");
const genericTags = new Set(["div", "span", "p"]);

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return entry.name.endsWith(".tsx") && !/\.(?:spec|test)\.tsx$/.test(entry.name) ? [path] : [];
  });
}

function labelledWithoutRole(path: string): string[] {
  const text = readFileSync(path, "utf8");
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found: string[] = [];
  const visit = (node: ts.Node) => {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && genericTags.has(node.tagName.getText(source))) {
      const attributes = node.attributes.properties;
      const names = attributes.flatMap((attribute) => (ts.isJsxAttribute(attribute) ? [attribute.name.getText(source)] : []));
      const spreads = attributes.some((attribute) => ts.isJsxSpreadAttribute(attribute));
      if (names.includes("aria-label") && !names.includes("role") && !spreads) {
        const { line } = source.getLineAndCharacterOfPosition(node.getStart(source));
        found.push(`${relative(sourceRoot, path)}:${line + 1}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

it("gives every labelled div, span and p a role", () => {
  expect(sourceFiles(sourceRoot).flatMap(labelledWithoutRole)).toEqual([]);
});
