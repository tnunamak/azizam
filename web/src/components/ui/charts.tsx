"use client";

import type { ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { cn } from "@/lib/cn";
import type { BadgeTone } from "./legacy/badge";
import { StatusIcon, type StatusIconTone } from "./icons";

export interface ChartPoint {
  x: number | string;
  y: number;
  label?: string;
}

export type ChartTone = Exclude<BadgeTone, "neutral"> | "muted";

export type ChartAccessibility =
  | { mode: "summary"; label: string; summary: ReactNode }
  | { mode: "data-table"; label: string };

const DEFAULT_CHART_ACCESSIBILITY: ChartAccessibility = {
  mode: "data-table",
  label: "Trend data",
};

function toneVar(tone: ChartTone | undefined): string {
  switch (tone) {
    case "ok":
      return "var(--vf-ok)";
    case "warn":
      return "var(--vf-warn)";
    case "bad":
      return "var(--vf-bad)";
    case "info":
      return "var(--vf-info)";
    case "muted":
      return CHART_OTHER;
    default:
      // DESIGN "Colour budget": one series takes one hue, --chart-1.
      return CHART_SERIES[0];
  }
}

/** Series hues in order; a breakdown shows at most three plus Other. */
export const CHART_SERIES = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
] as const;
export const CHART_OTHER = "var(--chart-other)";
export const CHART_OTHER_KEY = "Other";

/**
 * Colours for a breakdown: the first three keys (callers order them by
 * size) take the series hues, and every later key folds into Other, so a
 * chart never shows more than four colours. Capability hues never fill bars.
 */
export function chartColors(keys: readonly string[]): {
  colors: Record<string, string>;
  /** The keys drawn in their own hue; the rest are Other. */
  shown: string[];
  folded: string[];
} {
  const named = keys.filter((key) => key !== CHART_OTHER_KEY);
  const shown = named.slice(0, CHART_SERIES.length);
  const folded = named.slice(CHART_SERIES.length);
  const colors: Record<string, string> = {};
  shown.forEach((key, index) => {
    colors[key] = CHART_SERIES[index];
  });
  for (const key of folded) colors[key] = CHART_OTHER;
  if (keys.includes(CHART_OTHER_KEY)) colors[CHART_OTHER_KEY] = CHART_OTHER;
  return { colors, shown, folded };
}

/** Axis labels: `text-badge` (12) in subtle ink. */
const AXIS_TICK = { className: "fill-subtle-foreground text-badge" };

/** A chart legend: an 8 px dot and the word for each series. */
export function ChartLegend({
  items,
  className,
}: {
  items: Array<{ label: string; color: string }>;
  className?: string;
}) {
  return (
    <ul
      aria-label="Chart legend"
      className={cn(
        "text-muted-foreground text-meta flex flex-wrap items-center gap-x-4 gap-y-1",
        className,
      )}
    >
      {items.map((item) => (
        <li key={item.label} className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            data-slot="chart-legend-dot"
            className="size-2 shrink-0 rounded-full"
            style={{ background: item.color }}
          />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

function compactNumber(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0";
  return new Intl.NumberFormat(undefined, {
    notation: Math.abs(value) >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: value < 10 ? 1 : 0,
  }).format(value);
}

export function validChartSeries(
  series: ChartPoint[] | undefined,
): ChartPoint[] {
  return (series ?? []).filter(
    (point) =>
      point !== null &&
      point !== undefined &&
      Number.isFinite(point.y) &&
      (typeof point.x === "string" || Number.isFinite(point.x)),
  );
}

/** A chart is a measured trend only with three finite observations. */
export function hasMinimumChartSamples(
  series: ChartPoint[] | undefined,
): boolean {
  return validChartSeries(series).length >= 3;
}

export function resolveChartAccessibility(
  accessibility: ChartAccessibility | undefined,
): ChartAccessibility {
  return accessibility ?? DEFAULT_CHART_ACCESSIBILITY;
}

function ChartAccessibleEquivalent({
  data,
  accessibility,
}: {
  data: ChartPoint[];
  accessibility: ChartAccessibility;
}) {
  if (accessibility.mode === "summary") {
    return (
      <div role="group" className="sr-only" aria-label={accessibility.label}>
        {accessibility.summary}
      </div>
    );
  }

  return (
    // The wrapper clips the table; a table alone ignores sr-only and widens the page.
    <div className="sr-only">
      <table>
        <caption>{accessibility.label}</caption>
        <thead>
          <tr>
            <th scope="col">Sample</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {data.map((point, index) => (
            <tr key={`${String(point.x)}-${index}`}>
              <th scope="row">{point.label ?? String(point.x)}</th>
              <td>{String(point.y)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ChartFrame({
  data,
  accessibility,
  className,
  height,
  children,
}: {
  data: ChartPoint[];
  accessibility: ChartAccessibility;
  className?: string;
  height: number;
  children: ReactNode;
}) {
  return (
    <div className={cn(className)} style={{ height }}>
      <div className="vf-chart-visual" aria-hidden>
        {children}
      </div>
      <ChartAccessibleEquivalent data={data} accessibility={accessibility} />
    </div>
  );
}

export interface TrendLineChartProps {
  series?: ChartPoint[];
  tone?: ChartTone;
  height?: number;
  showAxes?: boolean;
  showTooltip?: boolean;
  accessibility?: ChartAccessibility;
  emptyLabel?: ReactNode;
  className?: string;
}

export function TrendLineChart({
  series,
  tone = "accent",
  height = 36,
  showAxes = false,
  showTooltip = false,
  accessibility,
  emptyLabel = null,
  className,
}: TrendLineChartProps) {
  const data = validChartSeries(series);
  const chartAccessibility = resolveChartAccessibility(accessibility);
  if (data.length < 3) {
    return emptyLabel ? (
      <div className={cn("vf-chart-empty", className)}>{emptyLabel}</div>
    ) : null;
  }

  return (
    <ChartFrame
      data={data}
      accessibility={chartAccessibility}
      className={cn("vf-chart-sparkline", className)}
      height={height}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={data}
          margin={{ top: 4, right: 2, bottom: 4, left: 2 }}
        >
          {showAxes && (
            <>
              <CartesianGrid
                vertical={false}
                stroke="var(--vf-border)"
                strokeOpacity={0.55}
              />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                tick={AXIS_TICK}
              />
              <YAxis hide />
            </>
          )}
          {showTooltip && (
            <Tooltip
              cursor={false}
              contentStyle={{
                background: "var(--vf-panel-raised)",
                border: "1px solid var(--vf-border-strong)",
                borderRadius: "var(--vf-radius-sm)",
                color: "var(--vf-text)",
                fontSize: "var(--vf-text-xs)",
              }}
              labelFormatter={(label) => String(label)}
              formatter={(value) => compactNumber(value)}
            />
          )}
          <Line
            type="monotone"
            dataKey="y"
            stroke={toneVar(tone)}
            strokeWidth={1.7}
            dot={false}
            activeDot={showTooltip ? { r: 3 } : false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

export interface TrendBarChartProps {
  series?: ChartPoint[];
  tone?: ChartTone;
  height?: number;
  showAxes?: boolean;
  accessibility?: ChartAccessibility;
  emptyLabel?: ReactNode;
  className?: string;
}

export function TrendBarChart({
  series,
  tone = "accent",
  height = 108,
  showAxes = false,
  accessibility,
  emptyLabel = "No trend data yet",
  className,
}: TrendBarChartProps) {
  const data = validChartSeries(series);
  const chartAccessibility = resolveChartAccessibility(accessibility);
  if (data.length < 3) {
    return <div className={cn("vf-chart-empty", className)}>{emptyLabel}</div>;
  }

  return (
    <ChartFrame
      data={data}
      accessibility={chartAccessibility}
      className={cn("vf-chart-bars", className)}
      height={height}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 4, bottom: 2, left: 0 }}>
          {showAxes && (
            <>
              <CartesianGrid
                vertical={false}
                stroke="var(--vf-border)"
                strokeOpacity={0.55}
              />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                tick={AXIS_TICK}
              />
              <YAxis hide />
            </>
          )}
          <Tooltip
            cursor={{
              fill: "color-mix(in srgb, var(--vf-accent) 8%, transparent)",
            }}
            contentStyle={{
              background: "var(--vf-panel-raised)",
              border: "1px solid var(--vf-border-strong)",
              borderRadius: "var(--vf-radius-sm)",
              color: "var(--vf-text)",
              fontSize: "var(--vf-text-xs)",
            }}
            labelFormatter={(label) => String(label)}
            formatter={(value) => compactNumber(value)}
          />
          <Bar
            dataKey="y"
            fill={toneVar(tone)}
            radius={[3, 3, 0, 0]}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

export interface MetricTrendCardProps {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  series?: ChartPoint[];
  tone?: ChartTone;
  accessibility?: ChartAccessibility;
  className?: string;
}

export function MetricTrendCard({
  label,
  value,
  detail,
  series,
  tone = "accent",
  accessibility,
  className,
}: MetricTrendCardProps) {
  return (
    <div className={cn("vf-telemetry-card", className)}>
      <div className="vf-telemetry-card-head">
        <span className="vf-health-metric-label">{label}</span>
        <TrendLineChart
          series={series}
          tone={tone}
          height={28}
          className="vf-trend"
          accessibility={accessibility}
        />
      </div>
      <span className="vf-telemetry-value tabular-nums">{value}</span>
      {detail && <span className="vf-telemetry-detail">{detail}</span>}
    </div>
  );
}

export interface StatusChartLegendItem {
  label: string;
  tone: StatusIconTone;
}

export interface StatusChartProps {
  label: string;
  series: ChartPoint[];
  tone?: ChartTone;
  detail?: ReactNode;
  legend?: StatusChartLegendItem[];
  accessibility?: ChartAccessibility;
  emptyLabel?: ReactNode;
  className?: string;
}

export function StatusChart({
  label,
  series,
  tone = "accent",
  detail,
  legend,
  accessibility,
  emptyLabel = "History unavailable",
  className,
}: StatusChartProps) {
  return (
    <section className={cn("vf-status-chart", className)}>
      <div className="vf-status-chart-head">
        <div className="min-w-0">
          <h3 className="text-vf-heading text-vf-text">{label}</h3>
          {detail && <p className="text-vf-small text-vf-muted">{detail}</p>}
        </div>
      </div>
      <TrendLineChart
        series={series}
        tone={tone}
        height={160}
        showAxes
        accessibility={accessibility ?? { mode: "data-table", label }}
        emptyLabel={emptyLabel}
      />
      {hasMinimumChartSamples(series) && (
        <div className="vf-status-chart-axis" aria-hidden>
          <span>Earlier</span>
          <span>Now</span>
        </div>
      )}
      {legend && legend.length > 0 && (
        <ul className="vf-status-chart-legend" aria-label="Chart legend">
          {legend.map((item) => (
            <li key={item.label} className="vf-status-chart-legend-item">
              <StatusIcon tone={item.tone} className="size-3" />
              {item.label}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export interface StatusUptimeProps {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: StatusIconTone;
  statusLabel: string;
  className?: string;
}

export function StatusUptime({
  label,
  value,
  detail,
  tone = "neutral",
  statusLabel,
  className,
}: StatusUptimeProps) {
  return (
    <div className={cn("vf-status-uptime", className)}>
      <span className="vf-health-metric-label">{label}</span>
      <span
        className={cn(
          "vf-status-uptime-value",
          tone === "ok" && "text-vf-ok",
          tone === "warn" && "text-vf-warn",
          tone === "bad" && "text-vf-bad",
          tone === "info" && "text-vf-info",
        )}
      >
        {value}
      </span>
      <span className="text-vf-small text-vf-muted inline-flex items-center gap-1.5">
        <StatusIcon tone={tone} className="size-3" />
        {statusLabel}
      </span>
      {detail && <span className="text-vf-micro text-vf-muted">{detail}</span>}
    </div>
  );
}
