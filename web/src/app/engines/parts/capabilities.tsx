"use client";

/**
 * "What this server can do": one row per capability with its tick, how sure
 * Azizam is, the evidence, and a real test. Used by step 3 of the dialog and by
 * the engine page.
 */
import { useId, type ReactNode } from "react";
import { Check, CircleCheck, CircleHelp, CircleX, Circle, Lock, Loader2, type LucideIcon } from "lucide-react";
import type { CapabilityLevel, CheckedCapability, SentRequest, StoredTest, TestResult } from "@/lib/engines-api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { clockTime } from "../add-engine/draft";
import { FindingDetails } from "./finding-card";
import { Note } from "./form";

export type RowLevel = CapabilityLevel | "unchecked";

export interface CapabilityRow {
  id: string;
  label: string;
  level: RowLevel;
  evidence?: string;
  why?: string;
}

export type TestState =
  | { status: "running"; snapshot?: string }
  | { status: "done"; result: TestResult; snapshot?: string; tickedForYou?: boolean }
  | { status: "stored"; stored: StoredTest }
  | { status: "error"; message: string; snapshot?: string };

const LEVEL: Record<RowLevel, { word: string; icon: LucideIcon; className: string }> = {
  likely: { word: "Likely", icon: CircleCheck, className: "bg-success-tint text-success" },
  possible: { word: "Possible", icon: CircleHelp, className: "bg-warning-tint text-warning" },
  declared: { word: "You chose it", icon: Check, className: "bg-info-tint text-info" },
  against: { word: "Not supported", icon: CircleX, className: "bg-muted text-subtle-foreground" },
  none: { word: "Not found", icon: Circle, className: "bg-muted text-subtle-foreground" },
  needs_key: { word: "Needs a key", icon: Lock, className: "bg-muted text-subtle-foreground" },
  unchecked: { word: "Not checked yet", icon: Circle, className: "bg-muted text-subtle-foreground" },
};
const ORDER: Record<RowLevel, number> = { declared: 0, likely: 0, possible: 1, against: 2, none: 3, needs_key: 3, unchecked: 4 };

export function LevelBadge({ level, id }: { level: RowLevel; id?: string }) {
  const { word, icon: Icon, className } = LEVEL[level];
  return (
    <span
      id={id}
      className={cn("rounded-badge text-badge inline-flex h-5 shrink-0 items-center gap-1 px-1.5 whitespace-nowrap", className)}
    >
      <Icon aria-hidden="true" className="size-3.5" />
      {word}
    </span>
  );
}

/** A passing real test on a row raises it to Likely, with the test as its evidence. */
export function withTests(rows: CapabilityRow[], tests: Record<string, TestState | undefined>): CapabilityRow[] {
  return rows.map((row) => {
    const test = tests[row.id];
    const passed = test?.status === "done" && test.result.ok;
    if (!passed || row.level === "declared") return row;
    return {
      ...row,
      level: "likely",
      evidence: `A test of ${row.label.toLowerCase()}${test.result.model ? ` with ${test.result.model}` : ""} passed at ${new Date(test.result.at * 1000).toLocaleString()} using ${test.result.via === "saved" ? "saved" : "unsaved"} settings.`,
      why: undefined,
    };
  });
}

export function sortRows(rows: CapabilityRow[]): CapabilityRow[] {
  return [...rows].sort((a, b) => ORDER[a.level] - ORDER[b.level]);
}

export function rowsFromCheck(caps: CheckedCapability[]): CapabilityRow[] {
  return caps.map(({ id, label, level, evidence, why }) => ({ id, label, level, evidence, why }));
}

export function TestResultBlock({
  test,
  label,
  note,
  onTestAgain,
  disabled,
}: {
  test: TestState;
  label: string;
  note: string;
  onTestAgain: () => void;
  disabled?: boolean;
}) {
  if (test.status === "running")
    return (
      <p role="status" className="text-meta text-muted-foreground flex items-center gap-2">
        <Loader2 aria-hidden="true" className="size-4 animate-spin" />
        {note.startsWith("Tested the saved settings")
          ? "Testing with the saved settings…"
          : "Testing from the gateway’s computer…"}
      </p>
    );
  if (test.status === "error")
    return (
      <div className="flex flex-col gap-2">
        <Note tone="warn" role="alert">
          The test could not run. {test.message}
        </Note>
        <Button type="button" variant="ghost" size="sm" className="w-fit" disabled={disabled} onClick={onTestAgain}>
          Test again
        </Button>
      </div>
    );
  const ok = test.status === "done" ? test.result.ok : test.stored.ok;
  const at = test.status === "done" ? test.result.at : test.stored.at;
  const head = ok
    ? `Test passed at ${clockTime(at)}.${test.status === "done" && test.tickedForYou ? " Ticked for you." : ""}`
    : `Test failed at ${clockTime(at)}. See the reason below.`;
  const Icon = ok ? CircleCheck : CircleX;
  const rows: [string, ReactNode][] = [];
  if (test.status === "done") {
    const r = test.result;
    rows.push(["Sent", r.sent], ["Sent to", <span key="to" className="font-mono">{r.sent_to}</span>], ["Reply", r.reply], ["Time", `${r.seconds} s`], ["Cost", r.cost]);
  } else if (test.stored.seconds !== undefined) {
    rows.push(["Time", `${test.stored.seconds} s`]);
  }
  const finding = test.status === "done" && !ok ? test.result.finding : undefined;
  return (
    <div
      role={ok ? "status" : "alert"}
      aria-label={`Real test of ${label}`}
      className={cn("rounded-control border p-3", ok ? "border-success/30 bg-success-tint/40" : "border-danger/40 bg-danger-tint/40")}
    >
      <p className={cn("text-meta flex items-center gap-2 font-medium", ok ? "text-success" : "text-danger")}>
        <Icon aria-hidden="true" className="size-4 shrink-0" />
        <span>{head}</span>
      </p>
      {finding && (
        <dl className="text-meta mt-2 grid gap-x-3 gap-y-1 sm:grid-cols-[auto_1fr]">
          <dt className="text-muted-foreground">What happened</dt>
          <dd>{finding.what}</dd>
          <dt className="text-muted-foreground">Why</dt>
          <dd>{finding.why}</dd>
          <dt className="text-muted-foreground">What to do</dt>
          <dd>{finding.fix}</dd>
        </dl>
      )}
      {rows.length > 0 && (
        <dl className="text-meta mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {rows.map(([term, value]) => (
            <div key={term} className="contents">
              <dt className="text-muted-foreground">{term}</dt>
              <dd className="min-w-0 [overflow-wrap:anywhere]">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {test.status === "done" && test.result.request && <RequestTrace request={test.result.request} />}
      <p className="text-meta text-muted-foreground mt-2">{note} <time dateTime={new Date(at * 1000).toISOString()}>{new Date(at * 1000).toLocaleDateString()}</time></p>
      <Button type="button" variant="ghost" size="sm" className="mt-1" disabled={disabled} onClick={onTestAgain}>
        Test again
      </Button>
      {finding && <FindingDetails raw={finding.raw} rawLabel="What the server said" />}
    </div>
  );
}

export function CapabilityList({
  rows,
  ticked,
  onTick,
  tests,
  onTest,
  testNote,
  storedNote,
  readOnly,
}: {
  rows: CapabilityRow[];
  ticked: ReadonlySet<string>;
  onTick: (id: string, on: boolean) => void;
  tests: Record<string, TestState | undefined>;
  onTest?: (id: string) => void;
  /** Under a test run in this session. */
  testNote: string;
  /** Under a test stored on the record. */
  storedNote?: string;
  readOnly?: boolean;
}) {
  return (
    <div role="group" aria-label="What this server can do" className="bg-card rounded-panel divide-y border">
      {sortRows(rows).map((row) => (
        <CapabilityRowView
          key={row.id}
          row={row}
          on={ticked.has(row.id)}
          onTick={onTick}
          test={tests[row.id]}
          onTest={onTest}
          testNote={testNote}
          storedNote={storedNote}
          readOnly={readOnly}
        />
      ))}
    </div>
  );
}

function CapabilityRowView({
  row,
  on,
  onTick,
  test,
  onTest,
  testNote,
  storedNote,
  readOnly,
}: {
  row: CapabilityRow;
  on: boolean;
  onTick: (id: string, on: boolean) => void;
  test: TestState | undefined;
  onTest?: (id: string) => void;
  testNote: string;
  storedNote?: string;
  readOnly?: boolean;
}) {
  const base = useId();
  const unchecked = row.level === "unchecked";
  const dim = unchecked || row.level === "none" || row.level === "against" || row.level === "needs_key";
  const testable = !unchecked && !readOnly && onTest && (on || row.level === "possible");
  const showEvidence = !unchecked && Boolean(row.evidence);
  const showWhy = !unchecked && Boolean(row.why) && !on;
  const showWarning = on && !unchecked && row.level !== "likely" && row.level !== "declared";
  // The ticks are named by the capability; the level, the evidence and the warning describe them.
  const description = [
    `${base}-level`,
    showEvidence && `${base}-evidence`,
    showWhy && `${base}-why`,
    showWarning && `${base}-warning`,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="flex flex-col gap-2 p-4">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <label className="text-body flex min-h-8 cursor-pointer items-center gap-3 font-medium max-sm:min-h-11">
          <Checkbox
            aria-label={row.label}
            aria-describedby={description}
            checked={on}
            disabled={readOnly}
            onCheckedChange={(value) => onTick(row.id, value === true)}
          />
          <span className={cn(dim && !on && "text-muted-foreground")}>{row.label}</span>
        </label>
        <LevelBadge id={`${base}-level`} level={row.level} />
      </div>
      <div className="flex flex-col gap-2 pl-7">
        {showEvidence && (
          <p id={`${base}-evidence`} className="text-meta text-muted-foreground">
            {row.evidence}
          </p>
        )}
        {showWhy && (
          <p id={`${base}-why`} className="text-meta text-muted-foreground">
            <b className="text-foreground font-medium">Why unticked:</b> {row.why}
          </p>
        )}
        {showWarning && (
          <Note tone="warn" id={`${base}-warning`}>
            {row.level === "possible"
              ? "Not proven yet. Run a real test to be sure."
              : "Azizam could not see this. You can turn it on, but requests may fail."}
          </Note>
        )}
        {test && !unchecked ? (
          <TestResultBlock
            test={test}
            label={row.label}
            note={
              test.status === "stored"
                ? (storedNote ?? testNote)
                : test.status === "done"
                  ? test.result.via === "saved"
                    ? "Tested the saved settings from the gateway’s computer. This test is not listed in Activity."
                    : "Tested your unsaved changes from the gateway’s computer. The test did not save your changes and is not listed in Activity."
                  : testNote
            }
            disabled={readOnly || !onTest}
            onTestAgain={() => onTest?.(row.id)}
          />
        ) : (
          testable && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="w-fit"
              aria-label={`Run a real test for ${row.label}`}
              onClick={() => onTest?.(row.id)}
            >
              Run a real test
            </Button>
          )
        )}
      </div>
    </div>
  );
}

export function CapabilityLegend() {
  const items: [RowLevel, string][] = [
    ["likely", "The server’s own details say so, or a real test passed. Ticked for you."],
    ["possible", "The server answers, but nothing proves it. Left unticked."],
    ["against", "See the reason below. Left unticked."],
  ];
  return (
    <div className="flex flex-col gap-1.5">
      {items.map(([level, text]) => (
        <div key={level} className="text-meta text-muted-foreground flex flex-wrap items-center gap-2">
          <LevelBadge level={level} />
          <span>{text}</span>
        </div>
      ))}
    </div>
  );
}

/** Exactly what the test sent: method, URL, headers (the key hidden by the gateway), body and time limit. */
export function RequestTrace({ request }: { request: SentRequest }) {
  const headers = Object.entries(request.headers ?? {});
  const body =
    request.body === undefined || request.body === null
      ? ""
      : typeof request.body === "string"
        ? request.body
        : JSON.stringify(request.body, null, 2);
  return (
    <details className="text-meta mt-2">
      <summary className="text-link cursor-pointer max-sm:min-h-11">Request sent, with secrets hidden</summary>
      <pre
        tabIndex={0}
        aria-label="Request sent, with secrets hidden"
        className="bg-muted rounded-control mt-1 max-h-72 overflow-auto p-2 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]"
      >
        {[
          `${request.method} ${request.url}`,
          ...headers.map(([name, value]) => `${name}: ${value}`),
          ...(body ? ["", body] : []),
        ].join("\n")}
      </pre>
      {request.timeout_s !== undefined && <p className="text-muted-foreground mt-1">Waits up to {request.timeout_s} seconds for the reply.</p>}
    </details>
  );
}
