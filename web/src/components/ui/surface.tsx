"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type {
  ComponentPropsWithoutRef,
  CSSProperties,
  ReactElement,
  ReactNode,
} from "react";
import { cloneElement } from "react";

import { cn } from "@/lib/cn";
import { type BadgeTone } from "./badge";
import { CopyButton } from "./copy-button";

export interface BrandLockupProps {
  href?: string;
  name?: string;
  kicker?: string;
  className?: string;
}

function BrandLockupBody({
  name = "vivid.fish",
  kicker = "AI Gateway",
}: Pick<BrandLockupProps, "name" | "kicker">) {
  return (
    <>
      <span className="vf-brand-mark" aria-hidden />
      <span className="vf-brand-wordmark">
        <span className="vf-brand-name">{name}</span>
        <span className="vf-brand-kicker">{kicker}</span>
      </span>
    </>
  );
}

export function BrandLockup({
  href,
  name,
  kicker,
  className,
}: BrandLockupProps) {
  const classes = cn("vf-brand-lockup", className);
  if (href) {
    return (
      <Link href={href} className={classes}>
        <BrandLockupBody name={name} kicker={kicker} />
      </Link>
    );
  }
  return (
    <div className={classes}>
      <BrandLockupBody name={name} kicker={kicker} />
    </div>
  );
}

export function AmbientBackdrop() {
  return <div className="vf-ambient-backdrop" aria-hidden />;
}

export interface StatusClusterProps {
  tone?: BadgeTone;
  label: ReactNode;
  detail?: ReactNode;
  className?: string;
}

export function StatusCluster({
  tone = "neutral",
  label,
  detail,
  className,
}: StatusClusterProps) {
  return (
    <span className={cn("vf-status-cluster", className)}>
      <span
        className={cn(
          "vf-status-dot",
          tone === "warn" && "vf-status-dot-warn",
          tone === "bad" && "vf-status-dot-bad",
          tone === "info" && "vf-status-dot-info",
          tone === "neutral" && "vf-status-dot-neutral",
          tone === "ok" && "vf-status-dot-pulse",
        )}
        aria-hidden
      />
      <span className="min-w-0">
        <span className="text-vf-small text-vf-text block truncate">
          {label}
        </span>
        {detail && (
          <span className="text-vf-micro text-vf-subtle block truncate font-mono uppercase">
            {detail}
          </span>
        )}
      </span>
    </span>
  );
}

export interface AccountChipProps {
  email?: string | null;
  label?: string;
  className?: string;
}

function initials(value: string): string {
  const cleaned = value.trim();
  if (!cleaned) return "AI";
  const base = cleaned.includes("@") ? cleaned.split("@")[0]! : cleaned;
  const parts = base.split(/[.\-_\s]+/).filter(Boolean);
  if (parts.length >= 2) {
    return `${parts[0]![0] ?? ""}${parts[1]![0] ?? ""}`.toUpperCase();
  }
  return base.slice(0, 2).toUpperCase();
}

export function AccountChip({ email, label, className }: AccountChipProps) {
  const display = label ?? email ?? "Signed in";
  return (
    <span className={cn("vf-account-chip", className)}>
      <span className="vf-account-avatar" aria-hidden>
        {initials(display)}
      </span>
      <span className="truncate">{display}</span>
    </span>
  );
}

export interface PageLayoutProps {
  children: ReactNode;
  rail?: ReactNode;
  wide?: boolean;
  className?: string;
}

export function PageLayout({
  children,
  rail,
  wide,
  className,
}: PageLayoutProps) {
  return (
    <div
      className={cn(
        "vf-page-layout",
        (wide || !rail) && "vf-page-layout-wide",
        className,
      )}
    >
      <div className="vf-page-main">{children}</div>
      {rail && <aside className="vf-right-rail">{rail}</aside>}
    </div>
  );
}

export interface ProductPanelProps extends ComponentPropsWithoutRef<"section"> {
  tone?: "panel" | "control" | "feed";
  tight?: boolean;
}

export function ProductPanel({
  tone = "panel",
  tight = false,
  className,
  children,
  ...props
}: ProductPanelProps) {
  return (
    <section
      className={cn(
        tone === "control"
          ? "vf-control-panel"
          : tone === "feed"
            ? "vf-feed-panel"
            : "vf-panel",
        tight && tone === "panel" && "vf-panel-tight",
        className,
      )}
      {...props}
    >
      {children}
    </section>
  );
}

export function PanelHeader({
  title,
  description,
  action,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("vf-panel-head", className)}>
      <div className="min-w-0">
        <h2 className="vf-panel-title">{title}</h2>
        {description && <p className="vf-panel-copy">{description}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export interface ObjectRowProps {
  title: ReactNode;
  meta?: ReactNode;
  icon?: ReactNode;
  side?: ReactNode;
  href?: string;
  onClick?: () => void;
  className?: string;
}

function ObjectRowBody({ title, meta, icon, side }: ObjectRowProps) {
  return (
    <>
      <span className="vf-object-icon" aria-hidden>
        {icon}
      </span>
      <span className="vf-object-body">
        <span className="vf-object-title">{title}</span>
        {meta && <span className="vf-object-meta">{meta}</span>}
      </span>
      {side && <span className="vf-object-side">{side}</span>}
    </>
  );
}

export function ObjectRow({
  href,
  onClick,
  className,
  ...props
}: ObjectRowProps) {
  const classes = cn("vf-object-row", className);
  if (href) {
    return (
      <Link href={href} className={classes}>
        <ObjectRowBody {...props} />
      </Link>
    );
  }
  if (onClick) {
    return (
      <button type="button" className={cn(classes, "w-full")} onClick={onClick}>
        <ObjectRowBody {...props} />
      </button>
    );
  }
  return (
    <div className={classes}>
      <ObjectRowBody {...props} />
    </div>
  );
}

export function ObjectList({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn("vf-object-list", className)}>{children}</div>;
}

export interface ActionCardProps {
  href: string;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
}

export function ActionCard({
  href,
  title,
  description,
  icon,
}: ActionCardProps) {
  return (
    <Link href={href} className="vf-action-card">
      <span className="vf-object-icon">{icon}</span>
      <span className="flex min-w-0 flex-col gap-1">
        <span className="text-vf-small text-vf-text font-medium">{title}</span>
        {description && (
          <span className="text-vf-micro text-vf-subtle leading-snug">
            {description}
          </span>
        )}
      </span>
    </Link>
  );
}

export interface MobileNavItem {
  label: string;
  href: string;
  icon: ReactNode;
  onClick?: () => void;
  active?: boolean;
  /** Lets an owning Radix primitive attach its real trigger semantics. */
  trigger?: (tab: ReactElement) => ReactElement;
}

export function MobileTabBar({ items }: { items: MobileNavItem[] }) {
  const pathname = usePathname();
  return (
    <nav
      className="vf-mobile-tabbar"
      style={{ "--vf-mobile-tabs": String(items.length) } as CSSProperties}
      aria-label="Primary navigation"
    >
      {items.map((item) => {
        const active =
          item.active ??
          (item.href === "/admin"
            ? pathname === "/admin" || pathname === "/admin/"
            : pathname.startsWith(item.href));
        const tab = item.onClick ? (
            <button
              key={item.href}
              type="button"
              onClick={item.onClick}
              className="vf-mobile-tab"
              aria-current={active ? "page" : undefined}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ) : (
          <Link
            key={item.href}
            href={item.href}
            className="vf-mobile-tab"
            aria-current={active ? "page" : undefined}
          >
            {item.icon}
            <span>{item.label}</span>
          </Link>
        );
        return item.trigger
          ? cloneElement(item.trigger(tab), { key: item.href })
          : tab;
      })}
    </nav>
  );
}

export function SectionKicker({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <p className={cn("vf-section-label", className)}>{children}</p>;
}

export interface NoticeBannerProps {
  tone?: BadgeTone;
  title?: ReactNode;
  children: ReactNode;
  action?: ReactNode;
  onDismiss?: () => void;
  role?: "status" | "alert" | "note";
  className?: string;
}

export function NoticeBanner({
  tone = "neutral",
  title,
  children,
  action,
  onDismiss,
  role,
  className,
}: NoticeBannerProps) {
  const semanticRole = role ?? (tone === "bad" ? "alert" : "status");
  return (
    <div
      role={semanticRole}
      className={cn(
        "vf-notice",
        tone === "ok" && "vf-notice-ok",
        tone === "warn" && "vf-notice-warn",
        tone === "bad" && "vf-notice-bad",
        className,
      )}
    >
      <span
        className={cn(
          "vf-status-dot mt-1",
          tone === "warn" && "vf-status-dot-warn",
          tone === "bad" && "vf-status-dot-bad",
          tone === "info" && "vf-status-dot-info",
          tone === "neutral" && "vf-status-dot-neutral",
        )}
        aria-hidden
      />
      <span className="min-w-0 flex-1">
        {title && <span className="vf-notice-title block">{title}</span>}
        <span className="vf-notice-body block">{children}</span>
      </span>
      {(action || onDismiss) && (
        <span className="flex shrink-0 items-center gap-2">
          {action}
          {onDismiss && (
            <button
              type="button"
              className="vf-button vf-button-ghost vf-button-sm"
              onClick={onDismiss}
            >
              Dismiss
            </button>
          )}
        </span>
      )}
    </div>
  );
}

export interface CodeBlockProps {
  label?: ReactNode;
  value: string;
  copyValue?: string;
  className?: string;
  wrap?: boolean;
}

export function CodeBlock({
  label,
  value,
  copyValue,
  className,
  wrap = true,
}: CodeBlockProps) {
  return (
    <div className={cn("relative flex min-w-0 flex-col gap-1.5", className)}>
      {label && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-vf-micro text-vf-subtle font-mono uppercase">
            {label}
          </span>
          <CopyButton value={copyValue ?? value} aria-label="Copy value" />
        </div>
      )}
      <pre
        className={cn(
          "vf-key-block",
          !wrap && "whitespace-nowrap",
          !label && "pr-12",
        )}
      >
        {value}
      </pre>
      {!label && (
        <div className="absolute top-3 right-3">
          <CopyButton value={copyValue ?? value} aria-label="Copy value" />
        </div>
      )}
    </div>
  );
}
