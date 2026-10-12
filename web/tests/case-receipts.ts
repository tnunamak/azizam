// Case receipts for scripts/ci-local (docs/design/TEST_STRATEGY.md 3.1, 7 T0).
// One JSON line per attempt, appended to $CASE_RECEIPTS_FILE: a retried test
// writes a line per attempt, and a second run into the same file repeats its
// test IDs, so scripts/ci/merge_cases.py sees both as attempts above 0.
// Use with `vitest run --reporter=default --reporter=./tests/case-receipts.ts`.
import { appendFileSync } from "node:fs";
import type { Reporter, TestCase, TestModule } from "vitest/node";

export default class CaseReceipts implements Reporter {
  onTestRunEnd(testModules: ReadonlyArray<TestModule>) {
    const output = process.env.CASE_RECEIPTS_FILE;
    if (!output) return;
    const lines: string[] = [];
    for (const testModule of testModules) {
      for (const test of testModule.children.allTests()) {
        const titles = [test.name];
        for (let node: TestCase["parent"] = test.parent; node.type === "suite"; node = node.parent) {
          titles.unshift(node.name);
        }
        const diagnostic = test.diagnostic();
        // A retry follows a failed attempt; a repeat follows a passed one.
        const earlier = [
          ...Array<string>(diagnostic?.retryCount ?? 0).fill("failed"),
          ...Array<string>(diagnostic?.repeatCount ?? 0).fill("passed"),
        ];
        for (const status of [...earlier, test.result().state]) {
          lines.push(JSON.stringify({
            framework: "vitest", config: "vitest.config.ts", project: "", id: test.id,
            file: testModule.moduleId, titles, status, duration_ms: diagnostic?.duration ?? 0,
          }));
        }
      }
    }
    if (lines.length) appendFileSync(output, lines.join("\n") + "\n");
  }
}
