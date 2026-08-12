"use client";

import { useRef, useState } from "react";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

import {
  hasMinimumChartSamples,
  TrendLineChart,
  type ChartPoint,
} from "./charts";
import { useCopied } from "./copy-button";
import { CheckIcon, CopyIcon } from "./icons";

function mountedUiHref(href: string): string {
  if (href === "/admin") return "/ui/admin/";
  if (href.startsWith("/admin/api")) return href;
  const match = href.match(/^\/admin(\/[^?#]*)?([?#].*)?$/);
  if (!match) return href;
  const section = match[1] ?? "";
  const tail = match[2] ?? "";
  if (!section || section === "/") return `/ui/admin/${tail}`;
  return `/ui/admin${section.endsWith("/") ? section : `${section}/`}${tail}`;
}

// ─── Avatar ───────────────────────────────────────────────────────────────────

export type AvatarTone = "accent" | "info" | "ok" | "warn";

export interface AvatarProps {
  initials: string;
  tone?: AvatarTone;
  size?: "sm" | "md";
  className?: string;
}

/** Deterministic tone from a string so the same app always gets the same color. */
export function avatarToneFor(name: string): AvatarTone {
  const tones: AvatarTone[] = ["accent", "info", "ok", "warn"];
  let hash = 0;
  for (let i = 0; i < name.length; i++)
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return tones[hash % tones.length]!;
}

export function Avatar({
  initials,
  tone = "accent",
  size,
  className,
}: AvatarProps) {
  return (
    <span
      className={cn(
        "vf-avatar",
        size === "sm" && "vf-avatar-sm",
        tone === "info" && "vf-avatar-info",
        tone === "ok" && "vf-avatar-ok",
        tone === "warn" && "vf-avatar-warn",
        className,
      )}
      aria-hidden
    >
      {initials.slice(0, 2).toUpperCase()}
    </span>
  );
}

// ─── MetricStrip ──────────────────────────────────────────────────────────────

export interface MetricStripItem {
  label: string;
  value: string;
  detail?: string;
  tone?: HealthTone;
  series?: ChartPoint[];
}

export interface MetricStripProps {
  items: MetricStripItem[];
  className?: string;
}

export function MetricStrip({ items, className }: MetricStripProps) {
  return (
    <div className={cn("vf-metric-strip", className)}>
      {items.map((item, i) => (
        <div key={i} className="vf-metric-cell">
          <div className="flex items-center justify-between gap-2">
            <span className="vf-health-metric-label">{item.label}</span>
            {hasMinimumChartSamples(item.series) && (
              <span className="vf-desktop-inline">
                <TrendLineChart
                  series={item.series}
                  tone={
                    item.tone && item.tone !== "neutral" ? item.tone : "accent"
                  }
                  height={20}
                  className="vf-trend"
                />
              </span>
            )}
          </div>
          <span
            className={cn(
              "vf-health-metric-value tabular-nums",
              item.tone === "warn" && "text-vf-warn",
              item.tone === "bad" && "text-vf-bad",
              item.tone === "ok" && "text-vf-ok",
              item.tone === "info" && "text-vf-info",
            )}
          >
            {item.value}
          </span>
          {item.detail && (
            <span className="text-vf-micro text-vf-muted">{item.detail}</span>
          )}
        </div>
      ))}
    </div>
  );
}

// ─── StatusDotLabel ───────────────────────────────────────────────────────────

export interface StatusDotLabelProps {
  tone: HealthTone;
  label: string;
  pulse?: boolean;
  className?: string;
}

export type HealthTone = "ok" | "warn" | "bad" | "info" | "neutral";

function toneStatusDotClass(tone: HealthTone, pulse?: boolean): string {
  if (pulse && tone === "ok") return "vf-status-dot vf-status-dot-pulse";
  if (tone === "ok") return "vf-status-dot vf-status-dot-ok";
  if (tone === "warn") return "vf-status-dot vf-status-dot-warn";
  if (tone === "bad") return "vf-status-dot vf-status-dot-bad";
  if (tone === "info") return "vf-status-dot vf-status-dot-info";
  return "vf-status-dot vf-status-dot-neutral";
}

export function StatusDotLabel({
  tone,
  label,
  pulse,
  className,
}: StatusDotLabelProps) {
  return (
    <span
      className={cn(
        "vf-status-label inline-flex items-center gap-1.5",
        className,
      )}
    >
      <span className={toneStatusDotClass(tone, pulse)} aria-hidden />
      <span className="text-vf-small text-vf-text">{label}</span>
    </span>
  );
}

// ─── SystemHealthBand ─────────────────────────────────────────────────────────

export interface SystemHealthBandProps {
  tone: HealthTone;
  title: string;
  detail?: string;
  metrics: Array<{
    label: string;
    value: string;
    detail?: string;
    tone?: HealthTone;
  }>;
  action?: ReactNode;
  className?: string;
}

export function SystemHealthBand({
  tone,
  title,
  detail,
  metrics,
  action,
  className,
}: SystemHealthBandProps) {
  return (
    <div className={cn("vf-health-band", className)}>
      <div className="vf-health-band-main">
        <StatusDotLabel tone={tone} label={title} pulse={tone === "ok"} />
        {detail && (
          <p className="text-vf-micro text-vf-subtle mt-0.5">{detail}</p>
        )}
        {action && <div className="mt-2">{action}</div>}
      </div>
      {metrics.length > 0 && (
        <div className="vf-health-band-grid">
          {metrics.map((m, i) => (
            <div key={i} className="vf-health-metric">
              <span
                className={cn(
                  "vf-health-metric-value tabular-nums",
                  m.tone === "warn" && "text-vf-warn",
                  m.tone === "bad" && "text-vf-bad",
                  m.tone === "ok" && "text-vf-ok",
                  m.tone === "info" && "text-vf-info",
                )}
              >
                {m.value}
              </span>
              <span className="vf-health-metric-label">{m.label}</span>
              {m.detail && (
                <span className="text-vf-micro text-vf-subtle">{m.detail}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── IncidentBanner ───────────────────────────────────────────────────────────

export interface IncidentBannerProps {
  tone: "warn" | "bad" | "info";
  title: string;
  detail?: string;
  actions?: ReactNode;
  onDismiss?: () => void;
  className?: string;
}

export function IncidentBanner({
  tone,
  title,
  detail,
  actions,
  onDismiss,
  className,
}: IncidentBannerProps) {
  return (
    <div
      role={tone === "bad" ? "alert" : "status"}
      className={cn(
        "vf-incident-banner",
        tone === "warn" && "vf-incident-banner-warn",
        tone === "bad" && "vf-incident-banner-bad",
        tone === "info" && "vf-incident-banner-info",
        className,
      )}
    >
      <div className="min-w-0 flex-1">
        <p className="vf-incident-banner-title">{title}</p>
        {detail && <p className="vf-incident-banner-body">{detail}</p>}
      </div>
      {(actions || onDismiss) && (
        <div className="vf-incident-banner-actions">
          {actions}
          {onDismiss && (
            <button
              type="button"
              className="vf-button vf-button-ghost vf-button-sm"
              onClick={onDismiss}
              aria-label="Dismiss"
            >
              Dismiss
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ─── StatusAdvisory ───────────────────────────────────────────────────────────

export interface StatusAdvisoryItem {
  title: string;
  detail?: string;
  tone?: "warn" | "bad" | "info";
  href?: string;
  side?: ReactNode;
}

export interface StatusAdvisoryProps {
  /** Overall tone of the strip header (worst of the items). */
  tone: "warn" | "bad";
  /** Header line, e.g. "3 items need attention". */
  title: string;
  items: StatusAdvisoryItem[];
  /** Optional footer action, e.g. a "View all" link when items overflow. */
  footer?: ReactNode;
  className?: string;
}

export type CurrentAdvisoryItem = StatusAdvisoryItem;
export type CurrentAdvisoryProps = StatusAdvisoryProps;

/** Compact composed "needs attention" panel.
 *
 * One panel with a state header and dense single-line rows — replaces a stack
 * of full-height IncidentBanners so the dashboard content stays first-class. */
export function StatusAdvisory({
  tone,
  title,
  items,
  footer,
  className,
}: StatusAdvisoryProps) {
  return (
    <div
      role={tone === "bad" ? "alert" : "status"}
      className={cn(
        "vf-advisory",
        tone === "warn" && "vf-advisory-warn",
        tone === "bad" && "vf-advisory-bad",
        className,
      )}
    >
      <div className="vf-advisory-head">
        <span className="inline-flex min-w-0 items-center gap-2">
          <span
            className={toneStatusDotClass(tone === "bad" ? "bad" : "warn")}
            aria-hidden
          />
          <p className="vf-advisory-head-title truncate">{title}</p>
        </span>
      </div>
      {items.map((item, i) => {
        const inner = (
          <>
            <span
              className={cn(
                "mt-1 shrink-0",
                toneStatusDotClass(
                  item.tone === "bad"
                    ? "bad"
                    : item.tone === "info"
                      ? "info"
                      : "warn",
                ),
              )}
              aria-hidden
            />
            <span className="vf-advisory-row-body">
              <span className="vf-advisory-row-title">{item.title}</span>
              {item.detail && (
                <span className="vf-advisory-row-detail">{item.detail}</span>
              )}
            </span>
            {item.side && (
              <span className="vf-advisory-row-side">{item.side}</span>
            )}
          </>
        );
        return item.href ? (
          <a
            key={i}
            href={mountedUiHref(item.href)}
            className="vf-advisory-row"
          >
            {inner}
          </a>
        ) : (
          <div key={i} className="vf-advisory-row">
            {inner}
          </div>
        );
      })}
      {footer && <div className="vf-advisory-row">{footer}</div>}
    </div>
  );
}

/** Current-only alias. The component intentionally owns no incident history. */
export function CurrentAdvisory(props: CurrentAdvisoryProps) {
  if (props.items.length === 0) return null;
  return <StatusAdvisory {...props} />;
}

// ─── MiniSparkline ────────────────────────────────────────────────────────────

export interface MiniSparklinePoint {
  x: number;
  y: number;
}

interface MiniSparklineProps {
  series: MiniSparklinePoint[];
  className?: string;
}

export function MiniSparkline({ series, className }: MiniSparklineProps) {
  return (
    <TrendLineChart
      series={series as ChartPoint[]}
      tone="accent"
      height={28}
      className={className}
    />
  );
}

// ─── TelemetryCard ────────────────────────────────────────────────────────────

export interface TelemetryCardProps {
  label: string;
  value: string;
  detail?: string;
  tone?: HealthTone;
  series?: MiniSparklinePoint[];
  className?: string;
}

export function TelemetryCard({
  label,
  value,
  detail,
  tone,
  series,
  className,
}: TelemetryCardProps) {
  return (
    <div className={cn("vf-telemetry-card", className)}>
      <div className="vf-telemetry-card-head">
        <span className="vf-health-metric-label">{label}</span>
        {hasMinimumChartSamples(series) && (
          <TrendLineChart
            series={series}
            tone={tone && tone !== "neutral" ? tone : "accent"}
            height={28}
            className="vf-trend"
          />
        )}
      </div>
      <span
        className={cn(
          "vf-telemetry-value tabular-nums",
          tone === "warn" && "text-vf-warn",
          tone === "bad" && "text-vf-bad",
          tone === "ok" && "text-vf-ok",
          tone === "info" && "text-vf-info",
        )}
      >
        {value}
      </span>
      {detail && <span className="vf-telemetry-detail">{detail}</span>}
    </div>
  );
}

// ─── ServiceHealthRow ─────────────────────────────────────────────────────────

export interface ServiceHealthRowProps {
  name: string;
  description?: string;
  status: HealthTone;
  statusLabel: string;
  meta?: string[];
  metrics?: Array<{ label: string; value: string }>;
  actions?: ReactNode;
  href?: string;
  className?: string;
}

export function ServiceHealthRow({
  name,
  description,
  status,
  statusLabel,
  meta,
  metrics,
  actions,
  href,
  className,
}: ServiceHealthRowProps) {
  const Inner = (
    <>
      <div className="vf-service-row-main min-w-0">
        <span className="text-vf-body text-vf-text font-medium">{name}</span>
        {description && (
          <span className="text-vf-small text-vf-subtle block">
            {description}
          </span>
        )}
        {meta && meta.length > 0 && (
          <span className="text-vf-micro text-vf-muted mt-0.5 flex flex-wrap gap-2 font-mono">
            {meta.map((m, i) => (
              <span key={i}>{m}</span>
            ))}
          </span>
        )}
      </div>
      <div className="vf-service-row-status shrink-0">
        <StatusDotLabel
          tone={status}
          label={statusLabel}
          pulse={status === "ok"}
        />
      </div>
      {metrics && metrics.length > 0 && (
        <div className="vf-service-row-metrics flex shrink-0 gap-4">
          {metrics.map((m, i) => (
            <span key={i} className="flex flex-col items-end">
              <span className="text-vf-small text-vf-text tabular-nums">
                {m.value}
              </span>
              <span className="text-vf-micro text-vf-subtle font-mono uppercase">
                {m.label}
              </span>
            </span>
          ))}
        </div>
      )}
      {actions && (
        <div className="vf-service-row-actions shrink-0">{actions}</div>
      )}
    </>
  );

  const classes = cn("vf-service-row", className);
  if (href) {
    return (
      <a href={mountedUiHref(href)} className={classes}>
        {Inner}
      </a>
    );
  }
  return <div className={classes}>{Inner}</div>;
}

// ─── ActivityFeedRow ──────────────────────────────────────────────────────────

export interface ActivityFeedRowProps {
  title: string;
  detail?: string;
  actor?: string;
  time?: ReactNode;
  tone?: HealthTone;
  stat?: ReactNode;
  href?: string;
  className?: string;
}

export function ActivityFeedRow({
  title,
  detail,
  actor,
  time,
  tone,
  stat,
  href,
  className,
}: ActivityFeedRowProps) {
  const Inner = (
    <>
      <span
        className={cn(
          "vf-activity-row-mark mt-1 shrink-0",
          tone === "bad" && "vf-status-dot vf-status-dot-bad",
          tone === "warn" && "vf-status-dot vf-status-dot-warn",
          tone === "ok" && "vf-status-dot vf-status-dot-ok",
          tone === "info" && "vf-status-dot vf-status-dot-info",
          !tone && "vf-status-dot vf-status-dot-neutral",
        )}
        aria-hidden
      />
      <span className="vf-activity-row-meta min-w-0 flex-1">
        <span className="text-vf-small text-vf-text block">{title}</span>
        {detail && (
          <span className="text-vf-micro text-vf-subtle block truncate">
            {detail}
          </span>
        )}
        {actor && (
          <span className="text-vf-micro text-vf-muted block">{actor}</span>
        )}
      </span>
      <span className="vf-activity-row-stat flex shrink-0 flex-col items-end gap-0.5">
        {time && <span className="text-vf-micro text-vf-muted">{time}</span>}
        {stat && <span className="text-vf-micro text-vf-subtle">{stat}</span>}
      </span>
    </>
  );

  const classes = cn("vf-activity-row", className);
  if (href) {
    return (
      <a href={mountedUiHref(href)} className={classes}>
        {Inner}
      </a>
    );
  }
  return <div className={classes}>{Inner}</div>;
}

// ─── EmptyOperationalState ────────────────────────────────────────────────────

export interface EmptyOperationalStateProps {
  title: string;
  detail?: string;
  action?: ReactNode;
  tone?: "neutral" | "info" | "warn" | "bad";
  className?: string;
}

export function EmptyOperationalState({
  title,
  detail,
  action,
  tone = "neutral",
  className,
}: EmptyOperationalStateProps) {
  return (
    <div className={cn("vf-empty-operational", className)}>
      <p
        className={cn(
          "vf-empty-operational-title",
          tone === "warn" && "text-vf-warn",
          tone === "bad" && "text-vf-bad",
          tone === "info" && "text-vf-info",
        )}
      >
        {title}
      </p>
      {detail && <p className="vf-empty-operational-body">{detail}</p>}
      {action && <div className="vf-empty-operational-actions">{action}</div>}
    </div>
  );
}

// ─── SectionActionHeader ──────────────────────────────────────────────────────

export interface SectionActionHeaderProps {
  title: string;
  as?: "h1" | "h2" | "h3";
  kicker?: string;
  count?: string | number;
  description?: string;
  action?: ReactNode;
  className?: string;
}

export function SectionActionHeader({
  title,
  as: Heading = "h2",
  kicker,
  count,
  description,
  action,
  className,
}: SectionActionHeaderProps) {
  return (
    <div
      className={cn(
        "vf-section-head flex items-end justify-between gap-3",
        className,
      )}
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        {kicker && (
          <p className="text-vf-micro text-vf-subtle font-mono uppercase">
            {kicker}
          </p>
        )}
        <Heading className="text-vf-title text-vf-text flex items-baseline gap-2 font-semibold">
          {title}
          {count !== undefined && (
            <span className="text-vf-small text-vf-muted font-normal tabular-nums">
              {count}
            </span>
          )}
        </Heading>
        {description && (
          <p className="text-vf-small text-vf-muted">{description}</p>
        )}
      </div>
      {action && (
        <div className="flex shrink-0 items-center gap-2">{action}</div>
      )}
    </div>
  );
}

// ─── QuickStartChecklist ──────────────────────────────────────────────────────

export interface QuickStartChecklistItem {
  id: string;
  label: string;
  detail?: string;
  complete: boolean;
  action?: ReactNode;
}

export interface QuickStartChecklistProps {
  items: QuickStartChecklistItem[];
  className?: string;
}

export function QuickStartChecklist({
  items,
  className,
}: QuickStartChecklistProps) {
  const allComplete = items.every((i) => i.complete);
  if (allComplete) return null;

  const doneCount = items.filter((i) => i.complete).length;
  const pct = Math.round((doneCount / items.length) * 100);

  return (
    <div
      className={cn("vf-quickstart-strip", className)}
      role="list"
      aria-label="Setup progress"
    >
      <div className="vf-quickstart-strip-steps">
        {items.map((item, index) => (
          <div
            key={item.id}
            role="listitem"
            className={cn(
              "vf-quickstart-strip-step",
              item.complete && "vf-quickstart-strip-step-done",
            )}
          >
            <span className="vf-quickstart-strip-num" aria-hidden>
              {index + 1}
            </span>
            <span className="vf-quickstart-strip-label">{item.label}</span>
            {!item.complete && item.action && (
              <div className="vf-quickstart-strip-action">{item.action}</div>
            )}
          </div>
        ))}
      </div>
      <div className="vf-quickstart-strip-bar" aria-hidden>
        <div
          className="vf-quickstart-strip-bar-fill"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ─── CopyField ────────────────────────────────────────────────────────────────

export interface CopyFieldProps {
  label?: string;
  value: string;
  copyValue?: string;
  description?: string;
  monospace?: boolean;
  className?: string;
}

export function CopyField({
  label,
  value,
  copyValue,
  description,
  monospace,
  className,
}: CopyFieldProps) {
  const { copied, copy } = useCopied();
  const toCopy = copyValue ?? value;

  return (
    <div className={cn("vf-copy-field", className)}>
      {label && (
        <span className="vf-copy-field-label text-vf-micro text-vf-subtle font-mono uppercase">
          {label}
        </span>
      )}
      <div className="flex min-w-0 items-center gap-2">
        <span
          className={cn(
            "vf-copy-field-value text-vf-small text-vf-text min-w-0 truncate",
            monospace && "font-mono",
          )}
          title={value}
        >
          {value}
        </span>
        <button
          type="button"
          className="vf-copy-field-button vf-button vf-button-ghost vf-button-sm w-16 shrink-0 justify-center"
          onClick={() => void copy(toCopy)}
          aria-label={copied ? "Copied" : "Copy"}
        >
          {copied ? (
            <CheckIcon className="text-vf-ok size-4" />
          ) : (
            <CopyIcon className="size-4" />
          )}
          <span className="sr-only" aria-live="polite">
            {copied ? "Copied to clipboard" : ""}
          </span>
        </button>
      </div>
      {description && (
        <p className="text-vf-micro text-vf-muted">{description}</p>
      )}
    </div>
  );
}

// ─── KeyRevealBlock ───────────────────────────────────────────────────────────

export interface KeyRevealBlockProps {
  secret?: string;
  fingerprint?: string;
  label?: string;
  canCopy?: boolean;
  onCopy?: () => void;
  className?: string;
}

export function KeyRevealBlock({
  secret,
  fingerprint,
  label,
  canCopy = true,
  onCopy,
  className,
}: KeyRevealBlockProps) {
  const { copied, copy } = useCopied();

  const handleCopy = async () => {
    if (!secret) return;
    await copy(secret);
    onCopy?.();
  };

  return (
    <div className={cn("vf-key-reveal", className)}>
      {label && (
        <p className="text-vf-micro text-vf-subtle mb-1 font-mono uppercase">
          {label}
        </p>
      )}
      <div className="vf-key-reveal-value relative">
        {secret ? (
          <pre className="vf-key-block text-vf-small pr-12 font-mono break-all whitespace-pre-wrap">
            {secret}
          </pre>
        ) : fingerprint ? (
          <span className="vf-key-fingerprint text-vf-small text-vf-muted font-mono">
            {fingerprint}
          </span>
        ) : null}
        {secret && canCopy && (
          <div className="vf-key-reveal-actions absolute top-2 right-2">
            <button
              type="button"
              className="vf-button vf-button-ghost vf-button-sm px-2"
              onClick={() => void handleCopy()}
              aria-label={copied ? "Copied" : "Copy key"}
            >
              {copied ? (
                <CheckIcon className="text-vf-ok size-4" />
              ) : (
                <CopyIcon className="size-4" />
              )}
              <span className="sr-only" aria-live="polite">
                {copied ? "Copied to clipboard" : ""}
              </span>
            </button>
          </div>
        )}
      </div>
      {secret && (
        <p className="text-vf-micro text-vf-muted mt-1">
          Store this key — it will not be shown again.
        </p>
      )}
      {!secret && fingerprint && (
        <p className="text-vf-micro text-vf-muted mt-1">
          Full key not recoverable. Rotate to get a new key.
        </p>
      )}
    </div>
  );
}

// ─── FilterToolbar ────────────────────────────────────────────────────────────

export interface FilterToolbarProps {
  search?: ReactNode;
  filters?: ReactNode;
  sort?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

export function FilterToolbar({
  search,
  filters,
  sort,
  actions,
  className,
}: FilterToolbarProps) {
  return (
    <div className={cn("vf-filter-toolbar", className)}>
      {search && <div className="min-w-0 flex-1">{search}</div>}
      {filters && (
        <div className="vf-filter-group flex flex-wrap items-center gap-2">
          {filters}
        </div>
      )}
      {sort && <div className="shrink-0">{sort}</div>}
      {actions && (
        <div className="vf-filter-group flex shrink-0 items-center gap-2">
          {actions}
        </div>
      )}
    </div>
  );
}

// ─── AppAccessRow ─────────────────────────────────────────────────────────────

export interface AppAccessRowProps {
  name: string;
  avatar?: ReactNode;
  owner?: string;
  profile?: string;
  keyCount?: number;
  lastUsedLabel?: string;
  status?: HealthTone;
  statusLabel?: string;
  /** Inline metric chips rendered after the identity block, before actions. */
  metrics?: ReactNode;
  actions?: ReactNode;
  href?: string;
  className?: string;
}

/** Header row that mirrors AppAccessRow's flex layout for column label alignment.
 *
 * Uses the same flex skeleton as AppAccessRow: avatar(34px) · identity(flex-1) ·
 * metrics(gap-4 per cell) · badges · usage · actions. The badge/usage/actions
 * slots are fixed at their typical rendered widths so column labels stay
 * approximately aligned across rows whose content varies in width. */
export interface AppAccessRowHeaderProps {
  /** Labels for the metric columns — one per metric cell in data rows. */
  metricLabels?: string[];
  className?: string;
}

const COL_LABEL =
  "text-[10px] text-[var(--vf-text-subtle)] font-mono uppercase tracking-wide leading-none";

export function AppAccessRowHeader({
  metricLabels,
  className,
}: AppAccessRowHeaderProps) {
  return (
    <div
      aria-hidden
      className={cn(
        "hidden items-center gap-[10px] px-3 py-[7px] md:flex",
        "border-b border-[var(--vf-border)] bg-[color-mix(in_srgb,var(--vf-surface-2)_40%,transparent)]",
        className,
      )}
    >
      {/* avatar — 34px, same as vf-access-row-avatar on desktop */}
      <div className="w-[34px] shrink-0" />
      {/* identity — flex-1 */}
      <span className={cn("min-w-0 flex-1", COL_LABEL)}>App name</span>
      {/* metrics — hidden md:flex gap-4, each cell matches "flex flex-col items-end" */}
      {metricLabels && metricLabels.length > 0 && (
        <div className="hidden shrink-0 items-center gap-4 md:flex">
          {metricLabels.map((label) => (
            <span
              key={label}
              className={cn("flex w-[3.75rem] flex-col items-end", COL_LABEL)}
            >
              {label}
            </span>
          ))}
        </div>
      )}
      {/* badges — vf-access-row-badges: shrink-0 flex gap-2; widths sized to typical content */}
      <div className="flex shrink-0 items-center gap-2">
        {/* status dot + label typically ~80px */}
        <span className={cn("w-[5rem]", COL_LABEL)}>Status</span>
        {/* key count "N keys" typically ~42px */}
        <span className={cn("w-[3rem]", COL_LABEL)}>Keys</span>
      </div>
      {/* usage — vf-access-row-usage: shrink-0, whitespace-nowrap; ~72px typical */}
      <span className={cn("w-[4.5rem] shrink-0", COL_LABEL)}>Last used</span>
      {/* actions — shrink-0; overflow menu button ~32px + kind badge ~50px */}
      <div className="w-[5.5rem] shrink-0" />
    </div>
  );
}

export function AppAccessRow({
  name,
  avatar,
  owner,
  profile,
  keyCount,
  lastUsedLabel,
  status,
  statusLabel,
  metrics,
  actions,
  href,
  className,
}: AppAccessRowProps) {
  const Inner = (
    <>
      {avatar && <div className="vf-access-row-avatar shrink-0">{avatar}</div>}
      <div className="vf-access-row-identity min-w-0 flex-1">
        <span
          className="text-vf-body text-vf-text block truncate font-medium"
          title={name}
        >
          {name}
        </span>
        {(owner || profile) && (
          <span
            className="text-vf-micro text-vf-subtle mt-0.5 flex min-w-0 gap-2 font-mono"
            title={[owner, profile].filter(Boolean).join(" · ")}
          >
            {owner && <span className="shrink truncate">{owner}</span>}
            {profile && <span className="shrink truncate">{profile}</span>}
          </span>
        )}
      </div>
      {metrics && (
        <div className="vf-access-row-metrics hidden shrink-0 items-center gap-4 md:flex">
          {metrics}
        </div>
      )}
      <div className="vf-access-row-badges flex shrink-0 flex-wrap items-center gap-2">
        {status && statusLabel && (
          <StatusDotLabel tone={status} label={statusLabel} />
        )}
        {keyCount !== undefined && (
          <span className="text-vf-micro text-vf-muted font-mono">
            {keyCount} {keyCount === 1 ? "key" : "keys"}
          </span>
        )}
        {lastUsedLabel && (
          <span className="vf-access-row-usage-inline text-vf-micro text-vf-muted font-mono">
            {lastUsedLabel}
          </span>
        )}
      </div>
      <div className="vf-access-row-usage shrink-0">
        {lastUsedLabel && (
          <span className="text-vf-micro text-vf-muted">{lastUsedLabel}</span>
        )}
      </div>
      {actions && (
        <div className="vf-access-row-actions shrink-0">{actions}</div>
      )}
    </>
  );

  const classes = cn("vf-access-row", className);
  if (href) {
    return (
      <a href={mountedUiHref(href)} className={classes}>
        {Inner}
      </a>
    );
  }
  return <div className={classes}>{Inner}</div>;
}

// ─── RightContextPanel ────────────────────────────────────────────────────────

export interface RightContextPanelSection {
  title?: string;
  content: ReactNode;
}

export interface RightContextPanelProps {
  title: string;
  subtitle?: string;
  status?: ReactNode;
  actions?: ReactNode;
  sections: RightContextPanelSection[];
  className?: string;
}

export function RightContextPanel({
  title,
  subtitle,
  status,
  actions,
  sections,
  className,
}: RightContextPanelProps) {
  return (
    <aside className={cn("vf-context-panel", className)}>
      <div className="vf-context-panel-head">
        <div className="min-w-0 flex-1">
          <h2 className="text-vf-body text-vf-text truncate font-semibold">
            {title}
          </h2>
          {subtitle && (
            <p className="text-vf-micro text-vf-subtle truncate">{subtitle}</p>
          )}
        </div>
        {status && <div className="shrink-0">{status}</div>}
      </div>
      {actions && <div className="vf-context-panel-actions">{actions}</div>}
      {sections.map((section, i) => (
        <div key={i} className="vf-context-panel-section">
          {section.title && (
            <p className="text-vf-micro text-vf-subtle mb-2 font-mono uppercase">
              {section.title}
            </p>
          )}
          {section.content}
        </div>
      ))}
    </aside>
  );
}

// ─── MobileTopRail ────────────────────────────────────────────────────────────

export interface MobileTopRailProps {
  brand?: ReactNode;
  account?: ReactNode;
  className?: string;
}

export function MobileTopRail({
  brand,
  account,
  className,
}: MobileTopRailProps) {
  return (
    <header className={cn("vf-mobile-top-rail", className)}>
      <div className="flex min-w-0 items-center gap-2">{brand}</div>
      {account && (
        <div className="flex shrink-0 items-center gap-2">{account}</div>
      )}
    </header>
  );
}

// ─── MobileActionDock ─────────────────────────────────────────────────────────

export interface MobileActionDockProps {
  children: ReactNode;
  className?: string;
}

export function MobileActionDock({
  children,
  className,
}: MobileActionDockProps) {
  return (
    <div className={cn("vf-mobile-action-dock", className)}>{children}</div>
  );
}

// ─── CommandSearch ────────────────────────────────────────────────────────────

export interface CommandSearchItem {
  id: string;
  label: string;
  description?: string;
  href?: string;
  onSelect?: () => void;
  group?: string;
}

export interface CommandSearchProps {
  items?: CommandSearchItem[];
  placeholder?: string;
  className?: string;
  inputClassName?: string;
}

export function CommandSearch({
  items,
  placeholder = "Search…",
  className,
  inputClassName,
}: CommandSearchProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const filtered = items
    ? items.filter(
        (item) =>
          !query ||
          item.label.toLowerCase().includes(query.toLowerCase()) ||
          item.description?.toLowerCase().includes(query.toLowerCase()),
      )
    : [];

  const open = query.length > 0 && items && filtered.length > 0;

  if (!items || items.length === 0) {
    return (
      <div
        className={cn(
          "vf-command-search vf-command-search-disabled",
          className,
        )}
        aria-disabled="true"
      >
        <input
          className={cn("vf-command-search-input", inputClassName)}
          placeholder="Search unavailable"
          disabled
          aria-label="Search unavailable"
        />
      </div>
    );
  }

  return (
    <div className={cn("vf-command-search relative", className)}>
      <input
        ref={inputRef}
        role="combobox"
        className={cn("vf-command-search-input", inputClassName)}
        placeholder={placeholder}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setQuery("");
            inputRef.current?.blur();
          }
        }}
        aria-label="Search"
        aria-autocomplete="list"
        aria-expanded={!!open}
        aria-haspopup="listbox"
        aria-controls="vf-command-menu"
      />
      {open && (
        <ul
          id="vf-command-menu"
          role="listbox"
          className="vf-command-menu"
          aria-label="Search results"
        >
          {filtered.map((item) => (
            <li key={item.id} role="option" aria-selected="false">
              {item.href ? (
                <a
                  href={mountedUiHref(item.href)}
                  className="vf-command-item"
                  onClick={() => setQuery("")}
                >
                  <span className="text-vf-small text-vf-text">
                    {item.label}
                  </span>
                  {item.description && (
                    <span className="vf-command-item-meta text-vf-micro text-vf-subtle">
                      {item.description}
                    </span>
                  )}
                </a>
              ) : (
                <button
                  type="button"
                  className="vf-command-item w-full text-left"
                  onClick={() => {
                    item.onSelect?.();
                    setQuery("");
                  }}
                >
                  <span className="text-vf-small text-vf-text">
                    {item.label}
                  </span>
                  {item.description && (
                    <span className="vf-command-item-meta text-vf-micro text-vf-subtle">
                      {item.description}
                    </span>
                  )}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
