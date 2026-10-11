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
        "vf-incident-banner flex flex-col gap-[8px] px-[14px] py-[12px] border border-solid border-border rounded-[var(--radius)] bg-transparent",
        tone === "warn" && "vf-incident-banner-warn border-[color-mix(in_srgb,var(--warning)_40%,var(--border))]",
        tone === "bad" && "vf-incident-banner-bad border-[color-mix(in_srgb,var(--danger)_40%,var(--border))]",
        tone === "info" && "vf-incident-banner-info border-[color-mix(in_srgb,var(--info)_40%,var(--border))]",
        className,
      )}
    >
      <div className="min-w-0 flex-1">
        <p className="vf-incident-banner-title flex items-center justify-between gap-[10px] m-0 text-foreground text-[0.9375rem] font-[650] leading-[1.25]">{title}</p>
        {detail && <p className="vf-incident-banner-body m-0 text-muted-foreground text-[0.875rem] leading-[1.45]">{detail}</p>}
      </div>
      {(actions || onDismiss) && (
        <div className="vf-incident-banner-actions flex items-center gap-[8px] flex-wrap mt-[2px]">
          {actions}
          {onDismiss && (
            <button
              type="button"
              className="vf-button vf-button-ghost vf-button-sm min-h-[36px] inline-flex items-center justify-center gap-[8px] px-[14px] py-0 border border-solid border-transparent rounded-[999px] bg-transparent text-muted-foreground font-sans text-[0.875rem] font-[650] leading-none no-underline cursor-pointer [transition:background_var(--motion-fast)_var(--motion-ease),border-color_var(--motion-fast)_var(--motion-ease),color_var(--motion-fast)_var(--motion-ease),opacity_var(--motion-fast)_var(--motion-ease),transform_var(--motion-fast)_var(--motion-ease)] [&:hover]:bg-muted [&:hover]:text-foreground active:[transform:translateY(1px)] disabled:cursor-not-allowed disabled:opacity-[0.45] aria-disabled:cursor-not-allowed aria-disabled:opacity-[0.45] focus-visible:[outline:2px_solid_var(--primary)] focus-visible:outline-offset-[3px]"
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
    <div className={cn("vf-empty-operational flex flex-col items-center justify-center gap-[10px] px-[20px] py-[32px] text-center text-muted-foreground", className)}>
      <p
        className={cn(
          "vf-empty-operational-title m-0 text-[1.0625rem] font-[650] leading-[1.25]",
          tone === "neutral" && "text-foreground",
          tone === "warn" && "text-vf-warn text-warning",
          tone === "bad" && "text-vf-bad text-danger",
          tone === "info" && "text-vf-info text-info",
        )}
      >
        {title}
      </p>
      {detail && <p className="vf-empty-operational-body m-0 text-muted-foreground text-[0.9375rem] leading-[1.5] max-w-[340px]">{detail}</p>}
      {action && <div className="vf-empty-operational-actions flex items-center gap-[8px] flex-wrap justify-center mt-[4px]">{action}</div>}
    </div>
  );
}
