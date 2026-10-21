"use client";
import { useId, useState } from "react";
import {
  AlertTriangle,
  Check,
  CircleCheck,
  ExternalLink,
  Link2,
  Loader2,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type {
  PlanRef,
  SignInRule,
  TestVerdict,
} from "@/lib/signin-api";
import { cn } from "@/lib/cn";
import { DisclosureButton, LinkButton } from "./bits";
import {
  causeTitle,
  formatWhen,
  localCauseDetails,
  localCauseText,
  mid,
  noGroupsText,
  noGroupsTitle,
  testCaption,
  testWorks,
  verdictCopy,
  type LocalCause,
  type ProviderLabel,
} from "./copy";
import type { TestResult } from "./use-sign-in-test";

export function TestSignIn({
  label,
  ready,
  addressBad,
  waiting,
  tested,
  testedWhen,
  changedSinceLastTest,
  result,
  verdict,
  checkingVerdict,
  rules,
  plans,
  justLinked,
  linking,
  connectionOpen,
  onTest,
  onCancel,
  onDismiss,
  onLink,
  onEditConnection,
}: {
  label: ProviderLabel;
  ready: boolean;
  addressBad: boolean;
  waiting: boolean;
  tested: boolean;
  testedWhen: string;
  changedSinceLastTest: boolean;
  result: TestResult | null;
  verdict: TestVerdict | null;
  checkingVerdict: boolean;
  rules: SignInRule[];
  plans: PlanRef[];
  justLinked: boolean;
  linking: boolean;
  connectionOpen: boolean;
  onTest: () => void;
  onCancel: () => void;
  onDismiss: () => void;
  onLink: (testId: string) => void;
  onEditConnection: () => void;
}) {
  const n = mid(label);
  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 p-4">
        <div className="min-w-0 flex-1 basis-72">
          <p className="text-body font-medium">Test sign-in</p>
          <p className="text-muted-foreground text-meta">
            {testCaption({
              waiting,
              addressBad,
              ready,
              tested,
              testedWhen,
              changedSinceLastTest,
              p: label,
            })}
          </p>
        </div>
        <div className="flex gap-2 max-sm:w-full max-sm:[&>*]:flex-1">
          {waiting ? (
            <>
              <Button variant="outline" disabled>
                <Loader2 className="animate-spin" />
                Waiting for {n}...
              </Button>
              <Button variant="ghost" onClick={onCancel}>
                Cancel
              </Button>
            </>
          ) : (
            <Button variant="outline" disabled={!ready} onClick={onTest}>
              <ExternalLink />
              Test sign-in
            </Button>
          )}
        </div>
      </div>
      {result && !waiting && (
        <ResultCard
          label={label}
          result={result}
          verdict={verdict}
          checkingVerdict={checkingVerdict}
          rules={rules}
          plans={plans}
          justLinked={justLinked}
          linking={linking}
          connectionOpen={connectionOpen}
          onTest={onTest}
          onDismiss={onDismiss}
          onLink={onLink}
          onEditConnection={onEditConnection}
        />
      )}
    </div>
  );
}

const EDITABLE_CAUSES = new Set(["redirect_not_allowed", "wrong_secret", "unreachable"]);

function ResultCard({
  label,
  result,
  verdict,
  checkingVerdict,
  rules,
  plans,
  justLinked,
  linking,
  connectionOpen,
  onTest,
  onDismiss,
  onLink,
  onEditConnection,
}: {
  label: ProviderLabel;
  result: TestResult;
  verdict: TestVerdict | null;
  checkingVerdict: boolean;
  rules: SignInRule[];
  plans: PlanRef[];
  justLinked: boolean;
  linking: boolean;
  connectionOpen: boolean;
  onTest: () => void;
  onDismiss: () => void;
  onLink: (testId: string) => void;
  onEditConnection: () => void;
}) {
  const detailsId = useId();
  const [showDetails, setShowDetails] = useState(false);
  const warn = result.kind === "pass" && result.status.warnings.includes("no_groups");

  if (result.kind === "fail" || warn) {
    const failed = result.kind === "fail" ? result : null;
    const title = failed ? causeTitle(failed.cause, label) : noGroupsTitle;
    const local = failed && ["blocked", "closed", "timeout", "start_failed"].includes(failed.cause);
    const text = failed
      ? local && !failed.message
        ? localCauseText(failed.cause as LocalCause, label)
        : (failed.message ?? "")
      : noGroupsText(label);
    const details = failed
      ? (failed.details ?? localCauseDetails[failed.cause as LocalCause] ?? "")
      : result.kind === "pass"
        ? result.status.details
        : "";
    return (
      <Card tone={failed ? "bad" : "warn"}>
        <CardHead
          tone={failed ? "bad" : "warn"}
          title={failed ? `Test failed: ${title}` : title}
          when={result.kind === "pass" ? formatWhen(result.status.tested_at) : "just now"}
        />
        <p className="text-body p-4">
          {text}
          {failed && EDITABLE_CAUSES.has(failed.cause) && !connectionOpen && (
            <>
              {" "}
              <LinkButton onClick={onEditConnection}>Edit connection</LinkButton>
            </>
          )}
        </p>
        {result.kind === "pass" && (
          <Verdict
            label={label}
            verdict={verdict}
            checking={checkingVerdict}
            rules={rules}
            plans={plans}
            justLinked={justLinked}
            linking={linking}
            testId={result.testId}
            onLink={onLink}
          />
        )}
        <Details
          id={detailsId}
          open={showDetails}
          onToggle={() => setShowDetails((v) => !v)}
          text={details}
        />
        <div className="text-muted-foreground flex flex-wrap items-center justify-end gap-2 border-t px-4 py-2.5">
          <Button variant="outline" onClick={onTest}>
            <ExternalLink />
            Test again
          </Button>
          <Button variant="ghost" onClick={onDismiss}>
            Dismiss
          </Button>
        </div>
      </Card>
    );
  }

  const { status, testId } = result;
  const id = status.identity;
  return (
    <Card tone="ok">
      <CardHead
        tone="ok"
        title={testWorks(label)}
        when={formatWhen(status.tested_at)}
      />
      <dl className="text-meta grid grid-cols-[150px_1fr] gap-x-4 gap-y-2 p-4 max-sm:grid-cols-1 max-sm:gap-y-0.5">
        <dt className="text-muted-foreground max-sm:mt-2">Name</dt>
        <dd className="min-w-0 [overflow-wrap:anywhere]">{id.name}</dd>
        <dt className="text-muted-foreground max-sm:mt-2">Email</dt>
        <dd className="min-w-0 [overflow-wrap:anywhere]">
          {id.email}{" "}
          {id.email_verified ? (
            <Badge variant="state" state="ready" className="ml-1.5">
              <Check aria-hidden="true" />
              verified
            </Badge>
          ) : (
            <Badge variant="state" state="degraded" className="ml-1.5">
              <AlertTriangle aria-hidden="true" />
              not verified
            </Badge>
          )}
        </dd>
        <dt className="text-muted-foreground max-sm:mt-2">Groups received</dt>
        <dd className="min-w-0">
          <Chips values={id.groups} />
        </dd>
        <dt className="text-muted-foreground max-sm:mt-2">Roles received</dt>
        <dd className="min-w-0">
          <Chips values={id.roles} />
        </dd>
      </dl>
      <Verdict
        label={label}
        verdict={verdict}
        checking={checkingVerdict}
        rules={rules}
        plans={plans}
        justLinked={justLinked}
        linking={linking}
        testId={testId}
        onLink={onLink}
      />
      <Details
        id={detailsId}
        open={showDetails}
        onToggle={() => setShowDetails((v) => !v)}
        text={status.details}
      />
      <div className="text-muted-foreground text-label flex flex-wrap items-center gap-x-4 gap-y-2 border-t px-4 py-2.5">
        <span className="min-w-0 flex-1 basis-64">
          {id.groups.length > 0 &&
            `Azizam remembered the groups it saw: ${id.groups.join(", ")}. Pick them in the rules below.`}
        </span>
        <Button variant="ghost" onClick={onDismiss}>
          Dismiss
        </Button>
      </div>
    </Card>
  );
}

function Card({
  tone,
  children,
}: {
  tone: "ok" | "bad" | "warn";
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-panel mx-4 mb-4 overflow-hidden border max-sm:mx-3 max-sm:mb-3",
        tone === "ok" && "border-success/30",
        tone === "bad" && "border-danger/30",
        tone === "warn" && "border-warning/30",
      )}
    >
      {children}
    </div>
  );
}

function CardHead({
  tone,
  title,
  when,
}: {
  tone: "ok" | "bad" | "warn";
  title: string;
  when: string;
}) {
  const Icon = tone === "ok" ? CircleCheck : tone === "bad" ? XCircle : AlertTriangle;
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-2 gap-y-0.5 px-4 py-3 font-semibold",
        tone === "ok" && "bg-success-tint text-success",
        tone === "bad" && "bg-danger-tint text-danger",
        tone === "warn" && "bg-warning-tint text-warning",
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <Icon aria-hidden="true" className="size-4 shrink-0" />
        <span>{title}</span>
      </span>
      <span className="text-label ml-auto font-medium max-sm:ml-0 max-sm:basis-full max-sm:font-normal">
        {when}
      </span>
    </div>
  );
}

function Chips({ values }: { values: string[] }) {
  if (!values.length) return <span className="text-muted-foreground">None</span>;
  return (
    <>
      {values.map((v) => (
        <span
          key={v}
          className="bg-muted rounded-badge text-label mr-1.5 mb-1 inline-flex max-w-full items-center px-2 py-0.5 font-mono [overflow-wrap:anywhere]"
        >
          {v}
        </span>
      ))}
    </>
  );
}

function Details({
  id,
  open,
  onToggle,
  text,
}: {
  id: string;
  open: boolean;
  onToggle: () => void;
  text: string;
}) {
  if (!text) return null;
  return (
    <div className="text-label border-t px-4 py-2.5">
      <DisclosureButton
        open={open}
        onClick={onToggle}
        controls={id}
        className="text-link font-medium"
      >
        Show details
      </DisclosureButton>
      {open && (
        <pre
          id={id}
          className="bg-muted rounded-control mt-2 px-3 py-2.5 font-mono text-sm whitespace-pre-wrap [overflow-wrap:anywhere]"
        >
          {text}
        </pre>
      )}
    </div>
  );
}

function Verdict({
  label,
  verdict,
  checking,
  rules,
  plans,
  justLinked,
  linking,
  testId,
  onLink,
}: {
  label: ProviderLabel;
  verdict: TestVerdict | null;
  checking: boolean;
  rules: SignInRule[];
  plans: PlanRef[];
  justLinked: boolean;
  linking: boolean;
  testId: string;
  onLink: (testId: string) => void;
}) {
  if (!verdict)
    return checking ? (
      <p role="status" className="text-muted-foreground text-meta border-t px-4 py-3.5">
        Checking what your rules say…
      </p>
    ) : null;
  const v = verdictCopy(verdict, { p: label, rules, plans, justLinked });
  return (
    <div
      className={cn(
        "border-t px-4 py-3.5",
        v.plain ? "bg-card text-foreground" : "bg-accent text-accent-foreground",
      )}
    >
      <p className="text-heading">{v.big}</p>
      {v.why && <p className="text-meta mt-0.5">{v.why}</p>}
      {v.link && (
        <div className="mt-2.5">
          <Button disabled={linking} onClick={() => onLink(testId)}>
            <Link2 />
            {v.link}
          </Button>
        </div>
      )}
    </div>
  );
}
