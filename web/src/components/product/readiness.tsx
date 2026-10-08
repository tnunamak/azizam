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
    label: "Voice",
    icon: AudioLines,
    tone: "text-capability-voice",
    tint: "bg-capability-voice/12",
  },
  voice: {
    label: "Voice",
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
