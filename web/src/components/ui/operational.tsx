"use client";

import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface IncidentBannerProps {
  tone: "warn" | "bad" | "info";
  title: string;
  detail?: string;
  actions?: ReactNode;
  onDismiss?: () => void;
  className?: string;
}

export function IncidentBanner({
  tone,
  title,
  detail,
  actions,
  onDismiss,
  className,
}: IncidentBannerProps) {
  return (
    <div
      role={tone === "bad" ? "alert" : "status"}
      className={cn(
        "vf-incident-banner",
        tone === "warn" && "vf-incident-banner-warn",
        tone === "bad" && "vf-incident-banner-bad",
        tone === "info" && "vf-incident-banner-info",
        className,
      )}
    >
      <div className="min-w-0 flex-1">
        <p className="vf-incident-banner-title">{title}</p>
        {detail && <p className="vf-incident-banner-body">{detail}</p>}
      </div>
      {(actions || onDismiss) && (
        <div className="vf-incident-banner-actions">
          {actions}
          {onDismiss && (
            <button
              type="button"
              className="vf-button vf-button-ghost vf-button-sm"
              onClick={onDismiss}
              aria-label="Dismiss"
            >
              Dismiss
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export interface EmptyOperationalStateProps {
  title: string;
  detail?: string;
  action?: ReactNode;
  tone?: "neutral" | "info" | "warn" | "bad";
  className?: string;
}

export function EmptyOperationalState({
  title,
  detail,
  action,
  tone = "neutral",
  className,
}: EmptyOperationalStateProps) {
  return (
    <div className={cn("vf-empty-operational", className)}>
      <p
        className={cn(
          "vf-empty-operational-title",
          tone === "warn" && "text-vf-warn",
          tone === "bad" && "text-vf-bad",
          tone === "info" && "text-vf-info",
        )}
      >
        {title}
      </p>
      {detail && <p className="vf-empty-operational-body">{detail}</p>}
      {action && <div className="vf-empty-operational-actions">{action}</div>}
    </div>
  );
}
