import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import * as ts from "typescript";
import { expect, it } from "vitest";

const sourceRoot = join(process.cwd(), "src");
const selectionProperty = /^(?:model|voice|default_voice|upstream_model|modelId|voiceId|selectedModel|selectedVoice|defaultModel|defaultVoice|fallbackModel)$/;
const selectionVariable = /^(?:model|voice|modelId|voiceId|selectedModel|selectedVoice|defaultModel|defaultVoice|fallbackModel)$/;

// These are capability names or UI labels, not selected model or voice IDs.
const capabilityMaps: Record<string, string[]> = {
  "app/_home/presentation.ts": ["names"],
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

const FILTER_SENTINELS = new Set(["all"]);

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

function selectionComparison(target: ts.PropertyAccessExpression): boolean {
  const owner = target.expression.getText();
  return (target.name.text === "id" && /(?:alias|model|voice)$/i.test(owner)) ||
    /^(?:use_name|model|voice|default_voice)$/.test(target.name.text);
}

function hardcodedSelections(source: string, file: string): string[] {
  const parsed = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  // Bind only this file: no imports, type inference or interprocedural provenance.
  // Symbols keep shadowed locals separate; assignments are conservatively
  // unioned, not control-flow evaluated. Component tests remain the backstop.
  const host = ts.createCompilerHost({ noLib: true, noResolve: true });
  host.getSourceFile = (name) => name === file ? parsed : undefined;
  const checker = ts.createProgram([file], { noLib: true, noResolve: true }, host)
    .getTypeChecker();
  const assignments = new Map<ts.Symbol, ts.Expression[]>();
  const collect = (node: ts.Node) => {
    if (ts.isBinaryExpression(node) &&
        node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
        ts.isIdentifier(node.left)) {
      const symbol = checker.getSymbolAtLocation(node.left);
      if (symbol) assignments.set(symbol, [...(assignments.get(symbol) ?? []), node.right]);
    }
    ts.forEachChild(node, collect);
  };
  collect(parsed);
  const values = (node: ts.Node | undefined, seen = new Set<ts.Symbol>()): string[] => {
    if (!node) return [];
    const direct = literal(node);
    if (direct !== null) return direct ? [direct] : [];
    if (ts.isJsxExpression(node)) return values(node.expression, seen);
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) ||
        ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node))
      return values(node.expression, seen);
    if (!ts.isIdentifier(node)) return [];
    const symbol = ts.isShorthandPropertyAssignment(node.parent)
      ? checker.getShorthandAssignmentValueSymbol(node.parent)
      : checker.getSymbolAtLocation(node);
    if (!symbol || seen.has(symbol)) return [];
    const next = new Set(seen).add(symbol);
    const initializers: (ts.Node | undefined)[] = [...(assignments.get(symbol) ?? [])];
    for (const declaration of symbol.declarations ?? []) {
      if (ts.isVariableDeclaration(declaration)) initializers.push(declaration.initializer);
      if (ts.isBindingElement(declaration) && ts.isArrayBindingPattern(declaration.parent) &&
          declaration.parent.elements[0] === declaration) {
        const variable = declaration.parent.parent;
        const call = ts.isVariableDeclaration(variable) ? variable.initializer : undefined;
        if (call && ts.isCallExpression(call) &&
            /^(?:React\.)?useState$/.test(call.expression.getText(parsed)))
          initializers.push(call.arguments[0]);
      }
    }
    return [...new Set(initializers.flatMap((value) => values(value, next)))];
  };
  const findings: string[] = [];
  const report = (node: ts.Node, value: string) => {
    // Filter sentinels ("all" models) select no model or voice identity.
    if (FILTER_SENTINELS.has(value.replace(/^\w+=/, "").replace(/^"|"$/g, ""))) return;
    const line = parsed.getLineAndCharacterOfPosition(node.getStart(parsed)).line + 1;
    findings.push(`${file}:${line}: ${value}`);
  };
  const visit = (node: ts.Node) => {
    if (ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node)) {
      const name = ts.isIdentifier(node.name) || ts.isStringLiteral(node.name)
        ? node.name.text : "";
      const selected = ts.isShorthandPropertyAssignment(node)
        ? node.name : node.initializer;
      const candidates = values(selected);
      const capabilityMap = name === "voice" &&
        capabilityMaps[file]?.includes(variableName(node) ?? "");
      if (selectionProperty.test(name) && !capabilityMap)
        for (const value of candidates) report(node, `${name}=${JSON.stringify(value)}`);
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
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      const target = node.left;
      const name = ts.isPropertyAccessExpression(target) ? target.name.text
        : ts.isElementAccessExpression(target) ? literal(target.argumentExpression)
        : ts.isIdentifier(target) ? target.text : null;
      if (name && selectionProperty.test(name))
        for (const value of values(node.right)) report(node, `${name}=${JSON.stringify(value)}`);
    }
    // A branch on an alias, model or voice name gives that name product meaning.
    if (ts.isBinaryExpression(node) && [
      ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken,
      ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken,
    ].includes(node.operatorToken.kind)) {
      for (const [side, other] of [[node.left, node.right], [node.right, node.left]]) {
        const value = literal(other);
        if (value && !FILTER_SENTINELS.has(value) &&
            ts.isPropertyAccessExpression(side) && selectionComparison(side))
          report(node, `${side.getText(parsed)}===${JSON.stringify(value)}`);
      }
    }
    if (ts.isJsxAttribute(node) && jsxSelection(node)) {
      for (const value of values(node.initializer))
        report(node, `${node.name.getText()}=${JSON.stringify(value)}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(parsed);
  return findings;
}

// Parses every source file, so its time grows with the tree: 2.8-4.8 s alone,
// and past the 5 s default beside the release gate's web build.
it("checks known model/voice sinks with a limited local syntactic guard", () => {
  const findings = sourceFiles(sourceRoot).flatMap((path) => {
    const file = relative(sourceRoot, path);
    return hardcodedSelections(readFileSync(path, "utf8"), file);
  });
  expect(findings).toEqual([]);
}, 15000);

it("catches new model and voice literals without an ID denylist", () => {
  const findings = hardcodedSelections(`
    const defaults = { model: "new-model", voice: "new-voice", upstream_model: "engine-id" };
    const [selectedVoice] = useState("another-voice");
    const picker = <VoicePicker defaultValue="third-voice" />;
    const other = <ModelPicker value={"fourth-model"} />;
  `, "mutation.tsx");
  expect(findings).toHaveLength(6);
});

it.each([
  'const chosen = chat.find((alias) => alias.id === "default");',
  'if ("tts" !== row.use_name) skip();',
  'const local = selectedModel.id == "fast";',
])("rejects a branch on a literal alias, model or voice name: %s", (source) => {
  expect(hardcodedSelections(source, "mutation.tsx")).toHaveLength(1);
});

it.each([
  'const choice = "new-engine"; const request = { model: choice };',
  'const [choice] = useState("new-engine"); const picker = <ModelPicker value={choice} />;',
  'const [choice] = React.useState("new-engine"); const picker = <VoicePicker value={choice} />;',
  'const choice = "new-engine"; const alias = choice; const request = { voice: alias };',
  'const choice = "new-engine"; const model = choice; const request = { model };',
  'const request = {}; request.model = "new-engine";',
  'const choice = "new-engine"; request["voice"] = choice;',
  'let choice; choice = "new-engine"; const request = { model: choice };',
])("rejects a renamed or assigned literal: %s", (source) => {
  expect(hardcodedSelections(source, "mutation.tsx").join("\n")).toContain("new-engine");
});

it("does not follow shadowed bindings or invent values for cycles or discovery", () => {
  expect(hardcodedSelections(`
    const choice = "unrelated-label";
    function picker(choice: string) { return <ModelPicker value={choice} />; }
    function other() { const choice = catalog.id; return { model: choice }; }
    const a = b; const b = a; const request = { voice: a };
  `, "mutation.tsx")).toEqual([]);
});
