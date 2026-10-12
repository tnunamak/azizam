// Case receipts for scripts/ci-local (docs/design/TEST_STRATEGY.md 3.1, 7 T0).
// One JSON line per attempt, appended to $CASE_RECEIPTS_FILE as each attempt
// ends: a retry or a second run into the same file repeats the test ID, so
// scripts/ci/merge_cases.py sees it as an attempt above 0.
import { appendFileSync } from "node:fs";
import path from "node:path";
import type { FullConfig, Reporter, TestCase, TestResult } from "@playwright/test/reporter";

export default class CaseReceipts implements Reporter {
  private config = "";

  onBegin(config: FullConfig) {
    this.config = path.basename(config.configFile ?? "");
  }

  onTestEnd(test: TestCase, result: TestResult) {
    const output = process.env.CASE_RECEIPTS_FILE;
    if (!output) return;
    appendFileSync(output, JSON.stringify({
      framework: "playwright", config: this.config, project: test.parent.project()?.name ?? "",
      id: test.id, file: test.location.file,
      // titlePath() starts with the root, project and file suites.
      titles: test.titlePath().slice(3), status: result.status, duration_ms: result.duration,
    }) + "\n");
  }
}
