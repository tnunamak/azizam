import type { ReactElement, ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  subtitle?: string | null;
  description?: string;
  answer?: string;
  /** One main action; callers keep other actions visually secondary. */
  primaryAction?: ReactElement;
  actions?: ReactNode;
  /** A muted count after the title, e.g. the number of creations. */
  count?: number | null;
  /** The status line or other content under the title row. */
  children?: ReactNode;
  /**
   * Keep a one-line status close under the title, as its subtitle (D1.5:
   * Home's status line floated in a gap between the title and Recent).
   */
  tight?: boolean;
}

export function PageHeader({
  title,
  subtitle,
  description,
  answer,
  primaryAction,
  actions,
  count,
  children,
  tight = false,
}: PageHeaderProps) {
  const legacySubtitle = answer || description;
  const requestedSubtitle = subtitle === undefined ? legacySubtitle : subtitle;
  const normalizedTitle = normalizeCopy(title);
  const normalizedSubtitle = requestedSubtitle
    ? normalizeCopy(requestedSubtitle)
    : "";
  const omitRestatement =
    normalizedSubtitle === normalizedTitle ||
    /^here['’]s how\b/i.test(requestedSubtitle?.trim() ?? "");
  const copy =
    requestedSubtitle?.trim() && !omitRestatement
      ? requestedSubtitle.trim()
      : null;
  const shortSubtitle = copy && wordCount(copy) <= 8 ? copy : null;
  const longSubtitle = copy && wordCount(copy) > 8 ? copy : null;

  return (
    // DESIGN "Spacing and page geometry": 24 from the header to the first
    // section, 4 from the title to a one-line subtitle.
    <header className="mb-(--space-title)">
      <div className="flex min-h-8 flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h1 className="text-title text-balance">
            {title}
            {typeof count === "number" ? (
              <span className="text-subtle-foreground figures text-heading ml-2.5 font-normal">
                {count}
              </span>
            ) : null}
          </h1>
          {shortSubtitle && (
            <p className="text-muted-foreground text-meta mt-1 max-w-2xl">
              {shortSubtitle}
            </p>
          )}
          {longSubtitle && (
            <details className="text-muted-foreground text-meta mt-1 max-w-2xl">
              <summary className="flex min-h-8 w-fit cursor-pointer items-center max-sm:min-h-11">
                About this page
              </summary>
              <p className="mt-2">{longSubtitle}</p>
            </details>
          )}
        </div>
        {(primaryAction || actions) && (
          <div className="flex flex-wrap items-center gap-2">
            {actions}
            {primaryAction}
          </div>
        )}
      </div>
      {children ? (
        <div className={tight ? "mt-1" : "mt-4"}>{children}</div>
      ) : null}
    </header>
  );
}

function wordCount(value: string): number {
  return value.split(/\s+/).filter(Boolean).length;
}

function normalizeCopy(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}
