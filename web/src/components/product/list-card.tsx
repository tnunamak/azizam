import Link from "next/link";
import { Fragment, type ComponentProps, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * The list template's one container (DESIGN "List, table and card anatomy";
 * Vercel Members, Linear Security, Mistral keys): a bordered card with no
 * padding of its own, an optional 36 header band in `text-label` muted ink,
 * and ListRows divided by hairlines.
 */
export function ListCard({
  header,
  children,
  label,
  labelledBy,
  className,
}: {
  /** The header band: column names or a short label. */
  header?: ReactNode;
  children: ReactNode;
  /** The list's accessible name when no heading names it. */
  label?: string;
  /** The id of the heading that names the list. */
  labelledBy?: string;
  className?: string;
}) {
  return (
    <div
      data-slot="list-card"
      className={cn(
        "bg-card text-card-foreground rounded-panel min-w-0 overflow-hidden border",
        className,
      )}
    >
      {header ? (
        <div
          data-slot="list-card-header"
          className="text-muted-foreground text-label flex h-9 items-center gap-3 border-b px-4"
        >
          {header}
        </div>
      ) : null}
      <ul
        role="list"
        aria-label={label}
        aria-labelledby={labelledBy}
        className="divide-y"
      >
        {children}
      </ul>
    </div>
  );
}

/**
 * One row of a ListCard. One line is 40 tall (48 on a phone); two lines are
 * 60 (64 on a phone): the title in `text-body` 500 and the meta line in
 * `text-meta` muted, 2 apart. `leading` is a 32 avatar or mark or a 16 icon
 * in ink, 12 from the text. `facts` are trailing `text-meta` columns on
 * wider screens; on a phone they join the meta line with " · ", which stays
 * one line and never wraps into a third. `trailing` holds a 32 row action.
 * With `href` the whole row opens it, and `trailing` stays clickable.
 * `factClassName` gives every fact column the same width, so a page can line
 * its header band up with them (`listFactClass` is the shared default).
 */
export function ListRow({
  lines = 1,
  leading,
  title,
  meta,
  facts,
  trailing,
  href,
  dense = false,
  factClassName = listFactClass,
  titleAs: Title = "p",
  badge,
  phoneLine = "meta",
  metaTitle,
  className,
  ...rest
}: Omit<ComponentProps<"li">, "title"> & {
  lines?: 1 | 2;
  leading?: ReactNode;
  title: ReactNode;
  /** The second line; an array is joined with " · ". */
  meta?: ReactNode | ReactNode[];
  /** Trailing columns on wider screens, folded into the meta line on a phone. */
  facts?: ReactNode[];
  trailing?: ReactNode;
  href?: string;
  /** A 36 one-line row, for long dense lists. */
  dense?: boolean;
  /** The width of each fact column (default `listFactClass`). */
  factClassName?: string;
  /** A heading level when each row names a thing pages link to (an app). */
  titleAs?: "p" | "h2" | "h3";
  /** One StateBadge beside the title, for a state that is not the usual one. */
  badge?: ReactNode;
  /**
   * The phone's second line: the meta then the facts ("meta"), or the facts
   * alone when they matter more there ("facts": People's "Builder · just now").
   */
  phoneLine?: "meta" | "facts";
  /** The full text of a second line that may be cut, shown on hover. */
  metaTitle?: string;
  className?: string;
}) {
  const metaParts = (
    Array.isArray(meta) ? meta : meta == null ? [] : [meta]
  ).filter((part) => part !== null && part !== undefined && part !== false);
  // Columns keep their place on wider screens (an empty fact leaves a blank
  // column under its header); the phone line joins only the facts present.
  const factColumns = facts ?? [];
  const factParts = factColumns.filter(
    (part) => part !== null && part !== undefined && part !== false,
  );
  const twoLines = lines === 2;
  const height = twoLines
    ? "min-h-(--row-2) max-sm:min-h-(--row-phone-2)"
    : dense
      ? "min-h-(--row-1-dense) max-sm:min-h-(--row-phone-1)"
      : "min-h-(--row-1) max-sm:min-h-(--row-phone-1)";
  const titleNode = href ? (
    <Link
      href={href}
      className="rounded-control after:absolute after:inset-0 after:content-[''] hover:underline"
    >
      {title}
    </Link>
  ) : (
    title
  );
  // On a phone the facts join the second line; a one-line row gains no line.
  const phoneFacts = twoLines ? factParts : [];
  return (
    <li
      {...rest}
      data-slot="list-row"
      data-lines={lines}
      className={cn(
        // The row's minimum height (which includes its hairline) sets its
        // size; 6 px of padding only keeps text off the edges.
        "relative flex items-center gap-3 px-4 py-1.5",
        href && "hover:bg-muted/50 transition-colors duration-(--motion-fast)",
        height,
        className,
      )}
    >
      {leading ? (
        <span
          data-slot="list-row-leading"
          className="text-muted-foreground flex shrink-0 items-center [&_svg:not([class*='size-'])]:size-4"
        >
          {leading}
        </span>
      ) : null}
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <Title className="text-body min-w-0 truncate font-medium">
            {titleNode}
          </Title>
          {badge ? <span className="flex shrink-0">{badge}</span> : null}
        </div>
        {twoLines && (metaParts.length || phoneFacts.length) ? (
          <p
            data-slot="list-row-meta"
            title={metaTitle}
            className="text-muted-foreground text-meta mt-0.5 truncate"
          >
            <span
              className={
                phoneLine === "facts" && phoneFacts.length
                  ? "max-sm:hidden"
                  : undefined
              }
            >
              <Joined parts={metaParts} />
            </span>
            {phoneFacts.length ? (
              <span className="sm:hidden">
                {metaParts.length && phoneLine === "meta" ? " · " : null}
                <Joined parts={phoneFacts} />
              </span>
            ) : null}
          </p>
        ) : null}
      </div>
      {factColumns.length ? (
        <div
          data-slot="list-row-facts"
          className={cn(
            "text-muted-foreground text-meta figures flex shrink-0 items-center gap-4",
            twoLines && "max-sm:hidden",
          )}
        >
          {factColumns.map((fact, index) => (
            <span key={index} className={cn("truncate", factClassName)}>
              {fact}
            </span>
          ))}
        </div>
      ) : null}
      {trailing ? (
        <div
          data-slot="list-row-trailing"
          className="relative z-10 flex shrink-0 items-center gap-2"
        >
          {trailing}
        </div>
      ) : null}
    </li>
  );
}

/** The default fact column: 128 wide, so a header band can match it. */
export const listFactClass = "w-32";

function Joined({ parts }: { parts: ReactNode[] }) {
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {index ? " · " : null}
          {part}
        </Fragment>
      ))}
    </>
  );
}
