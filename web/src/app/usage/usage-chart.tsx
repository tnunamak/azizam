"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface UsageChartProps {
  rows: Array<{ label: string; total: number }>;
}

export default function UsageChart({ rows }: UsageChartProps) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--vf-border)" />
        <XAxis
          dataKey="label"
          tickLine={false}
          axisLine={false}
          tick={{ fill: "var(--subtle-foreground)", fontSize: 12 }}
        />
        <YAxis
          width={32}
          allowDecimals={false}
          tickLine={false}
          axisLine={false}
          tick={{ fill: "var(--subtle-foreground)", fontSize: 12 }}
        />
        <Tooltip
          cursor={{
            fill: "color-mix(in srgb, var(--vf-accent) 8%, transparent)",
          }}
          contentStyle={{
            background: "var(--vf-panel-raised)",
            border: "1px solid var(--vf-border-strong)",
            borderRadius: "8px",
            color: "var(--vf-text)",
            fontSize: "12px",
          }}
        />
        <Bar
          dataKey="total"
          fill="var(--chart-1)"
          name="Requests"
          isAnimationActive={false}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
