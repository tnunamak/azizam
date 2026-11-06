"use client";
import { createContext, useContext, useId, type ReactNode } from "react";
import {
  AlertTriangle,
  ChevronRight,
  CircleCheck,
  Info,
} from "lucide-react";
import { cn } from "@/lib/cn";
import type { Status } from "./copy";

/** True when this viewer may not change sign-in: every control is disabled. */
const ReadOnlyContext = createContext(false);
export const ReadOnlyProvider = ReadOnlyContext.Provider;
export const useReadOnly = () => useContext(ReadOnlyContext);

/** A heading and one card, the shape of every block on this page. */
export function Block({
  title,
  description,
  id,
  children,
}: {
  title: string;
  description?: ReactNode;
  id?: string;
  children: ReactNode;
}) {
  const fallback = useId();
  const headingId = id ?? `sign-in-${fallback}`;
  return (
    <section aria-labelledby={headingId} className="min-w-0">
      <div className="mb-(--space-heading)">
        <h3 id={headingId} className="text-heading">
          {title}
        </h3>
        {description ? (
          <p className="text-muted-foreground text-meta">{description}</p>
        ) : null}
      </div>
      <div className="bg-card text-card-foreground rounded-panel min-w-0 border">
        {children}
      </div>
    </section>
  );
}

/** A status in a dot and words, so colour is never the only signal. */
export function StatusDot({
  status,
  id,
}: {
  status: Status;
  id?: string;
}) {
  return (
    <span
      id={id}
      className={cn(
        "text-meta inline-flex items-center gap-1.5",
        status.tone === "ok" && "text-success",
        status.tone === "warn" && "text-warning",
        status.tone === "off" && "text-subtle-foreground",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-2 shrink-0 rounded-full",
          status.tone === "ok" && "bg-success-fill",
          status.tone === "warn" && "bg-warning-fill",
          status.tone === "off" && "bg-neutral",
        )}
      />
      {status.text}
    </span>
  );
}

/** A full-width band inside a card: info, warning or danger. */
export function Band({
  tone,
  children,
  action,
  role,
}: {
  tone: "info" | "warn" | "danger";
  children: ReactNode;
  action?: ReactNode;
  role?: "status" | "alert";
}) {
  const Icon = tone === "info" ? Info : AlertTriangle;
  return (
    <div
      role={role}
      className={cn(
        "text-meta flex items-start gap-2 px-3 py-2.5",
        tone === "info" && "bg-info-tint text-info",
        tone === "warn" && "bg-warning-tint text-warning",
        tone === "danger" && "bg-danger-tint text-danger font-medium",
      )}
    >
      <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0 flex-1">{children}</span>
      {action}
    </div>
  );
}

/** A found / not found line under the provider address. */
export function FoundLine({
  tone,
  children,
}: {
  tone: "ok" | "bad" | "wait";
  children: ReactNode;
}) {
  return (
    <p
      role="status"
      className={cn(
        "text-meta mt-1.5 flex items-center gap-1.5",
        tone === "ok" && "text-success",
        tone === "bad" && "text-danger",
        tone === "wait" && "text-muted-foreground",
      )}
    >
      {tone === "ok" ? (
        <CircleCheck aria-hidden="true" className="size-4 shrink-0" />
      ) : tone === "bad" ? (
        <AlertTriangle aria-hidden="true" className="size-4 shrink-0" />
      ) : (
        <span
          aria-hidden="true"
          className="size-4 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      <span>{children}</span>
    </p>
  );
}


/** A button that shows or hides what follows it. */
export function DisclosureButton({
  open,
  onClick,
  controls,
  className,
  children,
}: {
  open: boolean;
  onClick: () => void;
  controls: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onClick}
      className={cn(
        "focus-visible:outline-ring flex w-full items-center gap-1 text-left focus-visible:outline-2 max-sm:min-h-11",
        className,
      )}
    >
      <ChevronRight
        aria-hidden="true"
        className={cn(
          "size-4 shrink-0 opacity-75 transition-transform",
          open && "rotate-90",
        )}
      />
      {children}
    </button>
  );
}

/** A quiet text link-button. */
export function LinkButton({
  children,
  className,
  ...props
}: React.ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn(
        "text-link text-label underline underline-offset-4 max-sm:min-h-11",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}
