import fs from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const sourceRoot = path.resolve(process.cwd(), "src");

// These are presentation synonyms from empathy/GLOSSARY.md. Only copy-bearing
// AST contexts are inspected; protocol keys such as `profile_id` stay outside
// the detector.
const banned: RegExp[] = [
  /\bbackend\b/i,
  /\bupstream\b/i,
  /\bclient identity\b/i,
  /\bprincipal\b/i,
  /\bcredential\b/i,
  /\bmint\b/i,
  /\bpolicy bundle\b/i,
  /^profile$/i,
  /^role$/i,
  /^users?$/i,
  /\bmodality\b/i,
  /\bintent\b/i,
  /\boperation\b/i,
  /\btext generation\b/i,
  /^image$/i,
  /^videos$/i,
  /\btts\b/i,
  /\bstt\b/i,
  /\bsfx\b/i,
  /^embed$/i,
  /^rerank$/i,
  /\bmodel id\b/i,
  /\buse name\b/i,
  /\bbacking model\b/i,
  /\bmanifest\b/i,
  /\bgraph\b/i,
  /^call$/i,
  /^event$/i,
  /\binvoice\b/i,
  /\bbill\b/i,
  /\btrace\b/i,
  /\baudit log\b/i,
  /^cost$/i,
  /^free$/i,
  /\bbase url\b/i,
  /\btest connection\b/i,
  /\btest gateway\b/i,
  /^raw$/i,
  /^json$/i,
  /\bdebug\b/i,
  // Raw capability keys. "Voice" is the glossary name (VOICE.md), so only
  // the lowercase key is banned for it.
  /^(?:voice|transcribe|decide|embed|rerank)$/,
  /^(?:Transcribe|Decide|Embed|Rerank)$/,
  /^(?:voice|rerank) request$/i,
];

// Current page-lane copy is grandfathered by exact file and exact literal.
// Existing exceptions stop applying as soon as a lane changes its copy. They
// do not fail merely because that copy has been removed.
const legacyCopy: Record<string, string[]> = {
  "app/activity/page.tsx": [
    "End of audit log",
    "End of audit log",
    "End of audit log",
  ],
  "app/library/page.tsx": [
    "Not recorded. The gateway does not report whether the prompt was omitted by policy or unavailable from the backend.",
    "Not recorded. The gateway does not report whether the prompt was omitted by policy or unavailable from the backend.",
  ],
  "app/design/page.tsx": ["Cost", "Image", "Cost"],
  "app/engines/page.tsx": ["Role"],
  "app/apps/page.tsx": [
    "Base URL",
    ", paste this base URL, and enter your saved key. The examples\n                below read the key from GATEWAY_API_KEY.",
  ],
  "app/home/page.tsx": [
    "Base URL",
    "Choose another key, or ask the gateway owner to check this\n                      key’s access plan and chat aliases. Models without an\n                      alias may still be available. Your base URL stays the\n                      same.",
    "Voice",
    "Transcribe",
    "Decide",
    "Embed",
    "Rerank",
  ],
  "app/models/models-client.tsx": [
    "Stable aliases stay the same when the backing model changes.\n                This catalog is scoped to models your access plan allows.",
    "Cost",
    "Cost",
  ],
  "app/providers/page.tsx": [
    "Profile",
    "Provider base URL",
    "Provider base URL",
  ],
  "app/try/page.tsx": ["Cost", "Image", "Decide", "Embed"],
  "app/models/models-client.spec.tsx": ["Rerank"],
  "app/workflows/page.tsx": [
    "No manifest notes provided.",
    "No manifest notes provided.",
  ],
  "app/usage/page.tsx": ["Cost", "Cost"],
  "app/settings/page.tsx": ["Public base URL"],
  "components/admin/add-service-dialog.tsx": [
    "Register an upstream engine. Select one API-supplied preset before\n            entering its connection details. Changes apply live — no restart\n            needed.",
    "Base URL",
    "Test connection",
  ],
};

// Contextual exceptions are separate from the migration baseline. Voice is a
// speaker selector on Try, while Transcribe is the verb on its action tab.
const contextualExceptions: Record<
  string,
  { value: string; reason: string }[]
> = {
  "app/try/page.tsx": [
    { value: "Voice", reason: "Selected speaker control label" },
    { value: "Transcribe", reason: "Button action verb" },
  ],
};

// Exact protocol/recipe wording remains valid where users must match an app's
// actual field or paste a protocol-specific configuration value.
const technicalExceptions: Record<string, { value: string; reason: string }[]> =
  {
    "app/apps/page.tsx": [
      {
        value: "Base URL",
        reason: "Exact field label used in app setup recipes",
      },
    ],
    "app/home/page.tsx": [
      {
        value: "Base URL",
        reason: "Exact field label used in app setup recipes",
      },
    ],
    "app/providers/page.tsx": [
      {
        value: "Provider base URL",
        reason: "Provider configuration field label",
      },
    ],
    "app/settings/page.tsx": [
      { value: "Public base URL", reason: "Gateway configuration field label" },
    ],
    "components/admin/add-service-dialog.tsx": [
      {
        value: "Base URL",
        reason: "Engine endpoint configuration field label",
      },
    ],
    "app/home/recipes.ts": [
      {
        value: "Base URL",
        reason: "Exact field label used by OpenAI-compatible app recipes",
      },
      {
        value: "Override OpenAI Base URL",
        reason: "Exact Cursor setting label",
      },
      {
        value:
          "In Cursor Settings → Models, enable your OpenAI API key, set Override OpenAI Base URL, and add the model below. Cursor’s servers must reach this URL; a localhost or private LAN URL will not work. Your key is sent through Cursor’s servers. Custom keys cover chat; Tab uses Cursor’s models.",
        reason: "Third-party Cursor setup recipe must name its exact setting",
      },
      {
        value:
          "Set GATEWAY_API_KEY to your saved key first, then run these commands in the shell that starts Claude Code. Check /status after launch to confirm the gateway URL and credential.",
        reason:
          "Third-party Claude Code recipe must name its exact environment variable and status command",
      },
    ],
  };

function sourceFiles(directory: string): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(fullPath);
    return /\.(tsx?|jsx?)$/.test(entry.name) ? [fullPath] : [];
  });
}

function outputStrings(node: ts.Expression): string[] {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
    return [node.text];
  if (ts.isTemplateExpression(node))
    return [
      node.head.text,
      ...node.templateSpans.map((span) => span.literal.text),
    ];
  if (
    ts.isParenthesizedExpression(node) ||
    ts.isAsExpression(node) ||
    ts.isTypeAssertionExpression(node) ||
    ts.isSatisfiesExpression(node)
  ) {
    return outputStrings(node.expression);
  }
  if (ts.isConditionalExpression(node))
    return [...outputStrings(node.whenTrue), ...outputStrings(node.whenFalse)];
  if (
    ts.isBinaryExpression(node) &&
    node.operatorToken.kind === ts.SyntaxKind.PlusToken
  ) {
    return [...outputStrings(node.left), ...outputStrings(node.right)];
  }
  if (
    ts.isBinaryExpression(node) &&
    [
      ts.SyntaxKind.BarBarToken,
      ts.SyntaxKind.AmpersandAmpersandToken,
      ts.SyntaxKind.QuestionQuestionToken,
    ].includes(node.operatorToken.kind)
  ) {
    return [...outputStrings(node.left), ...outputStrings(node.right)];
  }
  return [];
}

const copyFieldNames = new Set([
  "label",
  "title",
  "description",
  "placeholder",
  "ariaLabel",
  "emptyTitle",
  "emptyDescription",
  "helperText",
  "errorText",
]);

function copyStrings(source: ts.SourceFile): string[] {
  const strings: string[] = [];

  function visit(node: ts.Node) {
    if (ts.isJsxText(node)) strings.push(node.getText(source).trim());
    if (ts.isJsxAttribute(node) && node.initializer) {
      const name = node.name.getText(source).toLowerCase();
      if (
        /^(aria-label|placeholder|title|label|description|helpertext|error|emptytitle|emptydescription)$/.test(
          name,
        )
      ) {
        if (
          ts.isJsxExpression(node.initializer) &&
          node.initializer.expression
        ) {
          strings.push(...outputStrings(node.initializer.expression));
        } else if (ts.isStringLiteral(node.initializer))
          strings.push(node.initializer.text);
      }
    }
    if (
      ts.isPropertyAssignment(node) &&
      copyFieldNames.has(node.name.getText(source).replace(/^['"]|['"]$/g, ""))
    ) {
      strings.push(...outputStrings(node.initializer));
    }
    // Conditional and nested JSX expressions often select labels from inline
    // maps. Walk their descendants instead of checking only a direct literal.
    if (
      ts.isJsxExpression(node) &&
      node.expression &&
      !ts.isJsxAttribute(node.parent)
    ) {
      strings.push(...outputStrings(node.expression));
    }
    ts.forEachChild(node, visit);
  }

  visit(source);
  return strings.filter(Boolean);
}

function copyViolations(text: string, relative: string): string[] {
  const source = ts.createSourceFile(
    relative,
    text,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const allowed = [...(legacyCopy[relative] ?? [])];
  const exceptions = [
    ...(contextualExceptions[relative] ?? []),
    ...(technicalExceptions[relative] ?? []),
  ];
  const violations: string[] = [];
  for (const value of copyStrings(source)) {
    const pattern = banned.find((candidate) => candidate.test(value));
    if (!pattern) continue;
    const allowedIndex = allowed.indexOf(value);
    if (allowedIndex >= 0) {
      allowed.splice(allowedIndex, 1);
      continue;
    }
    const exceptionIndex = exceptions.findIndex(
      (exception) => exception.value === value,
    );
    if (exceptionIndex >= 0) {
      exceptions.splice(exceptionIndex, 1);
      continue;
    }
    violations.push(`${relative}: ${JSON.stringify(value)} (${pattern})`);
  }
  return violations;
}

describe("product vocabulary", () => {
  // "Voice" is the glossary's capability name (VOICE.md); the raw key "voice" is not.
  it("rejects new banned labels in maps, config fields, and nested JSX expressions", () => {
    const source = `
      const labels = { voice: { label: "voice" }, summary: { description: "Rerank" } };
      const action = { title: condition ? "Decide" : "Chat", description: \`Rerank\` };
      export function Example() { return <p>{condition ? "Embed" : labels.voice.label}</p>; }
    `;
    expect(copyViolations(source, "app/new-page/page.tsx")).toHaveLength(5);
  });

  it("does not treat technical identifiers as presentation copy", () => {
    expect(
      copyViolations(
        `const request = { profile_id: "profile_id" };`,
        "lib/api-types.ts",
      ),
    ).toEqual([]);
  });

  it("allows the Voice speaker selector and Transcribe action explicitly", () => {
    expect(
      copyViolations(
        `<label>Voice</label><button>Transcribe</button>`,
        "app/try/page.tsx",
      ),
    ).toEqual([]);
  });

  it("keeps user-facing copy within the shared glossary", () => {
    const violations: string[] = [];
    for (const file of sourceFiles(sourceRoot)) {
      const relative = path
        .relative(sourceRoot, file)
        .split(path.sep)
        .join("/");
      if (/\.spec\./.test(relative)) continue;
      violations.push(
        ...copyViolations(fs.readFileSync(file, "utf8"), relative),
      );
    }
    expect(violations).toEqual([]);
  });
});
