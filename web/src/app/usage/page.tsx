"use client";

import type { ReactNode } from "react";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Activity,
  CheckCircle2,
  ChevronRight,
  Clock3,
  DollarSign,
  FileText,
  Image,
  Layers,
  Mic,
  type LucideIcon,
} from "lucide-react";
import { useViewer } from "@/components/shell/session";
import { cn } from "@/lib/cn";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CapabilityIcon,
  CodeBlock,
  EmptyState,
  ErrorState,
  Money,
  PageHeader,
  RelativeTime,
  type Capability,
} from "@/components/product";

type UsageCapability = Capability | "audio" | "embeddings" | "rerank" | "other";

interface UsageDay {
  date: string;
  total: number;
  byCapability: Record<UsageCapability, number>;
}

interface UsageModel {
  model: string;
  capability: UsageCapability;
  requests: number;
  durationMs: number | null;
  costUsd: number | null;
}

interface UsageReceipt {
  requestId: string;
  createdAt: number | string | null;
  capability: UsageCapability;
  model: string | null;
  status: string | null;
  durationMs: number | null;
  costUsd: number | null;
  summary: string;
  raw: unknown;
}

interface UsagePerson {
  id: string;
  label: string;
}

interface UsageSummary {
  requests: number;
  mediaCount: number;
  remoteSpendUsd: number | null;
  spendMeasured: boolean;
  byDay: UsageDay[];
  byModel: UsageModel[];
  receipts: UsageReceipt[];
  people: UsagePerson[];
  retentionDays: number;
  coverageStart: string | null;
  coverageNote: string | null;
  spendMeasuredRequests: number | null;
  spendTotalRequests: number | null;
  truncated: boolean;
}

const capabilities: UsageCapability[] = [
  "chat",
  "image",
  "video",
  "audio",
  "voice",
  "transcribe",
  "decide",
  "embeddings",
  "rerank",
  "other",
];

const capabilityLabels: Record<UsageCapability, string> = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  audio: "Audio",
  voice: "Voice",
  transcribe: "Transcribe",
  decide: "Decide",
  embed: "Embed",
  embeddings: "Embeddings",
  rerank: "Rerank",
  other: "Other",
};

const chartColors: Record<UsageCapability, string> = {
  chat: "var(--capability-chat)",
  image: "var(--capability-image)",
  video: "var(--capability-video)",
  audio: "var(--capability-voice)",
  voice: "var(--capability-voice)",
  transcribe: "var(--capability-transcribe)",
  decide: "var(--capability-decide)",
  embed: "var(--capability-embed)",
  embeddings: "var(--capability-embed)",
  rerank: "var(--capability-decide)",
  other: "var(--vf-border-strong)",
};

export default function UsagePage() {
  const viewer = useViewer();
  const [person, setPerson] = useState("self");
  const [selected, setSelected] = useState<UsageReceipt | null>(null);
  const { data, isPending, isFetching, isError, refetch } = useQuery({
    queryKey: ["usage", "summary", person],
    queryFn: () => fetchUsageSummary(person),
    retry: false,
  });
  const hasUsage = Boolean(
    data && (data.requests > 0 || data.mediaCount > 0 || data.receipts.length),
  );

  return (
    <div className="space-y-8">
      <PageHeader
        title="Usage"
        answer="How much am I using, and what does it cost?"
        actions={
          ["owner", "superadmin"].includes(viewer.role) ? (
            <PersonFilter
              value={person}
              people={data?.people ?? []}
              disabled={isPending}
              onChange={setPerson}
            />
          ) : undefined
        }
      />
      {isPending ? (
        <UsageSkeleton />
      ) : isError || !data ? (
        <ErrorState
          message="Usage could not be loaded. Try again."
          retrying={isFetching}
          onRetry={() => void refetch()}
        />
      ) : !hasUsage ? (
        <EmptyState
          icon={Activity}
          title="No usage this month"
          description="Requests, generated media and receipts appear here after you use a gateway app. Receipts are retained for 14 days."
          action={
            <Button asChild variant="outline">
              <a href="/ui/apps/">Connect an app</a>
            </Button>
          }
        />
      ) : (
        <>
          <SummaryRow summary={data} />
          <p className="text-muted-foreground text-sm">
            Receipts are retained for {data.retentionDays} days
            {data.coverageStart
              ? `, covering usage since ${formatDate(data.coverageStart)}`
              : ""}
            .{data.coverageNote ? ` ${formatSentence(data.coverageNote)}` : ""}
            {data.truncated
              ? " This view is limited to the most recent stored receipts."
              : ""}
          </p>
          <DailyChart days={data.byDay} />
          <ModelTable rows={data.byModel} />
          <ReceiptsList
            receipts={data.receipts}
            loading={isFetching}
            onSelect={setSelected}
          />
        </>
      )}
      <ReceiptSheet
        receipt={selected}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      />
    </div>
  );
}

function PersonFilter({
  value,
  people,
  disabled,
  onChange,
}: {
  value: string;
  people: UsagePerson[];
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="space-y-2">
      <label
        className="text-muted-foreground block text-xs"
        htmlFor="usage-person"
      >
        Person
      </label>
      <Select value={value} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger id="usage-person" className="w-[180px] max-w-full">
          <SelectValue placeholder="Your usage" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="self">Your usage</SelectItem>
          <SelectItem value="all">Everyone</SelectItem>
          {people.map((item) => (
            <SelectItem key={item.id} value={item.id}>
              {item.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function SummaryRow({ summary }: { summary: UsageSummary }) {
  const spark = capabilitySpark(summary.byDay);
  return (
    <section className="grid gap-4 md:grid-cols-3" aria-label="This month">
      <MetricCard
        icon={Activity}
        label="Requests"
        value={formatNumber(summary.requests)}
        detail="This month"
      />
      <MetricCard
        icon={DollarSign}
        label="Remote spend"
        value={
          summary.spendMeasured ? (
            <Money amount={summary.remoteSpendUsd} />
          ) : (
            "Not measured yet"
          )
        }
        detail={
          summary.spendMeasured
            ? spendCoverageDetail(summary)
            : "No recorded provider costs"
        }
      />
      <MetricCard
        icon={Image}
        label="Media made"
        value={formatNumber(summary.mediaCount)}
        detail="Images, video and audio"
      />
      <Card className="border-0! bg-transparent! shadow-none! md:col-span-3">
        <CardHeader>
          <CardTitle>Capability sparklines</CardTitle>
          <CardDescription>Requests by capability this month</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {spark.length ? (
            spark.map((item) => (
              <div
                key={item.capability}
                className="bg-muted/30 min-w-0 rounded-lg p-3"
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <CapabilityLabel capability={item.capability} />
                  <span className="text-sm tabular-nums">
                    {formatNumber(item.total)}
                  </span>
                </div>
                <MiniSparkline
                  values={item.values}
                  capability={item.capability}
                />
              </div>
            ))
          ) : (
            <p className="text-muted-foreground text-sm">
              Capability trends appear after daily totals are measured.
            </p>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

function MetricCard({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: LucideIcon;
  label: string;
  value: ReactNode;
  detail: string;
}) {
  return (
    <Card className="border-0! bg-transparent! shadow-none!">
      <CardHeader className="flex-row items-center gap-3 space-y-0">
        <span className="bg-muted inline-flex size-10 shrink-0 items-center justify-center rounded-lg">
          <Icon aria-hidden="true" className="text-muted-foreground size-5" />
        </span>
        <div>
          <CardDescription>{label}</CardDescription>
          <CardTitle className="mt-1 text-2xl tabular-nums">{value}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="text-muted-foreground text-sm">
        {detail}
      </CardContent>
    </Card>
  );
}

function DailyChart({ days }: { days: UsageDay[] }) {
  const rows = days.map((day) => ({
    label: shortDate(day.date),
    ...day.byCapability,
  }));
  const active = capabilities.filter((capability) =>
    days.some((day) => (day.byCapability[capability] ?? 0) > 0),
  );
  return (
    <Card className="border-0! bg-transparent! shadow-none!">
      <CardHeader>
        <CardTitle>Requests by day</CardTitle>
        <CardDescription>Stacked by capability</CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length < 1 ? (
          <div className="text-muted-foreground flex min-h-40 items-center justify-center rounded-lg border text-sm">
            Daily history appears after requests are measured.
          </div>
        ) : (
          <>
            <div className="h-72 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={rows}
                  margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
                >
                  <CartesianGrid vertical={false} stroke="var(--vf-border)" />
                  <XAxis
                    dataKey="label"
                    tickLine={false}
                    axisLine={false}
                    tick={{ fill: "var(--vf-text-subtle)", fontSize: 11 }}
                  />
                  <YAxis
                    width={32}
                    allowDecimals={false}
                    tickLine={false}
                    axisLine={false}
                    tick={{ fill: "var(--vf-text-subtle)", fontSize: 11 }}
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
                  {active.map((capability) => (
                    <Bar
                      key={capability}
                      dataKey={capability}
                      stackId="capability"
                      fill={chartColors[capability]}
                      name={capabilityLabels[capability]}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
            <table className="sr-only">
              <caption>Requests by day and capability</caption>
              <tbody>
                {days.map((day) => (
                  <tr key={day.date}>
                    <th scope="row">{day.date}</th>
                    <td>{day.total}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-4 flex flex-wrap gap-3 text-xs">
              {active.map((capability) => (
                <span
                  key={capability}
                  className="inline-flex items-center gap-2"
                >
                  <span
                    className="size-2 rounded-full"
                    style={{ background: chartColors[capability] }}
                  />
                  {capabilityLabels[capability]}
                </span>
              ))}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ModelTable({ rows }: { rows: UsageModel[] }) {
  return (
    <Card className="border-0! bg-transparent! shadow-none!">
      <CardHeader>
        <CardTitle>Models</CardTitle>
        <CardDescription>Requests, duration and measured cost</CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto" tabIndex={0}>
        {rows.length ? (
          <table className="w-full min-w-[560px] text-sm">
            <thead className="text-muted-foreground border-b text-left">
              <tr>
                <th scope="col" className="py-3 pr-4 font-medium">
                  Model
                </th>
                <th scope="col" className="py-3 pr-4 font-medium">
                  Capability
                </th>
                <th scope="col" className="py-3 pr-4 text-right font-medium">
                  Requests
                </th>
                <th scope="col" className="py-3 pr-4 text-right font-medium">
                  Typical time
                </th>
                <th scope="col" className="py-3 text-right font-medium">
                  Cost
                </th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((row) => (
                <tr key={`${row.model}-${row.capability}`}>
                  <td className="max-w-[260px] truncate py-3 pr-4 font-medium">
                    {row.model}
                  </td>
                  <td className="py-3 pr-4">
                    <CapabilityLabel capability={row.capability} />
                  </td>
                  <td className="py-3 pr-4 text-right tabular-nums">
                    {formatNumber(row.requests)}
                  </td>
                  <td className="py-3 pr-4 text-right tabular-nums">
                    {formatDuration(row.durationMs)}
                  </td>
                  <td className="py-3 text-right">
                    <Money amount={row.costUsd} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-muted-foreground text-sm">
            Model totals appear after requests are measured.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function ReceiptsList({
  receipts,
  onSelect,
  loading,
}: {
  receipts: UsageReceipt[];
  onSelect: (receipt: UsageReceipt) => void;
  loading: boolean;
}) {
  return (
    <Card className="border-0! bg-transparent! shadow-none!">
      <CardHeader>
        <CardTitle>Receipts</CardTitle>
        <CardDescription>
          Receipts are retained for 14 days. Open one for the human summary and
          raw JSON.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loading && !receipts.length ? (
          <div aria-label="Loading receipts" className="space-y-3">
            {[0, 1, 2].map((item) => (
              <Skeleton key={item} className="h-16 w-full" />
            ))}
          </div>
        ) : receipts.length ? (
          <div className="divide-y">
            {receipts.map((receipt) => (
              <button
                type="button"
                key={receipt.requestId}
                aria-label={`Receipt details for ${receipt.model ?? "unknown model"}`}
                onClick={() => onSelect(receipt)}
                className="hover:bg-muted/40 focus-visible:outline-ring flex w-full items-center gap-3 py-4 text-left focus-visible:outline-2"
              >
                <CapabilityLabel capability={receipt.capability} compact />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">
                    {receipt.summary}
                  </p>
                  <p className="text-muted-foreground mt-1 text-xs">
                    {receipt.createdAt ? (
                      <RelativeTime ts={receipt.createdAt} />
                    ) : (
                      "Time not measured"
                    )}
                    {" · "}
                    {formatDuration(receipt.durationMs)}
                  </p>
                </div>
                <StatusBadge status={receipt.status} />
                <span className="hidden text-sm sm:block">
                  <Money amount={receipt.costUsd} />
                </span>
                <ChevronRight
                  aria-hidden="true"
                  className="text-muted-foreground size-4 shrink-0"
                />
              </button>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={FileText}
            title="No receipts yet"
            description="Receipts appear here after a gateway app makes a request."
          />
        )}
      </CardContent>
    </Card>
  );
}

function ReceiptSheet({
  receipt,
  onOpenChange,
}: {
  receipt: UsageReceipt | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Sheet open={receipt !== null} onOpenChange={onOpenChange}>
      <SheetContent className="overflow-y-auto px-4 pb-4">
        {receipt && (
          <>
            <SheetHeader>
              <SheetTitle>Request receipt</SheetTitle>
              <SheetDescription>{receipt.summary}</SheetDescription>
            </SheetHeader>
            <div className="mt-8 space-y-6">
              <div className="rounded-lg border p-4">
                <div className="mb-3 flex items-center gap-3">
                  <CapabilityLabel capability={receipt.capability} />
                  <StatusBadge status={receipt.status} />
                </div>
                <p className="text-sm">{receipt.summary}</p>
              </div>
              <dl className="grid gap-4 text-sm sm:grid-cols-2">
                <Detail
                  label="Model"
                  value={receipt.model ?? "Not measured yet"}
                />
                <Detail
                  label="Duration"
                  value={formatDuration(receipt.durationMs)}
                />
                <Detail
                  label="Cost"
                  value={<Money amount={receipt.costUsd} />}
                />
                <Detail
                  label="When"
                  value={
                    receipt.createdAt ? (
                      <RelativeTime ts={receipt.createdAt} />
                    ) : (
                      "Not measured yet"
                    )
                  }
                />
                <Detail label="Request ID" value={receipt.requestId} wide />
              </dl>
              <details>
                <summary className="cursor-pointer text-sm font-medium">
                  Technical details
                </summary>
                <div className="mt-4">
                  <CodeBlock
                    language="json"
                    code={JSON.stringify(receipt.raw, null, 2)}
                  />
                </div>
              </details>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

function Detail({
  label,
  value,
  wide = false,
}: {
  label: string;
  value: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={cn(wide && "sm:col-span-2")}>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words">{value}</dd>
    </div>
  );
}

function CapabilityLabel({
  capability,
  compact = false,
}: {
  capability: UsageCapability;
  compact?: boolean;
}) {
  if (capability === "audio") {
    return (
      <span className="inline-flex items-center gap-2">
        <span className="text-capability-voice bg-capability-voice/10 inline-flex size-8 items-center justify-center rounded-md">
          <Mic aria-hidden="true" className="size-4" />
        </span>
        {!compact && <span>{capabilityLabels.audio}</span>}
      </span>
    );
  }
  if (
    capability === "embeddings" ||
    capability === "rerank" ||
    capability === "other"
  ) {
    return (
      <span className="inline-flex items-center gap-2">
        <span className="bg-muted inline-flex size-8 items-center justify-center rounded-md">
          <Layers aria-hidden="true" className="text-muted-foreground size-4" />
        </span>
        {!compact && <span>{capabilityLabels[capability]}</span>}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-2">
      <CapabilityIcon capability={capability} />
      {!compact && <span>{capabilityLabels[capability]}</span>}
    </span>
  );
}

function StatusBadge({ status }: { status: string | null }) {
  const ok = status === "success" || status === "succeeded" || status === "ok";
  return (
    <Badge variant={ok ? "secondary" : "outline"}>
      {ok ? <CheckCircle2 aria-hidden="true" /> : <Clock3 aria-hidden="true" />}
      {status ?? "Unknown"}
    </Badge>
  );
}

function MiniSparkline({
  values,
  capability,
}: {
  values: number[];
  capability: UsageCapability;
}) {
  const max = Math.max(1, ...values);
  return (
    <div className="flex h-10 items-end gap-1" aria-hidden>
      {values.map((value, index) => (
        <span
          key={index}
          className="w-full rounded-t-sm"
          style={{
            height: value === 0 ? 0 : `${Math.max(3, (value / max) * 40)}px`,
            background: chartColors[capability],
          }}
        />
      ))}
    </div>
  );
}

function UsageSkeleton() {
  return (
    <div
      role="status"
      aria-label="Loading usage"
      aria-busy="true"
      className="space-y-6"
    >
      <div className="grid gap-4 md:grid-cols-3">
        {[0, 1, 2].map((item) => (
          <Skeleton key={item} className="h-36 w-full" />
        ))}
      </div>
      <Skeleton className="h-80 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

async function fetchUsageSummary(person: string): Promise<UsageSummary> {
  const params = new URLSearchParams({ period: "month" });
  if (person !== "self") params.set("person", person);
  const response = await fetch(`/me/api/usage/summary?${params}`, {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.ok) return normalizeSummary(await response.json());
  throw new Error("Usage summary could not be loaded.");
}

function normalizeSummary(raw: unknown): UsageSummary {
  const data = object(raw);
  const receipts = array(data.receipts).map(normalizeReceipt);
  const byDay = array(data.by_day ?? data.days ?? data.series).map(
    normalizeDay,
  );
  const byModel = array(data.by_model ?? data.models).map(normalizeModel);
  const remoteSpendRaw =
    data.remote_spend ?? data.remote_spend_usd ?? data.remoteSpendUsd;
  const hasRemoteSpendFlag =
    data.remote_spend_available != null ||
    data.spend_measured != null ||
    data.spendMeasured != null;
  const remoteSpendAvailable = hasRemoteSpendFlag
    ? Boolean(
        data.remote_spend_available ??
        data.spend_measured ??
        data.spendMeasured,
      )
    : remoteSpendRaw != null;
  const remoteSpendUsd = remoteSpendAvailable
    ? nullableNumber(remoteSpendRaw)
    : null;
  return {
    requests: number(data.requests ?? data.request_count, receipts.length),
    mediaCount: number(data.media_count ?? data.mediaCount, 0),
    remoteSpendUsd,
    spendMeasured: remoteSpendAvailable || remoteSpendUsd !== null,
    byDay,
    byModel,
    receipts,
    people: array(data.people)
      .map((item) => {
        const person = object(item);
        return {
          id: string(person.id, ""),
          label: string(
            person.label ?? person.name ?? person.email,
            "Unknown person",
          ),
        };
      })
      .filter((item) => item.id),
    retentionDays: number(data.retention_days, 14),
    coverageStart: nullableString(data.coverage_start ?? data.coverageStart),
    coverageNote: nullableString(data.coverage_note ?? data.coverageNote),
    spendMeasuredRequests: nullableNumber(
      data.spend_measured_requests ?? data.spendMeasuredRequests,
    ),
    spendTotalRequests: nullableNumber(
      data.spend_total_requests ?? data.spendTotalRequests,
    ),
    truncated: Boolean(data.truncated),
  };
}

function normalizeReceipt(raw: unknown): UsageReceipt {
  const data = object(raw);
  const metadata = object(data.metadata);
  const capability = normalizeCapability(
    data.capability ?? data.intent ?? metadata.capability ?? data.protocol,
  );
  const model = nullableString(
    data.resolved_model ?? data.requested_model ?? data.model ?? metadata.model,
  );
  const requestId = string(data.request_id ?? data.id, "receipt");
  const status = nullableString(data.status);
  const durationMs = nullableNumber(
    data.duration_ms ?? data.latency_ms ?? data.latencyMs,
  );
  const costUsd = nullableNumber(
    data.remote_spend ??
      data.cost_usd ??
      data.remote_cost_usd ??
      metadata.cost_usd,
  );
  return {
    requestId,
    createdAt: nullableDate(
      data.created_at ?? data.createdAt ?? data.accepted_at_ms,
    ),
    capability,
    model,
    status,
    durationMs,
    costUsd,
    summary: humanSummary({ capability, model, status, durationMs }),
    raw,
  };
}

function normalizeDay(raw: unknown): UsageDay {
  const data = object(raw);
  const byCapability = normalizeCapabilityCounts(
    data.capabilities ?? data.by_capability ?? data,
  );
  return {
    date: string(data.date ?? data.day, ""),
    total: number(data.total ?? data.requests, sumCounts(byCapability)),
    byCapability,
  };
}

function normalizeModel(raw: unknown): UsageModel {
  const data = object(raw);
  return {
    model: string(data.model ?? data.name, "Unknown model"),
    capability: normalizeCapability(data.capability ?? data.intent),
    requests: number(data.requests ?? data.count, 0),
    durationMs: nullableNumber(
      data.duration_ms ?? data.latency_ms ?? data.typical_ms,
    ),
    costUsd: nullableNumber(
      data.remote_spend ?? data.cost_usd ?? data.remote_cost_usd,
    ),
  };
}

function normalizeCapabilityCounts(
  raw: unknown,
): Record<UsageCapability, number> {
  const data = object(raw);
  return capabilities.reduce(
    (acc, capability) => ({
      ...acc,
      [capability]: number(data[capability], 0),
    }),
    {} as Record<UsageCapability, number>,
  );
}

function capabilitySpark(days: UsageDay[]) {
  return capabilities
    .map((capability) => ({
      capability,
      values: days.map((day) => day.byCapability[capability] ?? 0),
      total: days.reduce(
        (sum, day) => sum + (day.byCapability[capability] ?? 0),
        0,
      ),
    }))
    .filter((item) => item.total > 0)
    .slice(0, 8);
}

function normalizeCapability(value: unknown): UsageCapability {
  const text = String(value ?? "").toLowerCase();
  if (text.includes("image")) return "image";
  if (text.includes("video")) return "video";
  if (text.includes("rerank")) return "rerank";
  if (text.includes("embedding") || text.includes("embed")) return "embeddings";
  if (text.includes("speech") || text.includes("audio")) return "audio";
  if (text.includes("voice")) return "voice";
  if (text.includes("transcrib")) return "transcribe";
  if (text.includes("decision") || text.includes("decide")) return "decide";
  if (
    text.includes("chat") ||
    text.includes("completion") ||
    text.includes("openai")
  )
    return "chat";
  return "other";
}

function humanSummary({
  capability,
  model,
  status,
  durationMs,
}: {
  capability: UsageCapability;
  model: string | null;
  status: string | null;
  durationMs: number | null;
}) {
  const parts = [capabilityLabels[capability], model ?? "unknown model"];
  if (status) parts.push(status);
  if (durationMs != null) parts.push(formatDuration(durationMs));
  return parts.join(" · ");
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function string(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function number(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function nullableNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function nullableDate(value: unknown): number | string | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) return value;
  return null;
}

function sumCounts(counts: Record<UsageCapability, number>) {
  return Object.values(counts).reduce((sum, value) => sum + value, 0);
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("en-US").format(value);
}

function formatDuration(value: number | null) {
  if (value == null) return "Not measured yet";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)} s`;
}

function spendCoverageDetail(summary: UsageSummary) {
  if (
    summary.spendMeasuredRequests != null &&
    summary.spendTotalRequests != null &&
    summary.spendTotalRequests > 0
  ) {
    return `Measured for ${formatNumber(summary.spendMeasuredRequests)} of ${formatNumber(summary.spendTotalRequests)} requests`;
  }
  return "Measured from receipts";
}

function shortDate(value: string) {
  const date = parseDate(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function formatDate(value: string) {
  const date = parseDate(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function parseDate(value: string) {
  return new Date(value.includes("T") ? value : `${value}T00:00:00Z`);
}

function formatSentence(value: string) {
  return /[.!?]$/.test(value) ? value : `${value}.`;
}
