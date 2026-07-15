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
import type { BadgeTone } from "./badge";

export interface ChartPoint {
  x: number | string;
  y: number;
  label?: string;
}

export type ChartTone = Exclude<BadgeTone, "neutral"> | "muted";

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
    case "accent":
      return "var(--vf-accent)";
    default:
      return "var(--vf-text-subtle)";
  }
}

function compactNumber(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0";
  return new Intl.NumberFormat(undefined, {
    notation: Math.abs(value) >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: value < 10 ? 1 : 0,
  }).format(value);
}

function validSeries(series: ChartPoint[] | undefined): ChartPoint[] {
  return (series ?? []).filter(
    (point) =>
      point !== null &&
      point !== undefined &&
      Number.isFinite(point.y) &&
      (typeof point.x === "string" || Number.isFinite(point.x)),
  );
}

export interface TrendLineChartProps {
  series?: ChartPoint[];
  tone?: ChartTone;
  height?: number;
  showTooltip?: boolean;
  emptyLabel?: ReactNode;
  className?: string;
}

export function TrendLineChart({
  series,
  tone = "accent",
  height = 36,
  showTooltip = false,
  emptyLabel = null,
  className,
}: TrendLineChartProps) {
  const data = validSeries(series);
  if (data.length < 2) {
    return emptyLabel ? (
      <div className={cn("vf-chart-empty", className)}>{emptyLabel}</div>
    ) : null;
  }

  return (
    <div className={cn("vf-chart-sparkline", className)} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 2, bottom: 4, left: 2 }}>
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
    </div>
  );
}

export interface TrendBarChartProps {
  series?: ChartPoint[];
  tone?: ChartTone;
  height?: number;
  showAxes?: boolean;
  emptyLabel?: ReactNode;
  className?: string;
}

export function TrendBarChart({
  series,
  tone = "ok",
  height = 108,
  showAxes = false,
  emptyLabel = "No trend data yet",
  className,
}: TrendBarChartProps) {
  const data = validSeries(series);
  if (data.length < 2) {
    return <div className={cn("vf-chart-empty", className)}>{emptyLabel}</div>;
  }

  return (
    <div className={cn("vf-chart-bars", className)} style={{ height }}>
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
                tick={{ fill: "var(--vf-text-subtle)", fontSize: 10 }}
              />
              <YAxis hide />
            </>
          )}
          <Tooltip
            cursor={{ fill: "color-mix(in srgb, var(--vf-accent) 8%, transparent)" }}
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
    </div>
  );
}

export interface MetricTrendCardProps {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  series?: ChartPoint[];
  tone?: ChartTone;
  className?: string;
}

export function MetricTrendCard({
  label,
  value,
  detail,
  series,
  tone = "accent",
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
        />
      </div>
      <span className="vf-telemetry-value tabular-nums">{value}</span>
      {detail && <span className="vf-telemetry-detail">{detail}</span>}
    </div>
  );
}
