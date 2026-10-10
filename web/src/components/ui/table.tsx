"use client";

import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * DESIGN "List, table and card anatomy": a 36 header in `text-label` subtle
 * ink with no fill, 40 rows (36 with `dense`, for Usage and Activity), and
 * `numeric` cells right-aligned in tabular figures.
 */
function Table({
  className,
  dense = false,
  ...props
}: React.ComponentProps<"table"> & { dense?: boolean }) {
  // A table wider than its container scrolls sideways; keyboard users can
  // only scroll a region they can focus (WCAG 2.1.1, axe
  // scrollable-region-focusable), so the container takes focus only then.
  const containerRef = React.useRef<HTMLDivElement>(null);
  const [scrolls, setScrolls] = React.useState(false);
  React.useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const update = () =>
      setScrolls(container.scrollWidth > container.clientWidth + 1);
    const observer = new ResizeObserver(update);
    observer.observe(container);
    if (container.firstElementChild) observer.observe(container.firstElementChild);
    update();
    return () => observer.disconnect();
  }, []);
  return (
    <div
      ref={containerRef}
      data-slot="table-container"
      tabIndex={scrolls ? 0 : undefined}
      className="relative w-full overflow-x-auto"
    >
      <table
        data-slot="table"
        data-dense={dense ? "" : undefined}
        className={cn("group/table text-meta w-full caption-bottom", className)}
        {...props}
      />
    </div>
  );
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b", className)}
      {...props}
    />
  );
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  );
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "bg-muted/50 border-t font-medium [&>tr]:last:border-b-0",
        className,
      )}
      {...props}
    />
  );
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted border-b transition-colors",
        className,
      )}
      {...props}
    />
  );
}

function TableHead({
  className,
  numeric = false,
  ...props
}: React.ComponentProps<"th"> & { numeric?: boolean }) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "text-subtle-foreground text-label h-9 px-3 text-left align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        numeric && tableNumeric,
        className,
      )}
      {...props}
    />
  );
}

function TableCell({
  className,
  numeric = false,
  ...props
}: React.ComponentProps<"td"> & { numeric?: boolean }) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "h-10 px-3 py-1.5 align-middle whitespace-nowrap group-data-dense/table:h-9 [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        numeric && tableNumeric,
        className,
      )}
      {...props}
    />
  );
}

/** Right-aligned tabular figures, for counts, money and times. */
const tableNumeric = "figures text-right";

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("text-muted-foreground text-meta mt-4", className)}
      {...props}
    />
  );
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
  tableNumeric,
};
