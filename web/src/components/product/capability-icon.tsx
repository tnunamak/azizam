import {
  MessageSquare,
  Image,
  Video,
  AudioLines,
  Mic,
  GitBranch,
  Layers,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { capabilityLabels } from "@/lib/terms";

export type Capability =
  | "chat"
  | "image"
  | "video"
  | "voice"
  | "transcribe"
  | "decide"
  | "embed";
const capabilities = {
  chat: {
    icon: MessageSquare,
    label: capabilityLabels.chat,
    color: "text-capability-chat bg-capability-chat/10",
  },
  image: {
    icon: Image,
    label: capabilityLabels.image,
    color: "text-capability-image bg-capability-image/10",
  },
  video: {
    icon: Video,
    label: capabilityLabels.video,
    color: "text-capability-video bg-capability-video/10",
  },
  voice: {
    icon: AudioLines,
    label: capabilityLabels.speech,
    color: "text-capability-voice bg-capability-voice/10",
  },
  transcribe: {
    icon: Mic,
    label: capabilityLabels.transcribe,
    color: "text-capability-transcribe bg-capability-transcribe/10",
  },
  decide: {
    icon: GitBranch,
    label: capabilityLabels.decisions,
    color: "text-capability-decide bg-capability-decide/10",
  },
  embed: {
    icon: Layers,
    label: capabilityLabels.embeddings,
    color: "text-capability-embed bg-capability-embed/10",
  },
};
export function CapabilityIcon({
  capability,
  className,
}: {
  capability: Capability;
  className?: string;
}) {
  const { icon: Icon, label, color } = capabilities[capability];
  return (
    <span
      role="img"
      aria-label={label}
      className={cn(
        "inline-flex size-8 shrink-0 items-center justify-center rounded-md [&>svg]:size-4",
        color,
        className,
      )}
    >
      <Icon aria-hidden="true" />
    </span>
  );
}
