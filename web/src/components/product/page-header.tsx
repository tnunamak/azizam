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
    <header className="mb-8">
      <div className="flex min-h-10 flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight text-balance">
            {title}
            {typeof count === "number" ? (
              <span className="text-subtle-foreground figures ml-2.5 text-lg font-normal">
                {count}
              </span>
            ) : null}
          </h1>
          {shortSubtitle && (
            <p className="text-muted-foreground mt-2 max-w-2xl text-sm">
              {shortSubtitle}
            </p>
          )}
          {longSubtitle && (
            <details className="text-muted-foreground mt-2 max-w-2xl text-sm">
              <summary className="min-h-11 w-fit cursor-pointer py-3">
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
      {children ? <div className="mt-4">{children}</div> : null}
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
