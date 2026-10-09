import { useId, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * The settings template's section (DESIGN "Page templates"; Linear Security,
 * Vercel Firewall): a `text-heading` 12 above one card of SettingRows with
 * hairlines between them.
 */
export function SettingsSection({
  title,
  description,
  action,
  children,
  id,
  className,
}: {
  title: string;
  /** One line under the heading, in `text-meta`. */
  description?: ReactNode;
  /** One action on the heading row, at 32. */
  action?: ReactNode;
  children: ReactNode;
  id?: string;
  className?: string;
}) {
  const fallbackId = useId();
  const headingId = id ?? `settings-${fallbackId}`;
  return (
    <section
      data-slot="settings-section"
      aria-labelledby={headingId}
      className={cn("min-w-0", className)}
    >
      <div className="mb-(--space-heading) flex min-h-8 items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 id={headingId} className="text-heading">
            {title}
          </h2>
          {description ? (
            <p className="text-muted-foreground text-meta">{description}</p>
          ) : null}
        </div>
        {action}
      </div>
      <div className="bg-card text-card-foreground rounded-panel divide-y border">
        {children}
      </div>
    </section>
  );
}

/**
 * One setting: the label in `text-body` 500, one line of description in
 * `text-meta`, and the control right-aligned at 32. At least 64 tall,
 * padding 12 × 16. On a phone the control drops under the text when the
 * two do not fit side by side, and the description stays one line.
 */
export function SettingRow({
  label,
  description,
  control,
  htmlFor,
  className,
}: {
  label: ReactNode;
  description?: ReactNode;
  control?: ReactNode;
  /** The id of the control the label names, when it is a form field. */
  htmlFor?: string;
  className?: string;
}) {
  const Label = htmlFor ? "label" : "p";
  return (
    <div
      data-slot="setting-row"
      className={cn(
        "flex min-h-(--row-setting) flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3",
        className,
      )}
    >
      <div className="min-w-0 flex-1 basis-60">
        <Label
          {...(htmlFor ? { htmlFor } : {})}
          className="text-body block font-medium"
        >
          {label}
        </Label>
        {description ? (
          <p className="text-muted-foreground text-meta mt-0.5 truncate">
            {description}
          </p>
        ) : null}
      </div>
      {control ? (
        <div
          data-slot="setting-row-control"
          className="flex shrink-0 items-center gap-2"
        >
          {control}
        </div>
      ) : null}
    </div>
  );
}
