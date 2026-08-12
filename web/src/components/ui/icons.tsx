/** Tiny inline icon set used by the ui components. stroke=currentColor so
 * every icon inherits its color from the surrounding token classes. */

import type { SVGProps } from "react";

export type StatusIconTone = "ok" | "warn" | "bad" | "info" | "neutral";

function base(props: SVGProps<SVGSVGElement>): SVGProps<SVGSVGElement> {
  return {
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
    ...props,
  };
}

export function CheckIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      <path d="M3 8.5 6.5 12 13 4.5" />
    </svg>
  );
}

export function CopyIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
      <path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" />
    </svg>
  );
}

export function XIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      <path d="m4 4 8 8M12 4l-8 8" />
    </svg>
  );
}

export function ChevronDownIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      <path d="m4 6.5 4 4 4-4" />
    </svg>
  );
}

export function DotsIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base({ ...props, fill: "currentColor", stroke: "none" })}>
      <circle cx="3.5" cy="8" r="1.25" />
      <circle cx="8" cy="8" r="1.25" />
      <circle cx="12.5" cy="8" r="1.25" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Admin nav icons (same 16×16 1.5-stroke style as above)

export function OverviewIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      {/* 2×2 grid of squares */}
      <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="2.5" y="9" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="9" width="4.5" height="4.5" rx="1" />
    </svg>
  );
}

export function ServicesIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      {/* Server stack */}
      <rect x="2" y="3" width="12" height="4" rx="1.5" />
      <rect x="2" y="9" width="12" height="4" rx="1.5" />
      <circle cx="12.5" cy="5" r="0.75" fill="currentColor" stroke="none" />
      <circle cx="12.5" cy="11" r="0.75" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function AppsIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      {/* Key shape */}
      <circle cx="6" cy="8" r="3" />
      <path d="M9 8h5M12 6.5V8" />
    </svg>
  );
}

export function ProfilesIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      {/* Shield */}
      <path d="M8 2 L14 4.5 V9 C14 12 8 14.5 8 14.5 C8 14.5 2 12 2 9 V4.5 Z" />
    </svg>
  );
}

export function UsersIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      {/* Person + person behind */}
      <circle cx="6.5" cy="5.5" r="2.5" />
      <path d="M1.5 14.5c0-2.76 2.24-5 5-5s5 2.24 5 5" />
      <path d="M10.5 3a2.5 2.5 0 0 1 0 5" strokeDasharray="0" />
      <path d="M13.5 14.5a4.99 4.99 0 0 0-3-4.58" />
    </svg>
  );
}

export function AuditIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      {/* Document with lines */}
      <rect x="3" y="2" width="10" height="12" rx="1.5" />
      <path d="M6 6h4M6 9h4M6 12h2" />
    </svg>
  );
}

export function SettingsIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      {/* Gear */}
      <circle cx="8" cy="8" r="2.5" />
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41" />
    </svg>
  );
}

export function MenuIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      <path d="M2.5 5h11M2.5 8h11M2.5 11h11" />
    </svg>
  );
}

/** Generic operational-state glyph. Pair it with visible state text whenever
 * it communicates meaning; the tone class is token-derived in brand CSS. */
export function StatusIcon({
  tone = "neutral",
  label,
  className,
  ...props
}: SVGProps<SVGSVGElement> & { tone?: StatusIconTone; label?: string }) {
  return (
    <svg
      {...base({
        ...props,
        className: `vf-status-icon vf-status-icon-${tone}${className ? ` ${className}` : ""}`,
        "aria-hidden": label ? undefined : true,
        "aria-label": label,
        role: label ? "img" : undefined,
      })}
    >
      <circle cx="8" cy="8" r="5.25" />
      <path d="M8 5.25v3.1M8 10.75h.01" />
    </svg>
  );
}

export function ExternalLinkIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base(props)}>
      <path d="M7 3H3.5A1.5 1.5 0 0 0 2 4.5v8A1.5 1.5 0 0 0 3.5 14h8A1.5 1.5 0 0 0 13 12.5V9" />
      <path d="M8.5 2H14v5.5" />
      <path d="M14 2 8 8" />
    </svg>
  );
}
