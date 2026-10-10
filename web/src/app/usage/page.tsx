"use client";

import type { ReactNode } from "react";
import { useEffect, useState } from "react";
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
  FileText,
  Layers,
  Mic,
} from "lucide-react";
import { useViewer } from "@/components/shell/session";
import { cn } from "@/lib/cn";
import { errorCopy } from "@/lib/error-copy";
import { receiptSelectionKeys } from "@/lib/receipt-selection";
import { memberTerms, taskNames } from "@/lib/terms";
import { technicalModelDetails } from "@/lib/model-name";
import { useModelName } from "@/lib/use-model-name";
import { sourceGroups, type UsageSource } from "./source-options";
import { normalizeCapability, type UsageCapability } from "./usage-capability";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
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
  SelectGroup,
  SelectItem,
  SelectLabel,
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
import { Stat, StatStrip } from "@/components/product/stat-strip";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { Callout, StateBadge, StatusLine } from "@/components/product/status-ui";
import {
  CapabilityIcon,
  CodeBlock,
  EmptyState,
  Money,
  PageHeader,
  RelativeTime,
} from "@/components/product";


interface UsageDay {
  date: string;
  total: number;
  byCapability: Record<UsageCapability, number>;
  /** Sum of the recorded capability counts when it exceeds `total`, else null. */
  countsExceedTotal: number | null;
}

interface UsageModel {
  model: string;
  capability: UsageCapability;
  requests: number;
  costUsd: number | null;
  /** Requests known to have cost money; null when the gateway did not say. */
  paidRequests: number | null;
  /** Paid requests whose cost was not recorded; null when not reported. */
  unknownPaidRequests: number | null;
}

interface UsageReceipt {
  selectionKey: string;
  requestId: string | null;
  createdAt: number | string | null;
  capability: UsageCapability;
  model: string | null;
  requestedModel: string | null;
  servedModel: string | null;
  fallbackModel: string | null;
  status: string | null;
  durationMs: number | null;
  costUsd: number | null;
  summary: string;
  sourceLabel: string | null;
  raw: unknown;
}

interface UsagePerson {
  id: string;
  label: string;
}

interface UsageSummary {
  requests: number;
  failedRequests: number | null;
  mediaCount: number;
  remoteSpendUsd: number | null;
  spendMeasured: boolean;
  byDay: UsageDay[];
  byModel: UsageModel[];
  receipts: UsageReceipt[];
  people: UsagePerson[];
  sources: UsageSource[];
  retentionDays: number | null;
  coverageStart: string | null;
  coverageNote: string | null;
  spendMeasuredRequests: number | null;
  spendTotalRequests: number | null;
  paidRequests: number | null;
  unknownPaidRequests: number | null;
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

// The names Home and Try use for the same things.
const capabilityLabels: Record<UsageCapability, string> = {
  chat: taskNames.chat,
  image: taskNames.image,
  video: taskNames.video,
  audio: taskNames.audio,
  voice: taskNames.speech,
  transcribe: taskNames.transcribe,
  decide: taskNames.decisions,
  embed: taskNames.embeddings,
  embeddings: taskNames.embeddings,
  rerank: taskNames.reranking,
  other: "Other",
};

// The noun in a receipt's title: "Image request", as Home and Activity say it.
const requestNouns: Record<UsageCapability, string> = {
  chat: "Chat",
  image: "Image",
  video: "Video",
  audio: "Audio",
  voice: "Voice",
  transcribe: "Transcription",
  decide: "Decision",
  embed: "Embedding",
  embeddings: "Embedding",
  rerank: "Rerank",
  other: "Other",
};


export default function UsagePage() {
  const viewer = useViewer();
  const [person, setPerson] = useState("default");
  const [source, setSource] = useState("all");
  const [showAllModels, setShowAllModels] = useState(false);
  const [showReceipts, setShowReceipts] = useState(false);
  const owner = ["owner", "superadmin"].includes(viewer.role);
  const effectivePerson =
    person === "default" ? (owner ? "all" : "self") : person;
  const [selectedReceiptKey, setSelectedReceiptKey] = useState<string | null>(null);
  const { data, isPending, isFetching, isError, refetch } = useQuery({
    queryKey: ["usage", "summary", effectivePerson, source, owner],
    queryFn: () =>
      fetchUsageSummary(owner ? effectivePerson : "default", source),
    retry: false,
  });
  // Display names: the catalog, else a capability's recommended name.
  const nameModel = useModelName();
  const selected = data?.receipts.find((receipt) => receipt.selectionKey === selectedReceiptKey) ?? null;
  useEffect(() => {
    if (selectedReceiptKey !== null && selected === null) setSelectedReceiptKey(null);
  }, [selectedReceiptKey, selected]);
  const sources = sourceGroups(data?.sources ?? []);
  const hasUsage = Boolean(
    data && (data.requests > 0 || data.mediaCount > 0 || data.receipts.length),
  );
  const filtersActive =
    effectivePerson !== (owner ? "all" : "self") || source !== "all";

  return (
    <div className="space-y-8">
      <PageHeader
        title="Usage"
        subtitle="Current month usage"
        primaryAction={
          data && hasUsage ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowReceipts((value) => !value)}
              aria-expanded={showReceipts}
              aria-controls="usage-receipts"
            >
              {showReceipts ? "Hide recent requests" : "View recent requests"}
            </Button>
          ) : undefined
        }
        actions={
          owner ? (
            <div className="flex flex-wrap items-center gap-2">
              <PersonFilter
                value={effectivePerson}
                people={data?.people ?? []}
                disabled={isPending}
                onChange={(value) => {
                  setPerson(value);
                  setSource("all");
                }}
              />
              <div className="space-y-2">
                <label
                  htmlFor="usage-source"
                  className="sr-only"
                >
                  Source
                </label>
                <Select
                  value={source}
                  onValueChange={setSource}
                  disabled={isPending}
                >
                  <SelectTrigger
                    id="usage-source"
                    size="sm"
                    className="w-52 max-w-full"
                  >
                    <SelectValue placeholder="All sources" />
                  </SelectTrigger>
                  <SelectContent
                    position="popper"
                    align="start"
                    collisionPadding={8}
                    className="max-w-[calc(100vw-1rem)]"
                  >
                    <SelectItem value="all">All sources</SelectItem>
                    {sources.active.map((item) => (
                      <SelectItem key={item.id} value={item.id}>
                        {item.label}
                      </SelectItem>
                    ))}
                    {sources.inactive.length ? (
                      <SelectGroup>
                        <SelectLabel>Turned off or no requests</SelectLabel>
                        {sources.inactive.map((item) => (
                          <SelectItem key={item.id} value={item.id}>
                            {item.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    ) : null}
                  </SelectContent>
                </Select>
              </div>
            </div>
          ) : undefined
        }
      >
        {isPending || isFetching || isError || !data ? (
          <StatusLine
            compact
            tone={isFetching || isPending ? "busy" : data ? "warn" : "unknown"}
            text={
              isPending
                ? "Busy · Loading usage"
                : isFetching
                  ? data ? "Busy · Refreshing usage" : "Busy · Loading usage"
                  : data
                    ? "Degraded · Usage refresh failed"
                    : "Unknown · Usage unavailable"
            }
          />
        ) : null}
      </PageHeader>
      {isError && data ? (
        <Callout
          severity="attention"
          title="Usage could not be refreshed"
          headerAction={
            <Button variant="outline" disabled={isFetching} onClick={() => void refetch()}>
              {isFetching ? "Retrying…" : "Retry usage"}
            </Button>
          }
        >
          <p>Showing the last loaded usage. New requests and spend may be missing.</p>
        </Callout>
      ) : null}
      {isPending ? (
        <UsageSkeleton />
      ) : !data ? (
        <Callout
          as="div"
          severity="critical"
          title="Usage could not be loaded"
          headerAction={
            <Button variant="outline" disabled={isFetching} onClick={() => void refetch()}>
              {isFetching ? "Retrying…" : "Retry usage"}
            </Button>
          }
        >
          <p>Try again to load this month’s usage.</p>
        </Callout>
      ) : !hasUsage ? (
        <EmptyState
          icon={Activity}
          title={
            filtersActive
              ? "No matching requests"
              : "No requests in this period"
          }
          description={
            data.retentionDays
              ? `Requests, generated media and ${data.retentionDays}-day receipts appear here after a gateway app makes a request.`
              : "Requests, generated media and receipts appear here after a gateway app makes a request."
          }
          action={
            filtersActive ? (
              <Button
                variant="outline"
                onClick={() => {
                  setPerson(owner ? "all" : "self");
                  setSource("all");
                }}
              >
                Clear filters
              </Button>
            ) : (
              <Button asChild variant="outline">
                <a href="/ui/apps/">Connect an app</a>
              </Button>
            )
          }
        />
      ) : (
        <>
          <SummaryRow summary={data} />
          {showReceipts ? (
            <ReceiptsList
              receipts={data.receipts}
              nameModel={nameModel}
              loading={isFetching}
              onSelect={(receipt) => setSelectedReceiptKey(receipt.selectionKey)}
            />
          ) : null}
          <DailyChart days={data.byDay} models={data.byModel} requests={data.requests} />
          <ModelTable
            rows={data.byModel}
            nameModel={nameModel}
            showAll={showAllModels}
            onToggle={() => setShowAllModels((value) => !value)}
          />
        </>
      )}
      <ReceiptSheet
        receipt={selected}
        onOpenChange={(open) => {
          if (!open) setSelectedReceiptKey(null);
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
        className="sr-only"
        htmlFor="usage-person"
      >
        Person
      </label>
      <Select value={value} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger
          id="usage-person"
          size="sm"
          className="w-44 max-w-full"
        >
          <SelectValue placeholder="Your usage" />
        </SelectTrigger>
        <SelectContent>
          {[
            { id: "all", label: "Everyone" },
            { id: "self", label: "Your apps" },
            ...people.filter((item) => !["all", "self"].includes(item.id)),
          ].map((item) => (
            <SelectItem key={item.id} value={item.id}>
              {item.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/** "count · share", or "Unknown" when the gateway cannot say how requests ended. */
function failedFigure(summary: UsageSummary): string {
  if (summary.failedRequests === null) return "Unknown";
  const share =
    summary.requests > 0
      ? `${Math.round((summary.failedRequests / summary.requests) * 1000) / 10}%`
      : "Unknown";
  return `${formatNumber(summary.failedRequests)} · ${share}`;
}

function SummaryRow({ summary }: { summary: UsageSummary }) {
  const noPaidRequests =
    summary.paidRequests === 0 && summary.unknownPaidRequests === 0 &&
    summary.remoteSpendUsd === 0;
  const hasCoverage =
    summary.spendMeasuredRequests !== null && summary.spendTotalRequests !== null;
  const partial =
    (summary.unknownPaidRequests ?? 0) > 0 ||
    (summary.spendMeasuredRequests !== null &&
      summary.spendTotalRequests !== null &&
      summary.spendMeasuredRequests < summary.spendTotalRequests);
  const spendLabel =
    !noPaidRequests && summary.remoteSpendUsd !== null
      ? partial
        ? "Remote spend · partial"
        : hasCoverage
          ? "Remote spend"
          : "Remote spend · coverage unknown"
      : "Remote spend";
  return (
    <StatStrip label="This month">
      <Stat
        label="Requests"
        value={formatNumber(summary.requests)}
        note="This month"
      />
      <Stat
        label={spendLabel}
        value={
          noPaidRequests ? (
            "No paid requests"
          ) : summary.remoteSpendUsd === null ? (
            "Cost not recorded"
          ) : (
            <Money amount={summary.remoteSpendUsd} summary />
          )
        }
        note={<><span>{spendCoverageDetail(summary)}</span><CoverageNote summary={summary} /></>}
      />
      <Stat
        label="Media made"
        value={formatNumber(summary.mediaCount)}
        note="Images, video and audio"
      />
      <Stat
        label="Failed"
        value={failedFigure(summary)}
        note={
          summary.failedRequests ? (
            <StateBadge state="degraded" word="Failed" />
          ) : null
        }
      />
    </StatStrip>
  );
}

function CoverageNote({ summary }: { summary: UsageSummary }) {
  const facts = [
    summary.retentionDays ? `Receipts are kept for ${summary.retentionDays} days.` : "",
    summary.coverageStart ? `Coverage starts ${formatDate(summary.coverageStart)}.` : "",
    summary.coverageNote ? formatSentence(summary.coverageNote) : "",
    summary.truncated ? "This view is limited to the most recent stored receipts." : "",
  ].filter(Boolean);
  if (!facts.length) return null;
  return (
    <details className="text-muted-foreground text-meta">
      <summary className="flex min-h-8 w-fit cursor-pointer items-center max-sm:min-h-11">
        Receipt coverage
      </summary>
      <ul className="mt-1 list-inside list-disc">{facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
    </details>
  );
}

function DailyChart({ days, models, requests }: { days: UsageDay[]; models: UsageModel[]; requests: number }) {
  const rows = days.map((day) => ({
    label: shortDate(day.date),
    total: day.total,
  }));
  const tasks = capabilities.map((capability) => {
    const modelsForTask = models.filter((model) => model.capability === capability);
    const measured = modelsForTask.filter((model) => model.costUsd !== null);
    const incomplete = modelsForTask.some((model) =>
      ["partial", "unavailable"].includes(modelCostKind(model)),
    );
    return {
      capability,
      requests: modelsForTask.reduce((sum, model) => sum + model.requests, 0),
      costUsd: measured.reduce((sum, model) => sum + (model.costUsd ?? 0), 0),
      unavailable: measured.length === 0 && incomplete,
      incomplete,
    };
  }).filter((task) => task.requests > 0).sort((a, b) => b.requests - a.requests);
  const active = capabilities.filter((capability) =>
    days.some((day) => (day.byCapability[capability] ?? 0) > 0),
  );
  const overfullDays = days.filter((day) => day.countsExceedTotal !== null).length;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Requests by day</CardTitle>
        <CardDescription>
          Total requests
          {overfullDays > 0 &&
            `. On ${overfullDays} ${overfullDays === 1 ? "day" : "days"} the ${memberTerms.task} counts add to more than the recorded total; the bars show the recorded totals and the daily table keeps the recorded task counts.`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length < 1 ? (
          <EmptyState icon={Activity} title="Daily request history will appear here." />
        ) : (
          <>
            <div className="h-72 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={rows}
                  margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
                >
                  <CartesianGrid vertical={false} stroke="var(--border)" />
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
                      fill: "color-mix(in srgb, var(--primary) 8%, transparent)",
                    }}
                    contentStyle={{
                      background: "var(--popover)",
                      border: "1px solid var(--input)",
                      borderRadius: "8px",
                      color: "var(--foreground)",
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
            </div>
            {/* The wrapper clips the table: a table ignores sr-only's 1px size and
                would otherwise widen the page. */}
            <div className="sr-only">
              <table>
                <caption>Requests by day and {memberTerms.task}</caption>
                <thead>
                  <tr>
                    <th scope="col">Date</th>
                    {active.map((capability) => (
                      <th key={capability} scope="col">
                        {capabilityLabels[capability]}
                      </th>
                    ))}
                    <th scope="col">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {days.map((day) => (
                    <tr key={day.date}>
                      <th scope="row">{day.date}</th>
                      {active.map((capability) => (
                        <td key={capability}>{day.byCapability[capability] ?? 0}</td>
                      ))}
                      <td>
                        {day.total}
                        {day.countsExceedTotal !== null &&
                          ` (${memberTerms.task} counts add to ${day.countsExceedTotal})`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        <div className="mt-4 space-y-3">
          <h2 className="text-heading font-semibold">By task</h2>
          {tasks.length ? (
            <>
              <p className="text-muted-foreground text-meta">Recorded model totals · share of this month’s requests</p>
              <Table dense aria-label="By task">
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col" className="max-sm:px-2">Task</TableHead>
                    <TableHead scope="col" numeric className="max-sm:px-2">Requests</TableHead>
                    <TableHead scope="col" numeric className="max-sm:px-2">Share</TableHead>
                    <TableHead scope="col" numeric className="max-sm:px-2">Spend</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tasks.map((task) => (
                    <TableRow key={task.capability} className="max-sm:h-(--row-phone-2)">
                      <TableCell rowName className="max-sm:px-2 max-sm:whitespace-normal">{capabilityLabels[task.capability]}</TableCell>
                      <TableCell numeric className="max-sm:px-2">{formatNumber(task.requests)}</TableCell>
                      <TableCell numeric className="max-sm:px-2">{requests > 0 ? `${Math.round(task.requests / requests * 1000) / 10}%` : "Unknown"}</TableCell>
                      <TableCell numeric className="max-sm:px-2">
                        {task.unavailable ? "Cost not recorded" : <Money amount={task.costUsd} summary />}
                        {!task.unavailable && task.incomplete ? <span className="text-muted-foreground ml-1 text-meta">partial</span> : null}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          ) : <p className="text-muted-foreground text-meta">Task totals are not recorded.</p>}
        </div>
      </CardContent>
    </Card>
  );
}

function ModelTable({
  rows,
  nameModel,
  showAll,
  onToggle,
}: {
  rows: UsageModel[];
  nameModel: (id: string) => string;
  showAll: boolean;
  onToggle: () => void;
}) {
  const sortedRows = [...rows].sort((a, b) =>
    b.requests - a.requests || (a.model < b.model ? -1 : a.model > b.model ? 1 : 0),
  );
  const visibleRows = showAll ? sortedRows : sortedRows.slice(0, 5);
  const kinds = new Set(visibleRows.map(modelCostKind));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Models</CardTitle>
        <CardDescription>Top models by requests</CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length ? (
          <div className="space-y-4">
            <Table dense aria-label="Models">
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">
                    Task and model
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Requests
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Spend
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleRows.map((row) => {
                  const name = row.model ? nameModel(row.model) : "";
                  return (
                    <TableRow key={`${row.model}-${row.capability}`}>
                      {/* max-w-0 lets a long name truncate instead of widening the page. */}
                      <TableCell className="w-full max-w-0 text-body font-medium">
                        <div className="flex min-w-0 items-center gap-2">
                          <span aria-hidden="true" className="bg-chart-1 size-2 shrink-0 rounded-full" />
                          <span>{capabilityLabels[row.capability]}</span>
                          {name && name !== capabilityLabels[row.capability] && (
                            <span className="text-muted-foreground min-w-0 truncate text-meta" title={name}>
                              · {name}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell numeric>
                        {formatNumber(row.requests)}
                      </TableCell>
                      <TableCell numeric>
                        <ModelCost row={row} />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            {kinds.has("local") && (
              <p className="text-muted-foreground text-meta">
                Local: no provider charge.
              </p>
            )}
            {rows.length > 5 ? (
              <Button variant="ghost" onClick={onToggle}>
                {showAll ? "Show top 5" : `View all ${rows.length} models`}
              </Button>
            ) : null}
          </div>
        ) : (
          <EmptyState icon={Layers} title="Model totals will appear here after your first request." />
        )}
      </CardContent>
    </Card>
  );
}

type ModelCostKind = "amount" | "partial" | "local" | "unavailable";

/**
 * What the Spend cell of a model row says. A recorded charge shows the amount.
 * No charge shows "Local" only when the gateway counted no paid requests;
 * anything else without an amount says Cost not recorded, never a blank or a guess.
 */
function modelCostKind(row: UsageModel): ModelCostKind {
  const unknownPaid = (row.unknownPaidRequests ?? 0) > 0;
  if (row.costUsd != null && row.costUsd > 0) return unknownPaid ? "partial" : "amount";
  if (unknownPaid) return "unavailable";
  if (row.paidRequests === 0) return "local";
  if (row.costUsd != null) return "amount";
  return "unavailable";
}

function ModelCost({ row }: { row: UsageModel }) {
  const kind = modelCostKind(row);
  if (kind === "local") return <span className="text-muted-foreground">Local</span>;
  if (kind === "unavailable")
    return <span className="text-muted-foreground">Cost not recorded</span>;
  return (
    <>
      <Money amount={row.costUsd} summary />
      {kind === "partial" ? (
        <span className="text-muted-foreground ml-1 text-meta">partial</span>
      ) : null}
    </>
  );
}

function ReceiptsList({
  receipts,
  nameModel,
  onSelect,
  loading,
}: {
  receipts: UsageReceipt[];
  nameModel: (id: string) => string;
  onSelect: (receipt: UsageReceipt) => void;
  loading: boolean;
}) {
  return (
    <Card id="usage-receipts">
      <CardHeader>
        <CardTitle>Receipts</CardTitle>
        <CardDescription>
          Open one to see what happened to a request.
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
                key={receipt.selectionKey}
                aria-label={`Receipt details for ${receipt.summary}`}
                onClick={() => onSelect(receipt)}
                className="hover:bg-muted/40 focus-visible:outline-ring flex w-full items-center gap-3 py-4 text-left focus-visible:outline-2"
              >
                <CapabilityLabel capability={receipt.capability} compact />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-meta font-medium">
                    {receipt.summary}
                  </p>
                  {receipt.model && (
                    <p
                      className="text-muted-foreground mt-1 truncate text-meta"
                      title={nameModel(receipt.model)}
                    >
                      {nameModel(receipt.model)}
                    </p>
                  )}
                  {receipt.sourceLabel ? (
                    <p className="text-muted-foreground mt-1 text-meta">
                      {receipt.sourceLabel}
                    </p>
                  ) : null}
                  {(receipt.createdAt || receipt.durationMs != null) ? (
                    <p className="text-muted-foreground mt-1 text-meta">
                      {[receipt.createdAt ? <RelativeTime key="time" ts={receipt.createdAt} /> : null,
                        receipt.durationMs != null ? formatDuration(receipt.durationMs) : null]
                        .filter(Boolean)
                        .map((part, index) => <span key={index}>{index ? " · " : ""}{part}</span>)}
                    </p>
                  ) : null}
                </div>
                <StatusBadge status={receipt.status} />
                <span className="hidden text-meta sm:block">
                  <ReceiptCost receipt={receipt} summary />
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
            action={
              <Button asChild variant="outline">
                <a href="/ui/apps/">Connect an app</a>
              </Button>
            }
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
  const nameModel = useModelName();
  const owner = ["owner", "superadmin"].includes(useViewer().role);
  const named = (id: string | null | undefined) => (id ? nameModel(id) : id);
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
              <div className="rounded-panel border p-4">
                <div className="mb-3 flex items-center gap-3">
                  <CapabilityLabel capability={receipt.capability} />
                  <StatusBadge status={receipt.status} />
                </div>
                {receiptOutcomeSummary(receipt) && (
                  <p className="text-muted-foreground mt-2 text-meta">
                    {receiptOutcomeSummary(receipt)}
                  </p>
                )}
                {receiptOutcomeCause(receipt) ? (
                  <p className="text-muted-foreground mt-2 text-meta">
                    {receiptOutcomeCause(receipt)}
                  </p>
                ) : null}
                {receiptNextAction(receipt) ? (
                  <p className="text-muted-foreground mt-2 text-meta">
                    {receiptNextAction(receipt)}
                  </p>
                ) : null}
              </div>
              <dl className="grid gap-4 text-meta sm:grid-cols-2">
                <Detail
                  label="Requested model"
                  value={named(receipt.requestedModel)}
                />
                <Detail
                  label="Served model"
                  value={named(receipt.servedModel)}
                />
                <Detail
                  label="Declared fallback"
                  value={named(receipt.fallbackModel)}
                />
                <Detail
                  label="Duration"
                  value={formatDuration(receipt.durationMs)}
                />
                <Detail
                  label="Cost"
                  value={<ReceiptCost receipt={receipt} />}
                />
                <Detail
                  label="When"
                  value={
                    receipt.createdAt ? (
                      <RelativeTime ts={receipt.createdAt} />
                    ) : (
                      null
                    )
                  }
                />
                <Detail
                  label="Request ID"
                  value={
                    receipt.requestId ? (
                      <span className="inline-flex min-w-0 items-center gap-2">
                        <span className="min-w-0 break-all">
                          {receipt.requestId}
                        </span>
                        <CopyButton
                          value={receipt.requestId}
                          aria-label="Copy request ID"
                        />
                      </span>
                    ) : "No request ID recorded"
                  }
                  wide
                />
                {receipt.sourceLabel ? (
                  <Detail label="Source" value={receipt.sourceLabel} wide />
                ) : null}
              </dl>
              <details>
                <summary className="cursor-pointer text-meta font-medium">
                  Technical details
                </summary>
                <div className="mt-4">
                  <CodeBlock
                    language="json"
                    code={JSON.stringify(technicalModelDetails(receipt.raw, owner), null, 2)}
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
  if (value == null || value === "") return null;
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
      {receiptOutcome(status)}
    </Badge>
  );
}

function receiptOutcome(status: string | null) {
  return (
    (
      {
        success: "Completed",
        succeeded: "Completed",
        ok: "Completed",
        failed: "Failed",
        error: "Failed",
        backend_failed: "Failed",
        rejected: "Rejected",
        cancelled: "Cancelled",
        disconnected: "Disconnected",
        timed_out: "Timed out",
        accepted: "Accepted",
        running: "In progress",
        in_progress: "In progress",
        stale_incomplete: "Outcome not recorded",
        incomplete: "Outcome not recorded",
      } as Record<string, string>
    )[status ?? ""] ?? "Outcome not recorded"
  );
}

function receiptOutcomeSummary(receipt: UsageReceipt) {
  const label = receiptOutcome(receipt.status);
  if (label === "Completed") return "";
  if (label === "Rejected") return "The gateway rejected this request.";
  if (label === "Failed") return "We couldn't tell where this stopped.";
  if (label === "Timed out")
    return "The request timed out before a result was recorded.";
  if (label === "Accepted")
    return "The gateway accepted this request for processing.";
  if (label === "In progress")
    return "The request was still running when this receipt was recorded.";
  if (label === "Cancelled")
    return "The request was cancelled before completion.";
  if (label === "Disconnected")
    return "The connection ended before completion was recorded.";
  return "The receipt does not include a final outcome.";
}

function receiptOutcomeCause(receipt: UsageReceipt) {
  const label = receiptOutcome(receipt.status);
  if (
    !["Failed", "Rejected", "Timed out"].includes(
      label,
    )
  )
    return null;
  const raw = object(receipt.raw);
  const metadata = object(raw.metadata);
  if (label === "Failed") {
    const stage = nullableString(raw.stage ?? metadata.stage);
    const code = nullableString(raw.error_code ?? metadata.error_code);
    return [stage ? `Stage: ${stage}` : null, code ? `Error code: ${code}` : null]
      .filter(Boolean).join(" · ") || null;
  }
  const status = nullableNumber(metadata.status_code ?? raw.status_code);
  if (status != null && status >= 400) return errorCopy({ status }).message;
  return null;
}

function receiptNextAction(receipt: UsageReceipt) {
  const label = receiptOutcome(receipt.status);
  if (
    ["Failed", "Rejected", "Timed out"].includes(
      label,
    )
  )
    return "Open Technical details for the recorded status and warnings.";
  if (label === "Outcome not recorded")
    return "Check for a newer receipt before treating this request as complete.";
  return null;
}

function UsageSkeleton() {
  return (
    <div
      aria-hidden="true"
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

async function fetchUsageSummary(
  person: string,
  source: string,
): Promise<UsageSummary> {
  const params = new URLSearchParams({ period: "month" });
  if (person !== "default") params.set("person", person);
  if (source !== "all") params.set("source", source);
  const response = await fetch(`/me/api/usage/summary?${params}`, {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.ok) return normalizeSummary(await response.json());
  throw new Error("Usage summary could not be loaded.");
}

function normalizeSummary(raw: unknown): UsageSummary {
  const data = object(raw);
  const rawReceipts = array(data.receipts);
  const keys = receiptSelectionKeys(rawReceipts.map(object));
  const receipts = rawReceipts.map((raw, index) => normalizeReceipt(raw, keys[index]));
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
    failedRequests: nullableNumber(data.failed_requests),
    mediaCount: number(data.media_count ?? data.mediaCount, 0),
    remoteSpendUsd,
    spendMeasured: remoteSpendAvailable || remoteSpendUsd !== null,
    byDay,
    byModel,
    receipts,
    sources: array(data.sources)
      .map((item) => {
        const row = object(item);
        return {
          id: string(row.id, ""),
          label: string(row.label, "Source"),
          requests: requestCount(row.requests),
          disabled: row.disabled === true,
          createdAt: nullableNumber(row.created_at),
        };
      })
      .filter((item) => item.id),
    people: normalizePeople(data.people),
    retentionDays: positiveNullableNumber(data.retention_days),
    coverageStart: nullableString(data.coverage_start ?? data.coverageStart),
    coverageNote: nullableString(data.coverage_note ?? data.coverageNote),
    spendMeasuredRequests: nullableNumber(
      data.spend_measured_requests ?? data.spendMeasuredRequests,
    ),
    spendTotalRequests: nullableNumber(
      data.spend_total_requests ?? data.spendTotalRequests,
    ),
    paidRequests: nullableNumber(data.paid_requests ?? data.paidRequests),
    unknownPaidRequests: nullableNumber(
      data.unknown_paid_requests ?? data.unknownPaidRequests,
    ),
    truncated: Boolean(data.truncated),
  };
}

function normalizePeople(raw: unknown): UsagePerson[] {
  const usedLabels = new Set<string>();
  const emailSuffixes = new Map(
    array(raw).map((item) => {
      const row = object(item);
      return [string(row.id, ""), string(row.email, "").trim().match(/^([^\s@]+)@[^\s@]+$/)?.[1]];
    }),
  );
  const people = array(raw)
    .map((item) => {
      const person = object(item);
      const id = string(person.id, "");
      const name = [person.label, person.name]
        .map((value) => string(value, "").trim())
        .find((value) => value && !/^[^\s@]+@[^\s@]+$/.test(value));
      if (name) return { id, label: name };
      // Derive a short identifier without exposing email addresses or other ID text.
      let hash = 2166136261;
      for (const character of id) {
        hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
      }
      const base = `Person · ${(hash >>> 0).toString(16).padStart(8, "0")}`;
      let label = base;
      let suffix = 2;
      while (usedLabels.has(label)) label = `${base}-${suffix++}`;
      usedLabels.add(label);
      return { id, label };
    })
    .filter((person) => person.id);
  const counts = new Map<string, number>();
  for (const { label } of people) {
    counts.set(label, (counts.get(label) ?? 0) + 1);
    usedLabels.add(label);
  }
  const ordinals = new Map<string, number>();
  // Assign in ID order so response ordering cannot swap a person's suffix.
  for (const person of [...people].sort((a, b) =>
    a.id < b.id ? -1 : a.id > b.id ? 1 : 0,
  )) {
    if (counts.get(person.label) === 1) continue;
    const ordinal = (ordinals.get(person.label) ?? 0) + 1;
    ordinals.set(person.label, ordinal);
    const suffix = emailSuffixes.get(person.id) || `#${ordinal}`;
    const base = `${person.label} · ${suffix}`;
    let label = base;
    let collision = 2;
    while (usedLabels.has(label)) label = `${base} · #${collision++}`;
    usedLabels.add(label);
    person.label = label;
  }
  return people;
}

function normalizeReceipt(raw: unknown, selectionKey: string): UsageReceipt {
  const data = object(raw);
  const metadata = object(data.metadata);
  const capability = normalizeCapability(
    data.capability ?? data.intent ?? metadata.capability ?? data.protocol,
  );
  const model = modelId(
    data.resolved_model ?? data.requested_model ?? data.model ?? metadata.model,
  );
  const requestedModel = modelId(
    data.requested_model ?? metadata.requested_model,
  );
  const servedModel = modelId(
    data.resolved_model ?? data.served_model ?? metadata.resolved_model,
  );
  const fallbackModel = modelId(
    data.fallback_model ?? data.declared_fallback ?? metadata.fallback_model,
  );
  const requestId = nullableString(data.request_id) ?? nullableString(data.id);
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
    selectionKey,
    requestId,
    createdAt: nullableDate(
      data.created_at ?? data.createdAt ?? data.accepted_at_ms,
    ),
    capability,
    model,
    requestedModel,
    servedModel,
    fallbackModel,
    status,
    durationMs,
    costUsd,
    summary: `${requestNouns[capability]} request`,
    sourceLabel: nullableString(object(data.source).label ?? data.source_label),
    raw,
  };
}

function normalizeDay(raw: unknown): UsageDay {
  const data = object(raw);
  const byCapability = normalizeCapabilityCounts(
    data.capabilities ?? data.by_capability ?? data,
  );
  const recordedSum = sumCounts(byCapability);
  const named = recordedSum - byCapability.other;
  const total = requestCount(data.total ?? data.requests) ?? recordedSum;
  // Never cap, reduce or rescale a recorded count. Unexplained remainder joins
  // Other; when recorded counts add to more than the total, flag the day.
  byCapability.other = Math.max(byCapability.other, total - named);
  return {
    date: string(data.date ?? data.day, ""),
    total,
    byCapability,
    countsExceedTotal: recordedSum > total ? recordedSum : null,
  };
}

/** A usable request count, or null for missing, negative or absurd values. */
function requestCount(value: unknown): number | null {
  const count = number(value, Number.NaN);
  return Number.isSafeInteger(count) && count >= 0 ? count : null;
}

/** Preserve exact IDs for catalog lookups and keys; format only when rendering. */
function modelId(value: unknown): string | null {
  return nullableString(value);
}

function normalizeModel(raw: unknown): UsageModel {
  const data = object(raw);
  return {
    model: nullableString(data.model ?? data.name) ?? "",
    capability: normalizeCapability(data.capability ?? data.intent),
    requests: number(data.requests ?? data.count, 0),
    costUsd: nullableNumber(
      data.remote_spend ?? data.cost_usd ?? data.remote_cost_usd,
    ),
    paidRequests: requestCount(data.paid_requests),
    unknownPaidRequests: requestCount(data.unknown_paid_requests),
  };
}

// Keys a day row carries besides its capability counts.
const dayFields = new Set(["date", "day", "total", "requests"]);

function normalizeCapabilityCounts(
  raw: unknown,
): Record<UsageCapability, number> {
  const counts = Object.fromEntries(
    capabilities.map((capability) => [capability, 0]),
  ) as Record<UsageCapability, number>;
  // Every recorded count lands somewhere: exact names, then aliases, then Other.
  for (const [key, value] of Object.entries(object(raw))) {
    if (dayFields.has(key)) continue;
    const capability = (capabilities as string[]).includes(key)
      ? (key as UsageCapability)
      : normalizeCapability(key);
    counts[capability] += requestCount(value) ?? 0;
  }
  return counts;
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
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function positiveNullableNumber(value: unknown): number | null {
  const parsed = nullableNumber(value);
  return parsed != null && parsed > 0 ? parsed : null;
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
  if (value == null) return "";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)} s`;
}

function spendCoverageDetail(summary: UsageSummary) {
  const unknownPaid = summary.unknownPaidRequests;
  // A positive counter proves paid requests exist even if the other is missing.
  if ((summary.paidRequests ?? 0) > 0 || (unknownPaid ?? 0) > 0) {
    const coverage =
      summary.spendMeasuredRequests !== null && summary.spendTotalRequests !== null
        ? `Cost measured for ${formatNumber(summary.spendMeasuredRequests)} of ${formatNumber(summary.spendTotalRequests)} requests.`
        : "Measurement coverage is unavailable.";
    const unknown =
      unknownPaid !== null && unknownPaid > 0
        ? ` ${formatNumber(unknownPaid)} paid ${unknownPaid === 1 ? "request has" : "requests have"} unknown cost.`
        : "";
    return `${coverage}${unknown}`;
  }
  if (summary.paidRequests === null || unknownPaid === null)
    return "Paid request count is unknown.";
  return "";
}

// A missing cost is unknown unless the receipt says the request stayed local.
function ReceiptCost({
  receipt,
  summary = false,
}: {
  receipt: UsageReceipt;
  summary?: boolean;
}) {
  if (receipt.costUsd != null)
    return <Money amount={receipt.costUsd} summary={summary} />;
  if (object(receipt.raw).remote === false) return <>No provider charge</>;
  return <>Cost not recorded</>;
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
