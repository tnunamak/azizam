// Case receipts for `node --test` (docs/design/TEST_STRATEGY.md 3.1, 7 T0).
// A node:test reporter that appends one JSON line per test case to
// $CASE_RECEIPTS_FILE in the shape of web/tests/case-receipts.ts, so
// scripts/ci/merge_cases.py reads node cases like vitest cases.
// Use beside a human reporter:
// `node --test --test-reporter=spec --test-reporter-destination=stdout
//  --test-reporter=<this file> --test-reporter-destination=stderr`.
import { appendFileSync } from "node:fs";

export default async function* caseReceipts(source) {
  const output = process.env.CASE_RECEIPTS_FILE;
  const lines = [];
  const ancestors = [];
  for await (const event of source) {
    if (event.type === "test:start") {
      ancestors[event.data.nesting] = event.data.name;
    } else if (event.type === "test:pass" || event.type === "test:fail") {
      const { name, nesting, file, skip, todo, details } = event.data;
      if (details?.type === "suite") continue;
      const titles = [...ancestors.slice(1, nesting), name];
      const status = event.type === "test:fail" ? "failed" : skip || todo ? "skipped" : "passed";
      lines.push(JSON.stringify({
        framework: "node", config: "", project: "", id: `${file}::${titles.join(" > ")}`,
        file, titles, status, duration_ms: details?.duration_ms ?? 0,
      }));
    }
  }
  if (output && lines.length) appendFileSync(output, lines.join("\n") + "\n");
}
