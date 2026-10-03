"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  Check,
  Copy,
  GitBranch,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  CapabilityIcon,
  type Capability,
} from "@/components/product/capability-icon";
import {
  CodeBlock,
  EmptyState,
  ErrorState,
  PageHeader,
} from "@/components/product";
import { FilterToggle } from "@/components/product/filter-toggle";

import {
  readJson,
  type RecentAsset,
  type Workflow as WorkflowRow,
  type WorkflowInput,
  type WorkflowCatalog as WorkflowsCatalog,
} from "./workflow-api";
import { RelativeTime } from "@/components/product";
type WorkflowCapability = Capability | "audio" | "workflow";
const EMPTY_WORKFLOWS: WorkflowRow[] = [];

const capabilityOrder: WorkflowCapability[] = [
  "image",
  "video",
  "audio",
  "workflow",
];

async function fetchWorkflows(): Promise<WorkflowsCatalog> {
  const response = await fetch("/me/api/workflows", {
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error("Workflows catalog could not be loaded. Try again.");
  }
  const payload = (await response.json()) as unknown;
  if (!isCatalog(payload)) {
    throw new Error("Workflows catalog response is not in the expected shape.");
  }
  return payload;
}

function isCatalog(value: unknown): value is WorkflowsCatalog {
  if (!value || typeof value !== "object") return false;
  const maybe = value as Partial<WorkflowsCatalog>;
  return maybe.schema_version === 1 && Array.isArray(maybe.workflows);
}

function capabilityLabel(value: string) {
  return (
    {
      image: "Images",
      video: "Video",
      voice: "Voice",
      audio: "Audio",
      workflow: "Workflows",
      chat: "Chat",
      transcribe: "Transcribe",
      decide: "Decide",
      embed: "Embed",
    }[value as WorkflowCapability] ?? value
  );
}

function iconCapability(value: string): Capability {
  return value === "audio"
    ? "voice"
    : [
          "image",
          "video",
          "voice",
          "chat",
          "transcribe",
          "decide",
          "embed",
        ].includes(value)
      ? (value as Capability)
      : "image";
}

function timeLabel(value: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "Not measured yet";
  }
  if (value < 1) return `~${Math.round(value * 1000)} ms`;
  return `~${value.toFixed(value >= 10 ? 0 : 1)} s`;
}

function rangeLabel(input: WorkflowInput) {
  if (input.enum?.length) return input.enum.map(String).join(", ");
  if (input.minimum != null && input.maximum != null) {
    return `${input.minimum} to ${input.maximum}`;
  }
  if (input.minimum != null) return `At least ${input.minimum}`;
  if (input.maximum != null) return `At most ${input.maximum}`;
  return "";
}

function valueLabel(value: unknown) {
  if (value === undefined || value === null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function matches(row: WorkflowRow, query: string) {
  if (!query) return true;
  const haystack = [
    row.id,
    row.title,
    row.description,
    ...row.aliases,
    row.engine_binding ?? "",
    ...row.inputs.map((input) => input.name),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function statusClass(row: WorkflowRow) {
  if (row.availability.status === "ready") return "bg-success";
  if (row.availability.status === "busy") return "bg-warning";
  if (row.availability.status === "starting") return "bg-info";
  return "bg-muted-foreground";
}

function WorkflowCard({
  workflow,
  onDetail,
}: {
  workflow: WorkflowRow;
  onDetail: (workflow: WorkflowRow) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  async function copyId() {
    try {
      await navigator.clipboard.writeText(workflow.id);
      setCopied(true);
      setCopyError(false);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopyError(true);
    }
  }
  return (
    <article className="bg-card text-card-foreground flex min-w-0 flex-col gap-5 rounded-xl border p-5">
      <div className="flex min-w-0 items-start gap-3">
        <CapabilityIcon capability={iconCapability(workflow.capability)} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-semibold">{workflow.title}</h3>
          <p className="text-muted-foreground mt-1 line-clamp-2 text-sm">
            {workflow.description ||
              "Workflow manifest visible to your access plan."}
          </p>
        </div>
      </div>
      <button
        type="button"
        aria-label={`Copy workflow id for ${workflow.title}`}
        onClick={copyId}
        className="bg-muted hover:bg-muted/80 focus-visible:ring-ring flex min-h-11 min-w-0 items-center justify-between gap-2 rounded-md px-3 py-2 text-left font-mono text-xs transition outline-none focus-visible:ring-[3px]"
      >
        <span className="truncate">{workflow.id}</span>
        {copied ? (
          <Check className="size-4 shrink-0" />
        ) : (
          <Copy className="size-4 shrink-0" />
        )}
      </button>
      {copyError && (
        <p role="alert" className="text-destructive text-sm">
          Copy failed. Select the workflow id to copy it manually.
        </p>
      )}
      <span role="status" className="sr-only">
        {copied ? "Workflow id copied" : ""}
      </span>
      <div className="flex flex-wrap gap-2">
        <Badge variant="secondary">
          {capabilityLabel(workflow.capability)}
        </Badge>
        <Badge variant="outline">{timeLabel(workflow.typical_seconds)}</Badge>
        {workflow.aliases.slice(0, 2).map((alias) => (
          <Badge key={alias} variant="outline">
            {alias}
          </Badge>
        ))}
      </div>
      <div className="flex min-w-0 items-start gap-2 text-sm">
        <span
          aria-hidden="true"
          className={cn(
            "mt-1.5 size-2 shrink-0 rounded-full",
            statusClass(workflow),
          )}
        />
        <div className="min-w-0">
          <div className="font-medium">
            {workflow.availability.status.charAt(0).toUpperCase() +
              workflow.availability.status.slice(1).replaceAll("_", " ")}
          </div>
          <p className="text-muted-foreground line-clamp-2 text-xs">
            {workflow.availability.reason}
          </p>
        </div>
      </div>
      <div className="mt-auto flex flex-wrap gap-2">
        <Button asChild size="sm">
          <Link href={`/try/?workflow=${encodeURIComponent(workflow.id)}`}>
            Run in Try
          </Link>
        </Button>
        <Button variant="outline" size="sm" onClick={() => onDetail(workflow)}>
          Details
        </Button>
      </div>
    </article>
  );
}

function LoadingWorkflows() {
  return (
    <div aria-label="Loading workflows" aria-busy="true" role="status">
      <div className="mb-6 flex flex-wrap gap-3">
        <Skeleton className="h-11 w-56" />
        <Skeleton className="h-11 w-28" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-64 rounded-xl" />
        ))}
      </div>
    </div>
  );
}

function DetailSheet({
  workflow,
  onOpenChange,
}: {
  workflow: WorkflowRow | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Sheet open={!!workflow} onOpenChange={onOpenChange}>
      <SheetContent className="w-full overflow-y-auto sm:!max-w-5xl [&_a[data-slot=button]]:min-h-11 [&_button]:min-h-11">
        {workflow && (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle>{workflow.title}</SheetTitle>
              <SheetDescription>
                {workflow.description || workflow.id}
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-6 p-4">
              <div className="grid grid-cols-2 gap-4 text-sm">
                <Fact label="Workflow" value={workflow.id} />
                <Fact
                  label="Capability"
                  value={capabilityLabel(workflow.capability)}
                />
                <Fact
                  label="Engine binding"
                  value={
                    workflow.engine_binding ?? "No engine binding configured"
                  }
                />
                <Fact
                  label="Typical time"
                  value={timeLabel(workflow.typical_seconds)}
                />
                <Fact
                  label="Native route"
                  value={
                    workflow.native_route ??
                    "No native passthrough route configured"
                  }
                />
                <Fact
                  label="Discovery"
                  value={
                    <a
                      className="underline underline-offset-4"
                      href={workflow.discovery_url}
                    >
                      Machine-readable workflow
                    </a>
                  }
                />
              </div>
              <div>
                <h3 className="font-medium">Stable aliases</h3>
                <div className="mt-2 flex flex-wrap gap-2">
                  {workflow.aliases.length ? (
                    workflow.aliases.map((alias) => (
                      <Badge key={alias} variant="secondary">
                        {alias}
                      </Badge>
                    ))
                  ) : (
                    <p className="text-muted-foreground text-sm">
                      No stable aliases point here.
                    </p>
                  )}
                </div>
              </div>
              <div>
                <h3 className="font-medium">Typed inputs</h3>
                <p className="text-muted-foreground mt-1 text-sm">
                  Applies inputs change the request. Ignored inputs have no
                  effect on this route.
                </p>
                <p className="text-muted-foreground mt-2 text-xs sm:hidden">
                  Scroll sideways to see every column.
                </p>
                <div
                  className="mt-3 max-w-full overflow-x-auto rounded-lg border [&>div]:overflow-visible"
                  tabIndex={0}
                  aria-label="Scrollable typed controls"
                >
                  <Table className="min-w-[40rem] table-fixed">
                    <TableHeader>
                      <TableRow>
                        <TableHead>Name</TableHead>
                        <TableHead>Type</TableHead>
                        <TableHead>Default</TableHead>
                        <TableHead>Range</TableHead>
                        <TableHead>Applies</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {workflow.inputs.map((input) => (
                        <TableRow key={input.name}>
                          <TableCell className="font-mono text-xs break-all !whitespace-normal">
                            {input.name}
                            {input.required && (
                              <span className="text-destructive ml-1">*</span>
                            )}
                          </TableCell>
                          <TableCell>{input.type}</TableCell>
                          <TableCell className="max-w-48 break-words !whitespace-normal">
                            {valueLabel(input.default)}
                          </TableCell>
                          <TableCell className="max-w-48 break-words !whitespace-normal">
                            {rangeLabel(input)}
                          </TableCell>
                          <TableCell>
                            <Badge
                              variant={
                                !input.accepted ? "outline" : "secondary"
                              }
                            >
                              {input.accepted ? "Applies" : "Ignored"}
                            </Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
              <div>
                <h3 className="font-medium">Example request</h3>
                <div className="mt-3 [&_pre]:overflow-visible [&_pre]:break-all [&_pre]:whitespace-pre-wrap">
                  <CodeBlock
                    snippets={[
                      { language: "curl", code: workflow.examples.curl },
                      { language: "Python", code: workflow.examples.python },
                      {
                        language: "JavaScript",
                        code: workflow.examples.javascript,
                      },
                    ]}
                  />
                </div>
              </div>
              <RecentJobs key={workflow.id} workflow={workflow} />
              <div className="flex flex-wrap gap-2">
                <Button asChild>
                  <Link
                    href={`/try/?workflow=${encodeURIComponent(workflow.id)}`}
                  >
                    Run in Try
                  </Link>
                </Button>
                <Button asChild variant="outline">
                  <a href={"/me/api/workflows"}>
                    Discovery list
                    <ArrowUpRight aria-hidden="true" />
                  </a>
                </Button>
              </div>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-muted-foreground text-xs">{label}</div>
      <div className="mt-1 font-medium break-words">{value}</div>
    </div>
  );
}

function RecentJobs({ workflow }: { workflow: WorkflowRow }) {
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["workflow-media", workflow.id],
    queryFn: () =>
      readJson<{ assets: RecentAsset[] }>(
        `/me/api/media?${new URLSearchParams({ workflow: workflow.id, limit: "5" })}`,
      ),
    retry: false,
  });
  return (
    <section aria-label="Recent jobs">
      <h3 className="font-medium">Recent jobs</h3>
      <p className="text-muted-foreground mt-1 text-sm">
        Your latest outputs from this workflow, with their original request
        details.
      </p>
      {isPending ? (
        <Skeleton
          role="status"
          aria-label="Loading recent jobs"
          className="mt-4 h-24"
        />
      ) : isError ? (
        <div className="mt-4">
          <ErrorState
            message="Recent jobs could not be loaded. Try again."
            onRetry={() => void refetch()}
            retrying={isFetching}
          />
        </div>
      ) : !data?.assets.length ? (
        <p className="text-muted-foreground mt-4 text-sm">
          No recent jobs yet. Run this workflow in Try to create your first
          result.
        </p>
      ) : (
        <ul className="mt-4 divide-y">
          {data.assets.map((asset) => (
            <li key={asset.asset_id} className="space-y-2 py-4 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {asset.kind.charAt(0).toUpperCase() + asset.kind.slice(1)}{" "}
                  output
                </span>
                <RelativeTime
                  ts={asset.created_at}
                  fallback="Time not recorded"
                  className="text-muted-foreground text-xs"
                />
              </div>
              <p className="break-words">
                {asset.prompt || "Prompt not recorded"}
              </p>
              <dl className="text-muted-foreground grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                <dt>Model</dt>
                <dd className="break-all">{asset.model || "Not recorded"}</dd>
                <dt>Workflow</dt>
                <dd className="break-all">
                  {asset.workflow || "Not recorded"}
                </dd>
                <dt>Duration</dt>
                <dd>
                  {asset.generation_ms == null
                    ? "Not measured yet"
                    : `${(asset.generation_ms / 1000).toFixed(1)} s`}
                </dd>
              </dl>
              {asset.download_url && (
                <Button variant="outline" size="sm" asChild>
                  <a href={asset.download_url} download>
                    Download output
                  </a>
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function WorkflowsPage() {
  const [query, setQuery] = useState("");
  const [capability, setCapability] = useState<WorkflowCapability | "all">(
    "all",
  );
  const [detail, setDetail] = useState<WorkflowRow | null>(null);
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["workflows", "catalog"],
    queryFn: fetchWorkflows,
    retry: false,
  });
  const rows = data?.workflows ?? EMPTY_WORKFLOWS;
  const availableCapabilities = capabilityOrder.filter((cap) =>
    rows.some((row) => row.capability === cap),
  );
  const filtered = useMemo(
    () =>
      rows.filter(
        (row) =>
          matches(row, query) &&
          (capability === "all" || row.capability === capability),
      ),
    [rows, query, capability],
  );
  const grouped = capabilityOrder
    .map((cap) => ({
      capability: cap,
      rows: filtered.filter((row) => row.capability === cap),
    }))
    .filter((group) => group.rows.length > 0);

  return (
    <div className="space-y-8 [&_a[data-slot=button]]:min-h-11 [&_button]:min-h-11">
      <PageHeader
        title="Workflows"
        answer="Which workflow should I run?"
        actions={
          isFetching ? (
            <Badge variant="outline">Refreshing</Badge>
          ) : data ? (
            <Badge variant="outline">
              {data.counts?.workflows ?? rows.length} workflows
            </Badge>
          ) : undefined
        }
      />
      {isPending ? (
        <LoadingWorkflows />
      ) : isError ? (
        <ErrorState
          message="Workflows catalog could not be loaded. Try again."
          onRetry={() => void refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={GitBranch}
          title="No workflows available"
          description="Workflows appear here when configured and allowed by your access plan. Ask the gateway owner to connect a workflow engine and grant access."
          action={
            <Button variant="outline" asChild>
              <Link href="/models">Explore models</Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <label className="relative min-w-0 flex-1 lg:max-w-md">
              <Search
                aria-hidden="true"
                className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search workflows"
                className="pl-9"
              />
              <span className="sr-only">Search workflows</span>
            </label>
            <div className="flex flex-wrap gap-2">
              <FilterToggle
                pressed={capability === "all"}
                size="sm"
                onClick={() => setCapability("all")}
              >
                <SlidersHorizontal aria-hidden="true" />
                All
              </FilterToggle>
              {availableCapabilities.map((cap) => (
                <FilterToggle
                  key={cap}
                  pressed={capability === cap}
                  size="sm"
                  onClick={() => setCapability(cap)}
                >
                  {capabilityLabel(cap)}
                </FilterToggle>
              ))}
            </div>
          </div>
          {filtered.length === 0 ? (
            <EmptyState
              icon={Search}
              title="No matching workflows"
              description="Clear search or choose another capability."
              action={
                <Button
                  variant="outline"
                  onClick={() => {
                    setQuery("");
                    setCapability("all");
                  }}
                >
                  Clear search
                </Button>
              }
            />
          ) : (
            <div
              aria-label="Available workflows"
              className={cn(
                "grid gap-6",
                filtered.length > 1 && "sm:grid-cols-2",
                filtered.length > 2 && "lg:grid-cols-3",
              )}
            >
              {grouped.map((group) => (
                <section
                  key={group.capability}
                  aria-label={`${capabilityLabel(group.capability)} workflows`}
                  className={cn(
                    "min-w-0 space-y-3",
                    group.rows.length > 1 && "sm:col-span-2",
                    group.rows.length > 2 && "lg:col-span-3",
                  )}
                >
                  <h2 className="text-lg font-semibold">
                    {capabilityLabel(group.capability)}
                  </h2>
                  <div
                    className={cn(
                      "grid gap-4",
                      group.rows.length > 1 && "sm:grid-cols-2",
                      group.rows.length > 2 && "lg:grid-cols-3",
                    )}
                  >
                    {group.rows.map((workflow) => (
                      <WorkflowCard
                        key={workflow.id}
                        workflow={workflow}
                        onDetail={setDetail}
                      />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </>
      )}
      <DetailSheet
        workflow={detail}
        onOpenChange={(open) => !open && setDetail(null)}
      />
    </div>
  );
}
