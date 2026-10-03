"use client";

import type { ReactNode } from "react";

import { formatCellValue, isEpochSeconds } from "@/lib/format";
import { cn } from "@/lib/cn";
import { EmptyState } from "./empty-state";
import { RelativeTime } from "./relative-time";
import { Skeleton } from "./legacy/skeleton";

export interface DataTableColumn<T> {
  /** Stable id; doubles as the property accessor when `value`/`cell` are
   * omitted (e.g. id "name" reads row.name). */
  id: string;
  header: ReactNode;
  /** Full custom renderer (EntityRef, Badge, SecretMask, …). Native child
   * controls remain usable in the mobile-card representation. */
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
  /** Called by the desktop primary action and the mobile card's primary button
   * (not row-menu or semantic child controls). */
  onRowClick?: (row: T) => void;
  /** Accessible name for the mobile card's real primary action. */
  mobileRowActionLabel?: (row: T) => string;
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
  "px-3 py-2 font-mono text-vf-micro font-normal uppercase text-vf-subtle";

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
  onRowClick,
  mobileRowActionLabel,
}: DataTableProps<T>) {
  const isEmpty = !loading && rows.length === 0;
  const [primary, ...rest] = columns;
  const mobileDetailColumns = rest.filter((column) => !column.hideOnMobile);

  return (
    <div className={cn("w-full", className)} aria-busy={loading || undefined}>
      {/* Desktop: real table */}
      <div className="vf-table-wrap vf-desktop-only">
        <table className="vf-table">
          {label && <caption className="sr-only">{label}</caption>}
          <thead>
            <tr>
              {onRowClick && (
                <th scope="col" className={cn(HEADER_CELL, "w-14")}>
                  <span className="sr-only">Open</span>
                </th>
              )}
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
                  <tr
                    key={i}
                    className="border-vf-border/60 border-b last:border-b-0"
                  >
                    {onRowClick && (
                      <td className="px-3 py-3">
                        <Skeleton className="size-8" />
                      </td>
                    )}
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
                    className={cn(
                      "border-vf-border/60 hover:bg-vf-row-hover border-b transition-colors last:border-b-0",
                    )}
                  >
                    {onRowClick && (
                      <td className="px-3 py-2 align-middle">
                        <button
                          type="button"
                          className="vf-touch-target text-vf-small text-vf-accent hover:bg-vf-row-hover rounded-vf-sm px-2 font-medium"
                          onClick={() => onRowClick(row)}
                          aria-label={mobileRowActionLabel?.(row) ?? "Open row"}
                        >
                          Open
                        </button>
                      </td>
                    )}
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
      <div className="vf-mobile-data-stack">
        {loading
          ? Array.from({ length: Math.min(skeletonRows, 3) }, (_, i) => (
              <div key={i} className="vf-mobile-data-card">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="mt-3 h-3 w-full" />
                <Skeleton className="mt-2 h-3 w-2/3" />
              </div>
            ))
          : rows.map((row) => (
              <div
                key={rowKey(row)}
                className={cn(
                  "vf-mobile-data-card",
                  onRowClick && "vf-mobile-data-card-actionable",
                )}
              >
                {onRowClick && (
                  <button
                    type="button"
                    className="vf-mobile-data-card-primary"
                    onClick={() => onRowClick(row)}
                    aria-label={mobileRowActionLabel?.(row) ?? "Open row"}
                  />
                )}
                <div className="vf-mobile-data-card-head">
                  <div className="text-vf-body text-vf-text min-w-0">
                    {primary ? renderCell(primary, row) : null}
                  </div>
                  {rowMenu && (
                    <div className="vf-mobile-data-card-menu shrink-0">
                      {rowMenu(row)}
                    </div>
                  )}
                </div>
                {mobileDetailColumns.length > 0 && (
                  <dl className="vf-mobile-data-card-fields">
                    {mobileDetailColumns.map((column) => (
                      <div
                        key={column.id}
                        className="vf-mobile-data-card-field"
                      >
                        <dt className="vf-mobile-data-card-label">
                          {column.header}
                        </dt>
                        <dd className="vf-mobile-data-card-value">
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
