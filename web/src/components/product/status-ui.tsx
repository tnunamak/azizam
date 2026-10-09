"use client";

import { useState, type ReactNode } from "react";
import {
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleMinus,
  CircleX,
  Clock3,
  Info,
  Lock,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import type { CapabilityView, StateTone } from "@/lib/capability-state";
import { RelativeTime } from "@/components/ui/relative-time";
import { capabilityMeta } from "./readiness";

/* One icon, one colour and one word per state tone. */
const toneIcon: Record<StateTone, LucideIcon> = {
  ok: CircleCheck,
  busy: Clock3,
  warn: CircleAlert,
  bad: CircleX,
  blocked: Lock,
  off: CircleMinus,
  unknown: CircleDashed,
};
const toneText: Record<StateTone, string> = {
  ok: "text-success",
  busy: "text-info",
  warn: "text-warning",
  bad: "text-danger",
  blocked: "text-blocked",
  off: "text-subtle-foreground",
  unknown: "text-subtle-foreground",
};

export function StateIcon({
  tone,
  className,
  label,
}: {
  tone: StateTone;
  className?: string;
  label?: string;
}) {
  const Icon = toneIcon[tone];
  return (
    <Icon
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? "img" : undefined}
      className={cn("size-4 shrink-0", toneText[tone], className)}
    />
  );
}

/** A state word: icon and word in the tone colour. Ready stays quiet. */
export function StateWord({
  tone,
  word,
  className,
}: {
  tone: StateTone;
  word: string;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-sm",
        tone === "ok"
          ? "text-muted-foreground"
          : cn("font-medium", toneText[tone]),
        className,
      )}
    >
      <StateIcon tone={tone} />
      {word}
    </span>
  );
}

/**
 * The verdict line under a page title: one label (no full stop), its state
 * icon, and the age of the evidence behind it.
 */
export function StatusLine({
  tone,
  text,
  checkedAt,
  children,
  className,
  compact = false,
  unconfirmed = 0,
}: {
  tone: StateTone;
  text: string;
  checkedAt?: number | null;
  /**
   * Capabilities the check time does not vouch for (Unknown): said beside
   * the time, so "Checked just now" never reads as a fresh check of them.
   */
  unconfirmed?: number;
  children?: ReactNode;
  className?: string;
  /**
   * One 14 px line (Home): the verdict, its age and any action side by side,
   * so a landing page leads with what to do, not with health (D1.3). Status
   * keeps the large verdict; it is that page's answer.
   */
  compact?: boolean;
}) {
  if (compact)
    return (
      <div
        className={cn(
          "flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm",
          className,
        )}
      >
        <p
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className="flex min-w-0 items-center gap-1.5 font-semibold"
        >
          <StateIcon tone={tone} className="size-4" />
          <span className="min-w-0">{text}</span>
        </p>
        {checkedAt != null ? (
          <span className="text-muted-foreground">
            Checked <RelativeTime ts={checkedAt} fallback="" />
            {unconfirmed ? ` · ${unconfirmed} unknown` : ""}
          </span>
        ) : null}
        {children}
      </div>
    );
  return (
    <div className={cn("min-w-0", className)}>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="flex items-center gap-2.5 text-xl font-semibold tracking-tight"
      >
        <StateIcon tone={tone} className="size-5" />
        <span className="min-w-0">{text}</span>
      </p>
      {checkedAt != null || children ? (
        <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 pl-7.5 text-sm">
          {checkedAt != null ? (
            <span>
              Checked <RelativeTime ts={checkedAt} fallback="" />
              {unconfirmed ? ` · ${unconfirmed} unknown` : ""}
            </span>
          ) : null}
          {children}
        </div>
      ) : null}
    </div>
  );
}

export type CalloutSeverity = "info" | "attention" | "critical";
const severityIcon: Record<CalloutSeverity, LucideIcon> = {
  info: Info,
  attention: CircleAlert,
  critical: CircleX,
};
const severityText: Record<CalloutSeverity, string> = {
  info: "text-info",
  attention: "text-warning",
  critical: "text-danger",
};

/**
 * The one callout: a card with a severity icon, a title that names the
 * subject, optional facts, a body and actions. Used for every problem,
 * notice and error, on every surface. `headerAction` puts the one action on
 * the title row on wide screens (problem cards), so no column sits empty
 * under it; on a phone it follows the body, so the instruction comes before
 * the button that carries it out. `evidence` facts follow `facts` on wide
 * screens and fold behind one control on a phone (D1.3: lead with scope and
 * the most useful evidence, fold the rest).
 */
export function Callout({
  severity,
  title,
  meta,
  facts,
  evidence,
  children,
  actions,
  headerAction,
  as: Tag = "section",
  className,
  headingLevel = 2,
  history,
  variant = "default",
}: {
  severity: CalloutSeverity;
  title: ReactNode;
  meta?: ReactNode;
  facts?: Array<[string, ReactNode]>;
  evidence?: Array<[string, ReactNode]>;
  children?: ReactNode;
  actions?: ReactNode;
  headerAction?: ReactNode;
  as?: "section" | "article" | "div";
  className?: string;
  headingLevel?: 2 | 3;
  /** The incident variant's history block (the 24-hour strip and story). */
  history?: ReactNode;
  /**
   * "incident" (Status problem cards, D1.6): one order at every width.
   * What is wrong and who is affected, then what to do in full-weight text
   * with its button, then history, then evidence folded behind one control.
   */
  variant?: "default" | "incident";
}) {
  const Icon = severityIcon[severity];
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const rows = (items: Array<[string, ReactNode]>) =>
    items.map(([label, value]) => (
      <div key={label} className="contents">
        <dt className="text-muted-foreground max-sm:font-medium">
          {label}
        </dt>
        <dd className="min-w-0 max-sm:mb-1">{value}</dd>
      </div>
    ));
  // One column on a phone, the label above its value (D1.5: the squeezed
  // two-column card wrapped the evidence mid-phrase); 14 px values on wide
  // screens.
  const factGrid =
    "grid grid-cols-1 gap-y-1 text-sm sm:grid-cols-[8.5rem_minmax(0,1fr)] sm:gap-x-4 sm:gap-y-2 sm:text-base";
  if (variant === "incident")
    return (
      <Tag
        role={severity === "critical" && Tag === "div" ? "alert" : undefined}
        className={cn(
          "bg-card rounded-panel min-w-0 border p-4 sm:p-5",
          className,
        )}
      >
        <div className="flex min-w-0 items-start gap-3">
          <Icon
            aria-hidden="true"
            className={cn("mt-0.5 size-5 shrink-0", severityText[severity])}
          />
          <div className="min-w-0">
            <Heading className="text-lg font-semibold">{title}</Heading>
            {meta ? (
              <p className="text-muted-foreground text-sm">{meta}</p>
            ) : null}
          </div>
        </div>
        <div className="mt-4 min-w-0 space-y-5 sm:pl-8">
          {facts?.length ? <dl className={factGrid}>{rows(facts)}</dl> : null}
          {children || actions ? (
            <div className="space-y-3">
              {children ? (
                <div className="text-foreground space-y-1 text-base font-medium">
                  {children}
                </div>
              ) : null}
              {actions ? (
                <div className="flex flex-wrap items-center gap-2">
                  {actions}
                </div>
              ) : null}
            </div>
          ) : null}
          {history ? <div className="border-t pt-4">{history}</div> : null}
          {evidence?.length ? (
            <div className="space-y-2">
              <button
                type="button"
                aria-expanded={evidenceOpen}
                onClick={() => setEvidenceOpen((open) => !open)}
                className="text-link inline-flex min-h-11 items-center gap-1 text-sm font-medium sm:min-h-8"
              >
                <ChevronRight
                  aria-hidden="true"
                  className={cn(
                    "size-4 opacity-75 transition-transform",
                    evidenceOpen && "rotate-90",
                  )}
                />
                {evidenceOpen ? "Hide evidence" : "Show evidence"}
              </button>
              {evidenceOpen ? (
                <dl className={factGrid}>{rows(evidence)}</dl>
              ) : null}
            </div>
          ) : null}
        </div>
      </Tag>
    );
  return (
    <Tag
      role={severity === "critical" && Tag === "div" ? "alert" : undefined}
      className={cn(
        "bg-card rounded-panel min-w-0 border p-4 sm:p-5",
        className,
      )}
    >
      <div className="flex min-w-0 items-start gap-3">
        <Icon
          aria-hidden="true"
          className={cn("mt-0.5 size-5 shrink-0", severityText[severity])}
        />
        {/* On a phone only the title sits beside the icon; everything under
            it uses the card's full width (incident.io's phone card). The
            order classes put a header action after the body on a phone. */}
        <div className="flex min-w-0 flex-1 flex-col gap-3 sm:grid sm:grid-cols-[minmax(0,1fr)_auto] sm:gap-x-4">
          <div className="min-w-0 sm:col-start-1 sm:row-start-1">
            <Heading className="text-base font-semibold">{title}</Heading>
            {meta ? (
              <p className="text-muted-foreground text-sm">{meta}</p>
            ) : null}
          </div>
          {headerAction ? (
            <div className="flex shrink-0 items-start gap-2 max-sm:order-3 max-sm:-ml-8 sm:col-start-2 sm:row-start-1">
              {headerAction}
            </div>
          ) : null}
          {children ? (
            <div className="space-y-1 text-sm max-sm:order-2 max-sm:-ml-8 sm:col-span-2">
              {children}
            </div>
          ) : null}
          {/* A phone reads: title, scope and history (facts), the
              instruction, its button, then evidence on request (D1.4). */}
          {facts?.length ? (
            <dl
              className={cn(
                factGrid,
                "max-sm:order-1 max-sm:-ml-8 sm:col-span-2",
              )}
            >
              {rows(facts)}
            </dl>
          ) : null}
          {evidence?.length ? (
            <div className="space-y-2 max-sm:order-4 max-sm:-ml-8 sm:col-span-2">
              <button
                type="button"
                aria-expanded={evidenceOpen}
                onClick={() => setEvidenceOpen((open) => !open)}
                className="text-link inline-flex min-h-11 items-center gap-1 text-sm font-medium sm:hidden"
              >
                {/* The disclosure chevron every <summary> draws (D1.5). */}
                <ChevronRight
                  aria-hidden="true"
                  className={cn(
                    "size-4 opacity-75 transition-transform",
                    evidenceOpen && "rotate-90",
                  )}
                />
                {evidenceOpen ? "Hide evidence" : "Show evidence"}
              </button>
              <dl className={cn(factGrid, !evidenceOpen && "max-sm:hidden")}>
                {rows(evidence)}
              </dl>
            </div>
          ) : null}
          {actions ? (
            <div className="flex flex-wrap items-center gap-2 max-sm:order-5 max-sm:-ml-8 sm:col-span-2">
              {actions}
            </div>
          ) : null}
        </div>
      </div>
    </Tag>
  );
}

/**
 * The one way to draw capabilities: a row per capability with a quiet icon,
 * the name, the state word and the reason or the age of the evidence.
 */
export function CapabilityList({
  views,
  columns = 1,
  showDetail = true,
  actionFor,
}: {
  views: CapabilityView[];
  columns?: 1 | 2;
  showDetail?: boolean;
  actionFor?: (view: CapabilityView) => ReactNode;
}) {
  // D1.5: one row family across Home, Status and Library. Rows sit in one
  // card with hairlines between them (none above the first row of each
  // column), and lead with the capability's icon in its colour, as Home's
  // task rows do; the 32 px tinted tiles were a second row style.
  return (
    <ul
      className={cn(
        "bg-card rounded-panel grid border px-4",
        columns === 2 && "sm:grid-cols-2 sm:gap-x-8",
      )}
    >
      {views.map((view) => {
        const meta = capabilityMeta(view.key);
        const Icon = meta.icon;
        return (
          <li
            key={view.key}
            className={cn(
              "grid min-h-11 grid-cols-[1rem_minmax(0,1fr)_auto] items-center gap-x-3 border-t py-2.5 first:border-t-0",
              columns === 2 && "sm:nth-2:border-t-0",
            )}
          >
            <Icon
              aria-hidden="true"
              className={cn(
                "size-4",
                view.state === "off" || view.state === "blocked"
                  ? "text-muted-foreground"
                  : meta.tone,
              )}
            />
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{view.label}</p>
              {showDetail && view.detail ? (
                <p className="text-muted-foreground text-sm">{view.detail}</p>
              ) : null}
              {actionFor ? actionFor(view) : null}
            </div>
            <StateWord tone={view.tone} word={view.word} />
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The capability list folded to one line of chips while cards explain a
 * problem (Apple Home's status chips): name and state icon, plus the state
 * word when it is not Ready. The detail stays in the chip's title.
 */
export function CapabilityChips({ views }: { views: CapabilityView[] }) {
  // 44 px rows in one card (D1.4: pills wrapped unevenly and the phone's
  // tab bar cut them off): one column on a phone, two beside each other in
  // Status's single reading column (D1.5b). Ready stays quiet; any other
  // state shows its word on the right.
  return (
    <ul className="bg-card rounded-panel grid border px-4 sm:grid-cols-2 sm:gap-x-8">
      {views.map((view) => (
        <li
          key={view.key}
          title={view.detail ?? undefined}
          className="flex min-h-11 items-center gap-2 border-t text-sm first:border-t-0 sm:nth-2:border-t-0"
        >
          <StateIcon
            tone={view.tone}
            label={view.state === "ready" ? view.word : undefined}
          />
          <span className="min-w-0 flex-1 truncate font-medium">
            {view.label}
          </span>
          {view.state !== "ready" ? (
            <span className={cn("shrink-0 font-medium", toneText[view.tone])}>
              {view.word}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
