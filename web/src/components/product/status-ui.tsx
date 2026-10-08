"use client";

import type { ReactNode } from "react";
import {
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleMinus,
  CircleX,
  Clock3,
  Info,
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
  off: CircleMinus,
  unknown: CircleDashed,
};
const toneText: Record<StateTone, string> = {
  ok: "text-success",
  busy: "text-info",
  warn: "text-warning",
  bad: "text-danger",
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
}: {
  tone: StateTone;
  text: string;
  checkedAt?: number | null;
  children?: ReactNode;
  className?: string;
}) {
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
 * notice and error, on every surface.
 */
export function Callout({
  severity,
  title,
  meta,
  facts,
  children,
  actions,
  as: Tag = "section",
  className,
  headingLevel = 2,
  split = false,
}: {
  severity: CalloutSeverity;
  title: ReactNode;
  meta?: ReactNode;
  facts?: Array<[string, ReactNode]>;
  children?: ReactNode;
  actions?: ReactNode;
  as?: "section" | "article" | "div";
  className?: string;
  headingLevel?: 2 | 3;
  /** Wide cards: what happened and the action on the left, the evidence on the right. */
  split?: boolean;
}) {
  const Icon = severityIcon[severity];
  const Heading = headingLevel === 2 ? "h2" : "h3";
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
        <div className="min-w-0 flex-1 space-y-3">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <Heading className="text-base font-semibold">{title}</Heading>
            {meta ? (
              <span className="text-muted-foreground text-sm">{meta}</span>
            ) : null}
          </div>
          <div
            className={cn(
              "space-y-3",
              split &&
                Boolean(facts?.length) &&
                "lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)] lg:space-y-0 lg:gap-x-10",
            )}
          >
            {children || actions ? (
              <div className="space-y-3">
                {children ? (
                  <div className="space-y-1 text-sm">{children}</div>
                ) : null}
                {actions ? (
                  <div className="flex flex-wrap items-center gap-2">
                    {actions}
                  </div>
                ) : null}
              </div>
            ) : null}
            {facts?.length ? (
              <dl
                className={cn(
                  "grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[8rem_minmax(0,1fr)]",
                  split && "lg:order-last",
                )}
              >
                {facts.map(([label, value]) => (
                  <div key={label} className="contents">
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd className="min-w-0 max-sm:mb-1">{value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>
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
  return (
    <ul className={cn("grid gap-x-10", columns === 2 && "sm:grid-cols-2")}>
      {views.map((view) => {
        const meta = capabilityMeta(view.key);
        const Icon = meta.icon;
        return (
          <li
            key={view.key}
            className="grid min-h-11 grid-cols-[1.25rem_minmax(0,1fr)_auto] items-start gap-x-3 border-b py-2.5"
          >
            <Icon
              aria-hidden="true"
              className="text-muted-foreground mt-0.5 size-4"
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
