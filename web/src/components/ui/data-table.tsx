"use client";

import type { ReactNode } from "react";

import { formatCellValue, isEpochSeconds } from "@/lib/format";
import { cn } from "@/lib/cn";
import { EmptyState } from "./empty-state";
import { RelativeTime } from "./relative-time";
import { Skeleton } from "./skeleton";

export interface DataTableColumn<T> {
  /** Stable id; doubles as the property accessor when `value`/`cell` are
   * omitted (e.g. id "name" reads row.name). */
  id: string;
  header: ReactNode;
  /** Full custom renderer (EntityRef, Badge, SecretMask, …). */
  cell?: (row: T) => ReactNode;
  /** Raw value accessor for the default formatter when `cell` is omitted. */
  value?: (row: T) => unknown;
  align?: "left" | "right";
  /** Skip this column in the mobile card view. */
  hideOnMobile?: boolean;
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  /** Skeleton rows while fetching. */
  loading?: boolean;
  skeletonRows?: number;
  /** EmptyState slot shown when not loading and there are no rows. */
  empty?: ReactNode;
  /** Per-row overflow menu slot — the one place for row actions. */
  rowMenu?: (row: T) => ReactNode;
  /** Accessible table caption (visually hidden). */
  label?: string;
  className?: string;
}

/** Default cell rendering routes through the shared formatters: epoch-second
 * numbers become <RelativeTime> (law #1 — no raw timestamps slip through a
 * lazy column), everything else through formatCellValue. */
function defaultCell(value: unknown): ReactNode {
  if (isEpochSeconds(value)) {
    return <RelativeTime ts={value} />;
  }
  const text = formatCellValue(value);
  return text === "—" ? <span className="text-vf-subtle">—</span> : text;
}

function renderCell<T>(column: DataTableColumn<T>, row: T): ReactNode {
  if (column.cell) return column.cell(row);
  const value = column.value
    ? column.value(row)
    : (row as Record<string, unknown>)[column.id];
  return defaultCell(value);
}

const HEADER_CELL =
  "px-3 py-2 font-mono text-vf-micro font-normal tracking-wider uppercase text-vf-subtle";

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  skeletonRows = 5,
  empty,
  rowMenu,
  label,
  className,
}: DataTableProps<T>) {
  const isEmpty = !loading && rows.length === 0;
  const [primary, ...rest] = columns;
  const mobileDetailColumns = rest.filter((column) => !column.hideOnMobile);

  return (
    <div className={cn("w-full", className)} aria-busy={loading || undefined}>
      {/* Desktop: real table */}
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full border-collapse">
          {label && <caption className="sr-only">{label}</caption>}
          <thead>
            <tr className="border-vf-border border-b">
              {columns.map((column) => (
                <th
                  key={column.id}
                  scope="col"
                  className={cn(
                    HEADER_CELL,
                    column.align === "right" ? "text-right" : "text-left",
                  )}
                >
                  {column.header}
                </th>
              ))}
              {rowMenu && (
                <th scope="col" className={cn(HEADER_CELL, "w-10")}>
                  <span className="sr-only">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: skeletonRows }, (_, i) => (
                  <tr key={i} className="border-vf-border/60 border-b">
                    {columns.map((column) => (
                      <td key={column.id} className="px-3 py-3">
                        <Skeleton className="h-4 w-24" />
                      </td>
                    ))}
                    {rowMenu && (
                      <td className="px-3 py-3">
                        <Skeleton className="size-4" />
                      </td>
                    )}
                  </tr>
                ))
              : rows.map((row) => (
                  <tr
                    key={rowKey(row)}
                    className="border-vf-border/60 hover:bg-vf-surface-1 border-b transition-colors"
                  >
                    {columns.map((column) => (
                      <td
                        key={column.id}
                        className={cn(
                          "text-vf-small text-vf-text px-3 py-3 align-middle",
                          column.align === "right" && "text-right",
                        )}
                      >
                        {renderCell(column, row)}
                      </td>
                    ))}
                    {rowMenu && (
                      <td className="px-3 py-2 text-right align-middle">
                        {rowMenu(row)}
                      </td>
                    )}
                  </tr>
                ))}
          </tbody>
        </table>
      </div>

      {/* Mobile: card collapse (CSS-only switch) */}
      <div className="flex flex-col gap-2 md:hidden">
        {loading
          ? Array.from({ length: Math.min(skeletonRows, 3) }, (_, i) => (
              <div
                key={i}
                className="rounded-vf-md border-vf-border bg-vf-surface-1 border p-4"
              >
                <Skeleton className="h-4 w-32" />
                <Skeleton className="mt-3 h-3 w-full" />
                <Skeleton className="mt-2 h-3 w-2/3" />
              </div>
            ))
          : rows.map((row) => (
              <div
                key={rowKey(row)}
                className="rounded-vf-md border-vf-border bg-vf-surface-1 border p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="text-vf-body text-vf-text min-w-0">
                    {primary ? renderCell(primary, row) : null}
                  </div>
                  {rowMenu && <div className="shrink-0">{rowMenu(row)}</div>}
                </div>
                {mobileDetailColumns.length > 0 && (
                  <dl className="mt-3 flex flex-col gap-1.5">
                    {mobileDetailColumns.map((column) => (
                      <div
                        key={column.id}
                        className="flex items-baseline justify-between gap-3"
                      >
                        <dt className="text-vf-micro text-vf-subtle shrink-0 font-mono tracking-wider uppercase">
                          {column.header}
                        </dt>
                        <dd className="text-vf-small text-vf-text min-w-0 text-right">
                          {renderCell(column, row)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </div>
            ))}
      </div>

      {isEmpty && (empty ?? <EmptyState>Nothing to show yet.</EmptyState>)}
    </div>
  );
}
