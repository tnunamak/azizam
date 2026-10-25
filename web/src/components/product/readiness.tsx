"use client";

import type { ReactNode } from "react";
import {
  AudioLines,
  CircleHelp,
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
import { capabilityLabels } from "@/lib/terms";

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
    label: capabilityLabels.chat,
    icon: MessageSquare,
    tone: "text-capability-chat",
    tint: "bg-capability-chat/12",
  },
  image: {
    label: capabilityLabels.image,
    icon: ImageIcon,
    tone: "text-capability-image",
    tint: "bg-capability-image/12",
  },
  video: {
    label: capabilityLabels.video,
    icon: Video,
    tone: "text-capability-video",
    tint: "bg-capability-video/12",
  },
  speech: {
    label: capabilityLabels.speech,
    icon: AudioLines,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  voice: {
    label: capabilityLabels.speech,
    icon: AudioLines,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  transcribe: {
    label: capabilityLabels.transcribe,
    icon: Mic,
    tone: "text-capability-transcribe",
    tint: "bg-capability-transcribe/12",
  },
  music: {
    label: capabilityLabels.music,
    icon: Music2,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  sfx: {
    label: capabilityLabels.sfx,
    icon: Waves,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  audio: {
    label: capabilityLabels.audio,
    icon: FileAudio,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  embed: {
    label: capabilityLabels.embed,
    icon: Layers,
    tone: "text-capability-embed",
    tint: "bg-capability-embed/12",
  },
  rerank: {
    label: capabilityLabels.rerank,
    icon: ListFilter,
    tone: "text-capability-embed",
    tint: "bg-capability-embed/12",
  },
  decide: {
    label: capabilityLabels.decide,
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

/** The label for a kind this UI names, or null for one it does not (so a caller can use the gateway's title). */
export function knownCapabilityLabel(key: string | null | undefined): string | null {
  return capabilityMetaMap[String(key ?? "")]?.label ?? null;
}

/**
 * Every titled region: `text-heading`, 12 above its content, an optional
 * count and one action at 32 (Button's default size).
 */
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
      <h2 id={id} className="text-heading">
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
