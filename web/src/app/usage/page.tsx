"use client";

import Link from "next/link";
import { lazy, Suspense, useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
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
import { receiptSelectionKeys } from "@/lib/receipt-selection";
import { formatTimestamp } from "@/lib/format";
import { memberTerms, taskNames } from "@/lib/terms";
import { RequestReceiptSheet } from "../activity/requests";
import { useModelName } from "@/lib/use-model-name";
import { sourceGroups, usageCsv, type UsageSource } from "./source-options";
import { normalizeCapability, type UsageCapability } from "./usage-capability";
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
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Stat, StatStrip } from "@/components/product/stat-strip";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { Callout, StatusLine } from "@/components/product/status-ui";
import {
  CapabilityIcon,
  EmptyState,
  Money,
  ErrorState,
  PageHeader,
} from "@/components/product";

const UsageChart = lazy(() => import("./usage-chart"));

interface UsageDay {
  date: string;
  total: number | null;
  byCapability: Record<UsageCapability, number | null>;
  /** Sum of the recorded capability counts when it exceeds `total`, else null. */
  countsExceedTotal: number | null;
}

interface UsageModel {
  model: string;
  capability: UsageCapability;
  requests: number | null;
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
  personLabel?: string | null;
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
  receiptsForever: boolean;
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
  const { data, error, isPending, isFetching, isError, refetch } = useQuery({
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

  function downloadCsv() {
    if (!data || isFetching) return;
    const csv = usageCsv(data.receipts.map((receipt) => {
      const date = receipt.createdAt === null ? null : new Date(receipt.createdAt);
      return {
        date: date && !Number.isNaN(date.getTime()) ? date.toISOString() : "",
        app: receipt.sourceLabel ?? nullableString(object(receipt.raw).app_label) ?? "",
        modelName: receipt.model ? nameModel(receipt.model) : "",
        requests: 1,
        spendUsd: receipt.costUsd ?? (object(receipt.raw).remote === false ? 0 : null),
      };
    }));
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "usage-loaded-receipts.csv";
    document.body.appendChild(link);
    try {
      link.click();
    } finally {
      link.remove();
      // Give the browser time to consume the object URL before releasing it.
      const revoke = URL.revokeObjectURL.bind(URL);
      window.setTimeout(() => revoke(url), 1000);
    }
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title="Usage"
        subtitle="Usage this month"
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
                  App or connection
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
                    <SelectValue placeholder="All apps and connections" />
                  </SelectTrigger>
                  <SelectContent
                    position="popper"
                    align="start"
                    collisionPadding={8}
                    className="max-w-[calc(100vw-1rem)]"
                  >
                    <SelectItem value="all">All apps and connections</SelectItem>
                    {sources.active.map((item) => (
                      <SelectItem key={item.id} value={item.id}>
                        {item.label}
                      </SelectItem>
                    ))}
                    {sources.connections.length ? (
                      <SelectGroup>
                        <SelectLabel>Connected with sign-in</SelectLabel>
                        {sources.connections.map((item) => (
                          <SelectItem key={item.id} value={item.id}>
                            {item.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    ) : null}
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
        {isPending || (isFetching && !isError) ? (
          <StatusLine compact tone="busy" text={data ? "Refreshing usage…" : "Loading usage…"} />
        ) : null}
      </PageHeader>
      <ReceiptDeepLink />
      {data ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" disabled={isFetching} onClick={downloadCsv}
            aria-describedby="usage-csv-scope">
            Download CSV
          </Button>
          <p id="usage-csv-scope" className="text-muted-foreground text-xs">
            Exports {data.receipts.length} loaded request receipts for these filters.
            {data.truncated || data.requests > data.receipts.length
              ? " Recent receipts only; excludes requests not loaded."
              : ""}
          </p>
        </div>
      ) : null}
      {isError && data ? (
        <Callout
          severity="attention"
          title="Usage couldn’t refresh. These are the last loaded totals."
          headerAction={
            <Button variant="outline" disabled={isFetching} onClick={() => void refetch()}>
              {isFetching ? "Retrying…" : "Retry usage"}
            </Button>
          }
        >
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
          <p>{error instanceof Error && error.message === "Usage totals weren’t included in the response. Try again." ? error.message : "Try again to load this month’s usage."}</p>
        </Callout>
      ) : !hasUsage ? (
        <EmptyState
          icon={Activity}
          title={
            filtersActive
              ? "No requests match these filters this month"
              : "No requests this month"
          }
          description={
            data.retentionDays
              ? `Use a connected app to see its requests and generated media here. Request receipts are kept for ${data.retentionDays} days.`
              : "Use a connected app to see its requests, generated media and request receipts here."
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
                <Link prefetch={false} href="/apps/">Connect an app</Link>
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
              showPerson={owner && effectivePerson === "all"}
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
  const unnamed = people.filter((person) => person.label.startsWith(UNNAMED_PERSON));
  return (
    <div className="space-y-2">
      <label
        className="sr-only"
        htmlFor="usage-person"
      >
        Whose usage
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
            { id: "self", label: "You" },
            ...people.filter((item) => !["all", "self"].includes(item.id)),
          ].map((item) => (
            <SelectItem key={item.id} value={item.id}>
              {item.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {/* Only people without a recorded name need their account ID to be told apart. */}
      {unnamed.length > 0 && <details className="text-meta">
        <summary className="min-h-11 min-w-11 cursor-pointer content-center">Person details</summary>
        <dl className="mt-2 space-y-2">
          {unnamed.map((person) => <div key={person.id}><dt>{person.label}</dt><dd className="break-all">{person.id}</dd></div>)}
        </dl>
      </details>}
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
  return `${formatRequests(summary.failedRequests)} · ${share === "Unknown" ? "share unknown" : `${share} of requests`}`;
}

function SummaryRow({ summary }: { summary: UsageSummary }) {
  const noPaidRequests =
    summary.paidRequests === 0 && summary.unknownPaidRequests === 0 &&
    summary.remoteSpendUsd === 0;
  return (
    <StatStrip label="This month">
      <Stat
        label="Requests"
        value={formatRequests(summary.requests)}
        note="This month"
      />
      <Stat
        label="Spend this month (USD)"
        value={
          noPaidRequests ? (
            "No paid requests"
          ) : summary.remoteSpendUsd === null ? (
            "Cost not recorded"
          ) : (
            <Money amount={summary.remoteSpendUsd} summary precise />
          )
        }
        note={<><span>{spendCoverageDetail(summary)}</span><CoverageNote summary={summary} /></>}
      />
      <Stat
        label="Generated media"
        value={`${formatNumber(summary.mediaCount)} media ${summary.mediaCount === 1 ? "item" : "items"}`}
        note="Images, videos and audio this month"
      />
      <Stat
        label="Failed requests"
        value={failedFigure(summary)}
        note="This month"
      />
    </StatStrip>
  );
}

function CoverageNote({ summary }: { summary: UsageSummary }) {
  const facts = [
    summary.receiptsForever ? "Receipts are kept until you delete them." : summary.retentionDays ? `Receipts are kept for ${summary.retentionDays} days.` : "",
    summary.coverageStart ? `Coverage starts ${formatDate(summary.coverageStart)}.` : "",
    summary.coverageNote ? formatSentence(summary.coverageNote) : "",
    summary.truncated ? "This view is limited to the most recent stored receipts." : "",
  ].filter(Boolean);
  if (!facts.length) return null;
  return (
    <details className="text-muted-foreground text-meta">
      <summary className="flex min-h-8 w-fit cursor-pointer items-center max-sm:min-h-11 max-sm:min-w-11">
        Receipt coverage
      </summary>
      <ul className="mt-1 list-inside list-disc">{facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
    </details>
  );
}

function DailyChart({ days, models, requests }: { days: UsageDay[]; models: UsageModel[]; requests: number }) {
  const rows = days.filter((day) => day.total !== null).map((day) => ({
    label: shortDate(day.date),
    total: day.total as number,
  }));
  const tasks = capabilities.map((capability) => {
    const modelsForTask = models.filter((model) => model.capability === capability);
    const measured = modelsForTask.filter((model) => model.costUsd !== null);
    const incomplete = modelsForTask.some((model) =>
      ["partial", "unavailable"].includes(modelCostKind(model)),
    );
    return {
      capability,
      requests: modelsForTask.every((model) => model.requests !== null)
        ? modelsForTask.reduce((sum, model) => sum + (model.requests ?? 0), 0) : null,
      costUsd: measured.length ? measured.reduce((sum, model) => sum + (model.costUsd ?? 0), 0) : null,
      unavailable: measured.length === 0,
      incomplete,
    };
  }).filter((task) => task.requests === null || task.requests > 0).sort((a, b) => (b.requests ?? -1) - (a.requests ?? -1));
  const active = capabilities.filter((capability) =>
    days.some((day) => (day.byCapability[capability] ?? 0) > 0),
  );
  const overfullDays = days.filter((day) => day.countsExceedTotal !== null).length;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Requests by day</CardTitle>
        <CardDescription>
          Daily request totals this month
          {overfullDays > 0 &&
            `. On ${overfullDays} ${overfullDays === 1 ? "day" : "days"} the ${memberTerms.task} counts add to more than the recorded total; the bars show the recorded totals and the daily table keeps the recorded task counts.`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length < 1 ? (
          <EmptyState icon={Activity} title="Daily totals weren’t recorded for these requests." />
        ) : (
          <>
            <div className="h-72 w-full">
              <Suspense fallback={<div role="status" className="relative h-72 w-full"><Skeleton className="h-72 w-full" /><span className="absolute inset-0 flex items-center justify-center">Loading request chart</span></div>}>
                <UsageChart rows={rows} />
              </Suspense>
            </div>
          </>
        )}
        {days.length > 0 && (
            <details className="mt-4 min-w-0 overflow-x-auto">
              <summary className="flex min-h-8 w-fit cursor-pointer items-center text-meta max-sm:min-h-11 max-sm:min-w-11">View daily totals</summary>
              <table className="mt-2 w-full text-meta whitespace-nowrap [&_td]:px-2 [&_td]:py-1 [&_td]:text-right [&_td]:tabular-nums [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-medium [&_thead_th]:text-muted-foreground [&_thead_th:not(:first-child)]:text-right">
                <caption className="sr-only">Requests by day and {memberTerms.task}</caption>
                <thead>
                  <tr>
                    <th scope="col">Date</th>
                    {active.map((capability) => (
                      <th key={capability} scope="col">
                        {capabilityLabels[capability]}
                      </th>
                    ))}
                    <th scope="col">Daily total</th>
                  </tr>
                </thead>
                <tbody>
                  {days.map((day) => (
                    <tr key={day.date}>
                      <th scope="row">{day.date}</th>
                      {active.map((capability) => (
                        <td key={capability}>{formatRequests(day.byCapability[capability])}</td>
                      ))}
                      <td>
                        {formatRequests(day.total)}
                        {day.countsExceedTotal !== null &&
                          ` (${memberTerms.task} counts add to ${formatRequests(day.countsExceedTotal)})`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
        )}
        <div className="mt-4 space-y-3">
          <h2 className="text-heading font-semibold">By task</h2>
          {tasks.length ? (
            <>
              <p className="text-muted-foreground text-meta">Request counts and recorded spend this month · share of this month’s requests</p>
              <Table dense aria-label="By task">
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col" className="max-sm:px-2">Task</TableHead>
                    <TableHead scope="col" numeric className="max-sm:px-2">Requests</TableHead>
                    <TableHead scope="col" numeric className="max-sm:px-2">Share of requests (%)</TableHead>
                    <TableHead scope="col" numeric className="max-sm:px-2">Spend (USD)</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tasks.map((task) => (
                    <TableRow key={task.capability} className="max-sm:h-(--row-phone-2)">
                      <TableCell rowName className="max-sm:px-2 max-sm:whitespace-normal">{capabilityLabels[task.capability]}</TableCell>
                      <TableCell numeric className="max-sm:px-2">{formatRequests(task.requests)}</TableCell>
                      <TableCell numeric className="max-sm:px-2">{requests > 0 && task.requests !== null ? `${Math.round(task.requests / requests * 1000) / 10}%` : "Unknown"}</TableCell>
                      <TableCell numeric className="max-sm:px-2 max-sm:whitespace-normal">
                        {task.unavailable ? "Cost not recorded" : <Money amount={task.costUsd} summary precise />}
                        {!task.unavailable && task.incomplete ? <span className="text-muted-foreground ml-1 text-meta">Some costs missing</span> : null}
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
  const modelsId = useId();
  const sortedRows = [...rows].sort((a, b) =>
    (b.requests ?? -1) - (a.requests ?? -1) || (a.model < b.model ? -1 : a.model > b.model ? 1 : 0),
  );
  const visibleRows = showAll ? sortedRows : sortedRows.slice(0, 5);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Models</CardTitle>
        <CardDescription>Request counts and recorded spend this month, busiest models first</CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length ? (
          <div className="space-y-4">
            <Table id={modelsId} dense aria-label="Models">
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">
                    Task and model
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Requests
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Spend (USD)
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleRows.map((row) => {
                  const name = row.model ? nameModel(row.model) : "";
                  return (
                    <TableRow key={`${row.model}-${row.capability}`}>
                      <TableCell className="w-full max-w-0 text-body font-medium">
                        {/* The cell keeps the table's nowrap; this block wraps the
                            task and model onto their own lines instead. */}
                        <div className="min-w-0 whitespace-normal break-words">
                          <span aria-hidden="true" className="bg-chart-1 mr-2 inline-block size-2 rounded-full align-middle" />
                          <span>{capabilityLabels[row.capability]}</span>
                          {name && name !== capabilityLabels[row.capability] && (
                            <span className="text-muted-foreground block min-w-0 break-words text-meta" title={name}>
                              {name}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell numeric>
                        {formatRequests(row.requests)}
                      </TableCell>
                      {/* On a phone "Cost not recorded" wraps instead of clipping. */}
                      <TableCell numeric className="max-sm:whitespace-normal">
                        <ModelCost row={row} />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            {rows.length > 5 ? (
              <Button variant="ghost" className="min-h-11 min-w-11" aria-expanded={showAll} aria-controls={modelsId} onClick={onToggle}>
                {showAll ? "Show top 5 models" : `View all ${rows.length} models`}
              </Button>
            ) : null}
          </div>
        ) : (
          <EmptyState icon={Layers} title="Model totals weren’t recorded." />
        )}
      </CardContent>
    </Card>
  );
}

type ModelCostKind = "amount" | "partial" | "unavailable";

/**
 * What the Spend cell of a model row says. A recorded charge shows the amount.
 * A paid-request counter is not routing evidence. Without an amount,
 * say Cost not recorded, never Local or a guessed zero.
 */
function modelCostKind(row: UsageModel): ModelCostKind {
  const unknownPaid = (row.unknownPaidRequests ?? 0) > 0;
  if (row.costUsd != null && row.costUsd > 0) return unknownPaid ? "partial" : "amount";
  if (unknownPaid) return "unavailable";
  if (row.costUsd != null) return "amount";
  return "unavailable";
}

function ModelCost({ row }: { row: UsageModel }) {
  const kind = modelCostKind(row);
  if (kind === "unavailable")
    return <span className="text-muted-foreground">Cost not recorded</span>;
  return (
    <>
      <Money amount={row.costUsd} summary precise />
      {kind === "partial" ? (
        <span className="text-muted-foreground ml-1 text-meta">Some costs missing</span>
      ) : null}
    </>
  );
}

function ReceiptsList({
  receipts,
  nameModel,
  onSelect,
  loading,
  showPerson,
}: {
  receipts: UsageReceipt[];
  nameModel: (id: string) => string;
  onSelect: (receipt: UsageReceipt) => void;
  loading: boolean;
  showPerson: boolean;
}) {
  return (
    <Card id="usage-receipts">
      <CardHeader>
        <CardTitle>Recent request receipts</CardTitle>
        <CardDescription>
          Open a receipt to see what happened to a recent request.
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
                aria-label={`Receipt details for ${receipt.summary}${showPerson ? ` · ${receipt.personLabel ?? "Person not recorded"}` : ""}`}
                onClick={() => onSelect(receipt)}
                className="hover:bg-muted/40 focus-visible:outline-ring flex w-full items-center gap-3 py-4 text-left focus-visible:outline-2"
              >
                <CapabilityLabel capability={receipt.capability} compact />
                <div className="min-w-0 flex-1">
                  <p className="break-words text-meta font-medium">
                    {receipt.summary}
                  </p>
                  {showPerson && (
                    <p className="text-muted-foreground mt-1 break-words text-meta">
                      {receipt.personLabel ?? "Person not recorded"}
                    </p>
                  )}
                  {receipt.model && (
                    <p
                      className="text-muted-foreground mt-1 break-words text-meta"
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
                      {[receipt.createdAt ? <time key="time" {...formatTimestamp(receipt.createdAt)} /> : null,
                        receipt.durationMs != null ? formatDuration(receipt.durationMs) : null]
                        .filter(Boolean)
                        .map((part, index) => <span key={index}>{index ? " · " : ""}{part}</span>)}
                    </p>
                  ) : null}
                  <p className="mt-1 break-words text-meta">
                    <ReceiptCost receipt={receipt} summary />
                  </p>
                </div>
                <StatusBadge status={receipt.status} />
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
            title="No receipts are available for this view."
          />
        )}
      </CardContent>
    </Card>
  );
}

// /usage/?receipt={request_id} opens that receipt (links from Try, Library,
// Home, Models, Workflows and Status). Closing it drops the query parameter.
class ReceiptUnavailable extends Error {}

async function fetchReceipt(requestId: string): Promise<UsageReceipt> {
  const response = await fetch(
    `/me/api/receipts/${encodeURIComponent(requestId)}`,
    { credentials: "same-origin", cache: "no-store" },
  );
  if (response.status === 404 || response.status === 403)
    throw new ReceiptUnavailable();
  if (!response.ok) throw new Error("The receipt could not be loaded.");
  return normalizeReceipt(await response.json(), `receipt:${requestId}`);
}

function ReceiptDeepLink() {
  const [requestId, setRequestId] = useState<string | null>(null);
  useEffect(() => {
    setRequestId(new URLSearchParams(window.location.search).get("receipt") || null);
  }, []);
  const {
    data: receipt,
    error,
    isPending,
    isError,
    isFetching,
    refetch,
  } = useQuery({
    queryKey: ["usage", "receipt", requestId],
    queryFn: () => fetchReceipt(requestId!),
    enabled: requestId !== null,
    retry: false,
  });
  function close() {
    setRequestId(null);
    const params = new URLSearchParams(window.location.search);
    params.delete("receipt");
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`,
    );
  }
  if (requestId === null) return null;
  return isPending ? (
    <StatusLine compact tone="busy" text="Opening receipt…" />
  ) : isError ? (
    error instanceof ReceiptUnavailable ? (
      <ErrorState
        severity="attention"
        heading="Receipt not available"
        message="This receipt isn’t available. It may belong to someone else or be deleted."
        action={{ label: "Dismiss", onClick: close }}
      />
    ) : (
      <ErrorState
        heading="Couldn’t open this receipt"
        message="We couldn’t load this receipt. Try again."
        action={{ label: "Try again", onClick: () => void refetch() }}
        retrying={isFetching}
      />
    )
  ) : (
    <ReceiptSheet
      receipt={receipt}
      onOpenChange={(open) => {
        if (!open) close();
      }}
    />
  );
}

function ReceiptSheet({ receipt, onOpenChange }: {
  receipt: UsageReceipt | null;
  onOpenChange: (open: boolean) => void;
}) {
  const raw = object(receipt?.raw);
  return <RequestReceiptSheet
    record={receipt ? {
      receipt: { ...raw, request_id: receipt.requestId ?? "", status: receipt.status ?? undefined,
        requested_model: receipt.requestedModel ?? undefined, resolved_model: receipt.servedModel ?? undefined,
        latency_ms: receipt.durationMs },
      person_label: nullableString(raw.person_label) ?? "",
      app_label: nullableString(raw.app_label) ?? receipt.sourceLabel ?? "",
      created_at: receipt.createdAt,
    } : null}
    title="Request receipt" description={receipt?.summary}
    cost={receipt ? <ReceiptCost receipt={receipt} /> : null}
    onOpenChange={onOpenChange}
  />;
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
  const requests = nullableNumber(data.requests ?? data.request_count);
  const mediaCount = nullableNumber(data.media_count ?? data.mediaCount);
  if (requests === null || mediaCount === null) {
    throw new Error("Usage totals weren’t included in the response. Try again.");
  }
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
    requests,
    failedRequests: nullableNumber(data.failed_requests),
    mediaCount,
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
          kind: string(row.kind, "app"),
          requests: requestCount(row.requests),
          disabled: row.disabled === true,
          createdAt: nullableNumber(row.created_at),
        };
      })
      .filter((item) => item.id),
    people: normalizePeople(data.people),
    retentionDays: positiveNullableNumber(data.retention_days),
    receiptsForever: data.retention_days === null,
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

const UNNAMED_PERSON = "Person name not recorded";

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
      return { id, label: UNNAMED_PERSON };
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
    personLabel: nullableString(data.person_label),
    raw,
  };
}

function normalizeDay(raw: unknown): UsageDay {
  const data = object(raw);
  const byCapability = normalizeCapabilityCounts(
    data.capabilities ?? data.by_capability ?? data,
  );
  const rawCounts = object(data.capabilities ?? data.by_capability ?? data);
  // One unreadable count makes the day's sum unknown; no counts at all sum to 0.
  const recordedSum = Object.entries(rawCounts).some(([key, value]) => !dayFields.has(key) && requestCount(value) === null) ? null : (sumCounts(byCapability) ?? 0);
  const named = recordedSum === null ? null : recordedSum - (byCapability.other ?? 0);
  const total = "total" in data ? requestCount(data.total)
    : "requests" in data ? requestCount(data.requests) : recordedSum;
  // Never cap, reduce or rescale a recorded count. Unexplained remainder joins
  // Other; when recorded counts add to more than the total, flag the day.
  if (total !== null && named !== null) byCapability.other = Math.max(byCapability.other ?? 0, total - named);
  return {
    date: string(data.date ?? data.day, ""),
    total,
    byCapability,
    countsExceedTotal: recordedSum !== null && total !== null && recordedSum > total ? recordedSum : null,
  };
}

/** A usable request count, or null for missing, negative or absurd values. */
function requestCount(value: unknown): number | null {
  const count = nullableNumber(value);
  return count !== null && Number.isSafeInteger(count) ? count : null;
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
    requests: nullableNumber(data.requests ?? data.count),
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
): Record<UsageCapability, number | null> {
  const counts = Object.fromEntries(
    capabilities.map((capability) => [capability, null]),
  ) as Record<UsageCapability, number | null>;
  const seen = new Set<UsageCapability>();
  // Every recorded count lands somewhere: exact names, then aliases, then Other.
  for (const [key, value] of Object.entries(object(raw))) {
    if (dayFields.has(key)) continue;
    const capability = (capabilities as string[]).includes(key)
      ? (key as UsageCapability)
      : normalizeCapability(key);
    const count = requestCount(value);
    counts[capability] = count === null || (seen.has(capability) && counts[capability] === null)
      ? null : (counts[capability] ?? 0) + count;
    seen.add(capability);
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

function nullableNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
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

function sumCounts(counts: Record<UsageCapability, number | null>) {
  const recorded = Object.values(counts).filter((value): value is number => value !== null);
  return recorded.length ? recorded.reduce((sum, value) => sum + value, 0) : null;
}

function formatNumber(value: number | null) {
  if (value === null) return "Unknown";
  return new Intl.NumberFormat("en-US").format(value);
}

function formatRequests(value: number | null) {
  return value === null ? "Unknown" : `${formatNumber(value)} ${value === 1 ? "request" : "requests"}`;
}

function formatDuration(value: number | null) {
  if (value == null) return "";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)} s`;
}

function spendCoverageDetail(summary: UsageSummary) {
  // No paid requests means no costs to record; say nothing beside "No paid requests".
  if (summary.paidRequests === 0 && summary.unknownPaidRequests === 0) return "";
  const coverage = summary.spendMeasuredRequests !== null && summary.spendTotalRequests !== null
    ? `Costs recorded for ${formatNumber(summary.spendMeasuredRequests)} of ${formatNumber(summary.spendTotalRequests)} requests.`
    : "We don't know how many request costs were recorded.";
  const unknown = summary.unknownPaidRequests;
  return coverage + (unknown !== null && unknown > 0
    ? ` ${formatNumber(unknown)} paid ${unknown === 1 ? "request has" : "requests have"} unknown cost.` : "");
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
    return <Money amount={receipt.costUsd} summary={summary} precise={summary} />;
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
