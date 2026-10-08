"use client";

import type { ReactNode } from "react";
import {
  AudioLines,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleHelp,
  CircleMinus,
  CircleX,
  Clock3,
  FileAudio,
  GitBranch,
  ImageIcon,
  Layers,
  ListFilter,
  type LucideIcon,
  MessageSquare,
  Mic,
  Music2,
  Video,
  Waves,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { RelativeTime } from "@/components/ui/relative-time";

/* ------------------------------------------------------------------ */
/* Capabilities                                                        */
/* ------------------------------------------------------------------ */

export interface CapabilityMeta {
  label: string;
  icon: LucideIcon;
  /** Text colour for the mark; the tile tints with the same token. */
  tone: string;
  tint: string;
}

const capabilityMetaMap: Record<string, CapabilityMeta> = {
  chat: {
    label: "Chat",
    icon: MessageSquare,
    tone: "text-capability-chat",
    tint: "bg-capability-chat/12",
  },
  image: {
    label: "Images",
    icon: ImageIcon,
    tone: "text-capability-image",
    tint: "bg-capability-image/12",
  },
  video: {
    label: "Video",
    icon: Video,
    tone: "text-capability-video",
    tint: "bg-capability-video/12",
  },
  speech: {
    label: "Speech",
    icon: AudioLines,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  voice: {
    label: "Speech",
    icon: AudioLines,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  transcribe: {
    label: "Transcription",
    icon: Mic,
    tone: "text-capability-transcribe",
    tint: "bg-capability-transcribe/12",
  },
  music: {
    label: "Music",
    icon: Music2,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  sfx: {
    label: "Sound effects",
    icon: Waves,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  audio: {
    label: "Audio",
    icon: FileAudio,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  embed: {
    label: "Embeddings",
    icon: Layers,
    tone: "text-capability-embed",
    tint: "bg-capability-embed/12",
  },
  rerank: {
    label: "Reranking",
    icon: ListFilter,
    tone: "text-capability-embed",
    tint: "bg-capability-embed/12",
  },
  decide: {
    label: "Decisions",
    icon: GitBranch,
    tone: "text-capability-decide",
    tint: "bg-capability-decide/12",
  },
};

export function capabilityMeta(key: string | null | undefined): CapabilityMeta {
  return (
    capabilityMetaMap[String(key ?? "")] ?? {
      label: "Capability",
      icon: CircleHelp,
      tone: "text-muted-foreground",
      tint: "bg-muted",
    }
  );
}

/* ------------------------------------------------------------------ */
/* States: always an icon, a colour and a word                         */
/* ------------------------------------------------------------------ */

export type Tone = "ok" | "warn" | "bad" | "off" | "unknown" | "busy";

export interface StateView {
  label: string;
  tone: Tone;
}

/** One vocabulary for capability and engine readiness, for every viewer. */
export function availabilityState(
  status: string | null | undefined,
  reasonCode?: string | null,
): StateView {
  const code = String(reasonCode || status || "").toLowerCase();
  const state = String(status || "").toLowerCase();
  if (code === "unavailable_disabled") return { label: "Turned off", tone: "off" };
  if (code === "unavailable_unconfigured")
    return { label: "Not set up", tone: "off" };
  if (code === "policy_denied") return { label: "Not in your plan", tone: "off" };
  if (state === "ready") return { label: "Ready", tone: "ok" };
  if (["idle", "idle-startable", "ready_on_request", "standby_stopped"].includes(state) || code === "ready_on_request")
    return { label: "Ready to start", tone: "ok" };
  if (state === "starting") return { label: "Starting", tone: "busy" };
  if (state === "busy") return { label: "Busy", tone: "warn" };
  if (state === "degraded") return { label: "Degraded", tone: "warn" };
  if (["unavailable", "down", "failed"].includes(state))
    return { label: "Unavailable", tone: "bad" };
  return { label: "Not checked yet", tone: "unknown" };
}

const toneStyles: Record<Tone, { icon: LucideIcon; text: string; dot: string }> = {
  ok: { icon: CircleCheck, text: "text-success", dot: "bg-success" },
  warn: { icon: CircleAlert, text: "text-warning", dot: "bg-warning" },
  busy: { icon: Clock3, text: "text-info", dot: "bg-info" },
  bad: { icon: CircleX, text: "text-danger", dot: "bg-danger" },
  off: { icon: CircleMinus, text: "text-muted-foreground", dot: "bg-neutral" },
  unknown: {
    icon: CircleDashed,
    text: "text-muted-foreground",
    dot: "bg-neutral",
  },
};

export function toneIcon(tone: Tone): LucideIcon {
  return toneStyles[tone].icon;
}
export function toneText(tone: Tone): string {
  return toneStyles[tone].text;
}
export function toneDot(tone: Tone): string {
  return toneStyles[tone].dot;
}

export function StateLabel({
  state,
  className,
  iconOnly = false,
}: {
  state: StateView;
  className?: string;
  iconOnly?: boolean;
}) {
  const { icon: Icon, text } = toneStyles[state.tone];
  return (
    <span
      className={cn(
        "inline-flex min-w-0 items-center gap-1.5 text-sm font-medium",
        text,
        className,
      )}
    >
      <Icon aria-hidden="true" className="size-4 shrink-0" />
      <span className={iconOnly ? "sr-only" : "truncate"}>{state.label}</span>
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Evidence: a check time is never shown as a use time, or the reverse */
/* ------------------------------------------------------------------ */

export interface EvidenceLike {
  source?: string | null;
  observed_at_s?: number | null;
  receipt_status?: string | null;
  kind?: string | null;
}

export type EvidenceStamp =
  | { kind: "checked"; at: number }
  | { kind: "used"; at: number; failed: boolean };

export function evidenceStamp(
  evidence: EvidenceLike | null | undefined,
): EvidenceStamp | null {
  if (!evidence || typeof evidence.observed_at_s !== "number") return null;
  const source = String(evidence.source ?? "");
  if (!source || ["registry", "capability"].includes(source)) return null;
  if (source === "request")
    return {
      kind: "used",
      at: evidence.observed_at_s,
      failed: !["succeeded", "success", "completed", "ok"].includes(
        String(evidence.receipt_status ?? "succeeded").toLowerCase(),
      ),
    };
  return { kind: "checked", at: evidence.observed_at_s };
}

export function EvidenceTime({
  stamp,
  className,
}: {
  stamp: EvidenceStamp | null;
  className?: string;
}) {
  if (!stamp) return null;
  const verb =
    stamp.kind === "checked" ? "Checked" : stamp.failed ? "Failed" : "Used";
  return (
    <span className={cn("text-muted-foreground text-sm", className)}>
      {verb} <RelativeTime ts={stamp.at} fallback="" />
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Verdict: the sentence every surface opens with                      */
/* ------------------------------------------------------------------ */

export function Verdict({
  tone,
  children,
  detail,
  className,
}: {
  tone: Tone;
  children: ReactNode;
  detail?: ReactNode;
  className?: string;
}) {
  const { icon: Icon, text } = toneStyles[tone];
  return (
    <div className={cn("min-w-0", className)}>
      <p
        role="status"
        aria-live="polite"
        className="flex items-start gap-3 text-2xl font-semibold tracking-tight text-balance sm:text-3xl"
      >
        <Icon
          aria-hidden="true"
          className={cn("mt-1 size-6 shrink-0 sm:mt-1.5 sm:size-7", text)}
        />
        <span className="min-w-0">{children}</span>
      </p>
      {detail ? (
        <div className="text-muted-foreground mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 pl-9 text-base sm:pl-10">
          {detail}
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Section heading: one pattern for every titled region                */
/* ------------------------------------------------------------------ */

export function SectionHeading({
  id,
  title,
  action,
  count,
  className,
}: {
  id?: string;
  title: string;
  action?: ReactNode;
  count?: number | null;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "mb-3 flex min-h-8 items-center justify-between gap-3",
        className,
      )}
    >
      <h2 id={id} className="text-lg font-semibold">
        {title}
        {typeof count === "number" ? (
          <span className="text-muted-foreground ml-2 font-normal">
            {count}
          </span>
        ) : null}
      </h2>
      {action}
    </div>
  );
}
