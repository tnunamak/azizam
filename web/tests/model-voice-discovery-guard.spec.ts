import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import * as ts from "typescript";
import { expect, it } from "vitest";

const sourceRoot = join(process.cwd(), "src");
const selectionProperty = /^(?:model|voice|default_voice|upstream_model|modelId|voiceId|selectedModel|selectedVoice|defaultModel|defaultVoice|fallbackModel)$/;
const selectionVariable = /^(?:model|voice|modelId|voiceId|selectedModel|selectedVoice|defaultModel|defaultVoice|fallbackModel)$/;

// These are capability names or UI labels, not selected model or voice IDs.
const capabilityMaps: Record<string, string[]> = {
  "app/home/presentation.ts": ["names"],
  "app/people/people-api.ts": ["labels", "INVITEE_TASKS"],
  "app/try/page.tsx": ["CAPABILITY_TABS"],
  "app/usage/page.tsx": ["requestNouns"],
  "lib/use-capability-views.ts": ["LABELS"],
};

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory())
      return ["fixtures", "audit-tests", "__tests__"].includes(entry.name)
        ? []
        : sourceFiles(path);
    return /\.tsx?$/.test(entry.name) &&
      !/\.(?:spec|test|e2e|playwright)\.tsx?$/.test(entry.name)
      ? [path]
      : [];
  });
}

function literal(node: ts.Node | undefined): string | null {
  if (node && ts.isJsxExpression(node)) return literal(node.expression);
  return node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
    ? node.text
    : null;
}

function variableName(node: ts.Node): string | null {
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name))
      return parent.name.text;
  }
  return null;
}

function jsxSelection(node: ts.JsxAttribute): boolean {
  const name = node.name.getText();
  if (selectionProperty.test(name)) return true;
  if (name !== "value" && name !== "defaultValue") return false;
  const attributes = node.parent;
  const opening = attributes.parent;
  if (!ts.isJsxOpeningElement(opening) && !ts.isJsxSelfClosingElement(opening))
    return false;
  if (/model|voice/i.test(opening.tagName.getText())) return true;
  return attributes.properties.some((property) =>
    ts.isJsxAttribute(property) &&
    property.name.getText() === "aria-label" &&
    /^(?:model|voice)(?: id| name)?$/i.test(literal(property.initializer) ?? ""),
  );
}

function hardcodedSelections(source: string, file: string): string[] {
  const parsed = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const findings: string[] = [];
  const report = (node: ts.Node, value: string) => {
    const line = parsed.getLineAndCharacterOfPosition(node.getStart(parsed)).line + 1;
    findings.push(`${file}:${line}: ${value}`);
  };
  const visit = (node: ts.Node) => {
    if (ts.isPropertyAssignment(node)) {
      const name = ts.isIdentifier(node.name) || ts.isStringLiteral(node.name)
        ? node.name.text : "";
      const value = literal(node.initializer);
      const capabilityMap = name === "voice" &&
        capabilityMaps[file]?.includes(variableName(node) ?? "");
      if (selectionProperty.test(name) && value && !capabilityMap)
        report(node, `${name}=${JSON.stringify(value)}`);
    }
    if (ts.isVariableDeclaration(node)) {
      const names = ts.isIdentifier(node.name)
        ? [node.name.text]
        : ts.isArrayBindingPattern(node.name)
          ? node.name.elements.flatMap((part) =>
              ts.isBindingElement(part) && ts.isIdentifier(part.name)
                ? [part.name.text]
                : [])
          : [];
      const initializer = node.initializer;
      const value = initializer && ts.isCallExpression(initializer) &&
        /^(?:React\.)?useState$/.test(initializer.expression.getText(parsed))
        ? literal(initializer.arguments[0])
        : literal(initializer);
      if (names.some((name) => selectionVariable.test(name)) && value &&
          !(file === "app/library/page.tsx" && names.includes("model") && value === "all"))
        report(node, `${names.join(",")}=${JSON.stringify(value)}`);
    }
    if (ts.isJsxAttribute(node) && jsxSelection(node)) {
      const value = literal(node.initializer);
      if (value) report(node, `${node.name.getText()}=${JSON.stringify(value)}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(parsed);
  return findings;
}

it("keeps model and voice selection IDs out of web/src code", () => {
  const findings = sourceFiles(sourceRoot).flatMap((path) => {
    const file = relative(sourceRoot, path);
    return hardcodedSelections(readFileSync(path, "utf8"), file);
  });
  expect(findings).toEqual([]);
});

it("catches new model and voice literals without an ID denylist", () => {
  const findings = hardcodedSelections(`
    const defaults = { model: "new-model", voice: "new-voice", upstream_model: "engine-id" };
    const [selectedVoice] = useState("another-voice");
    const picker = <VoicePicker defaultValue="third-voice" />;
    const other = <ModelPicker value={"fourth-model"} />;
  `, "mutation.tsx");
  expect(findings).toHaveLength(6);
});
