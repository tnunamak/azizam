"use client";

/**
 * Form pieces the Add engine dialog and the engine page share. A label with a
 * tag beside it (Suggested, Default 5 s, Owner only), help or an error below,
 * and the id wiring for the control.
 */
import { useId, type ComponentProps, type ReactNode } from "react";
import { CircleAlert, CircleCheck, Info, Lock } from "lucide-react";
import { cn } from "@/lib/cn";

export interface ControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

export function FormField({
  label,
  required,
  tag,
  help,
  error,
  after,
  className,
  children,
}: {
  label: ReactNode;
  required?: boolean;
  tag?: ReactNode;
  help?: ReactNode;
  error?: ReactNode;
  /** Shown under help or error: a call line, a finding. */
  after?: ReactNode;
  className?: string;
  children: (control: ControlProps) => ReactNode;
}) {
  const id = useId();
  const describedBy = error ? `${id}-error` : help ? `${id}-help` : undefined;
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <label htmlFor={id} className="text-meta text-foreground font-medium">
          {label}
          {required && (
            <span aria-hidden="true" className="text-danger">
              {" *"}
            </span>
          )}
        </label>
        {tag}
      </div>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {help && !error && (
        <p id={`${id}-help`} className="text-meta text-muted-foreground">
          {help}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-meta text-danger">
          {error}
        </p>
      )}
      {after}
    </div>
  );
}

export function Tag({
  tone = "neutral",
  children,
  className,
}: {
  tone?: "neutral" | "suggest" | "changed";
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "rounded-badge text-badge inline-flex h-5 shrink-0 items-center gap-1 px-1.5 whitespace-nowrap",
        tone === "suggest" && "bg-info-tint text-info",
        tone === "changed" && "bg-warning-tint text-warning",
        tone === "neutral" && "bg-muted text-muted-foreground",
        className,
      )}
    >
      {children}
    </span>
  );
}

export function OwnerOnlyTag() {
  return (
    <Tag>
      <Lock aria-hidden="true" className="size-3" />
      Owner only
    </Tag>
  );
}

export function NativeSelect({ className, ...props }: ComponentProps<"select">) {
  return (
    <select
      className={cn(
        "border-input bg-card text-body rounded-control h-10 w-full min-w-0 border px-3 disabled:cursor-not-allowed disabled:opacity-50 max-sm:h-11",
        className,
      )}
      {...props}
    />
  );
}

const noteTone = {
  info: { icon: Info, className: "bg-info-tint/60 text-foreground", iconClass: "text-info" },
  ok: { icon: CircleCheck, className: "bg-success-tint text-success", iconClass: "text-success" },
  warn: { icon: CircleAlert, className: "bg-warning-tint text-foreground", iconClass: "text-warning" },
  plain: { icon: Info, className: "bg-muted text-foreground", iconClass: "text-muted-foreground" },
  lock: { icon: Lock, className: "bg-muted text-foreground", iconClass: "text-muted-foreground" },
} as const;

/** A one-line note with an icon: OK lines, warnings under a row, info. */
export function Note({
  tone = "plain",
  role,
  children,
  className,
}: {
  tone?: keyof typeof noteTone;
  role?: "status" | "alert";
  children: ReactNode;
  className?: string;
}) {
  const t = noteTone[tone];
  const Icon = t.icon;
  return (
    <div role={role} className={cn("text-meta rounded-control flex items-start gap-2 px-3 py-2", t.className, className)}>
      <Icon aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", t.iconClass)} />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** A button that looks like a link: "Change kind", "Paste the key here instead". */
export function LinkButton({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn("text-link text-meta w-fit cursor-pointer underline-offset-4 hover:underline max-sm:min-h-11", className)}
      {...props}
    />
  );
}

export const inputClass =
  "border-input bg-card text-body placeholder:text-muted-foreground rounded-control h-10 w-full min-w-0 border px-3 disabled:cursor-not-allowed disabled:opacity-50 max-sm:h-11 aria-invalid:border-destructive";
