"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CapabilityIcon,
  type Capability,
} from "@/components/product/capability-icon";
import {
  ErrorState,
  PageHeader,
  StatusPill,
  EmptyState,
} from "@/components/product";
import { homeApi, type HomeCapability } from "@/app/home/home-api";
import { StatusClient } from "./status-client";

const capabilityNames: Record<string, string> = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  audio: "Speech",
  voice: "Speech",
  transcribe: "Transcription",
  decide: "Decisions",
  embed: "Embeddings",
};
function iconCapability(capability: string): Capability {
  const value = capability === "audio" ? "voice" : capability;
  return [
    "chat",
    "image",
    "video",
    "voice",
    "transcribe",
    "decide",
    "embed",
  ].includes(value)
    ? (value as Capability)
    : "chat";
}

function MemberCapability({ capability }: { capability: HomeCapability }) {
  return (
    <li className="flex min-w-0 items-start gap-3 border-b py-5 last:border-0">
      <CapabilityIcon capability={iconCapability(capability.capability)} />
      <div className="min-w-0 space-y-2">
        <h2 className="font-medium">
          {capabilityNames[capability.capability] ?? capability.capability}
        </h2>
        <StatusPill status={capability.status} reason={capability.reason} />
      </div>
    </li>
  );
}

function MemberStatus() {
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["account", "home"],
    queryFn: homeApi.home,
    refetchInterval: 30_000,
  });
  return (
    <div className="space-y-6">
      <PageHeader
        title="Status"
        answer="Is it my app, or is the gateway unavailable?"
      />
      {isPending ? (
        <div
          role="status"
          aria-label="Loading your model status"
          aria-busy="true"
          className="space-y-4"
        >
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : isError ? (
        <ErrorState
          message="Your model status could not be loaded. Check your connection and try again."
          onRetry={() => void refetch()}
          retrying={isFetching}
        />
      ) : (
        <>
          <div className="border-b pb-6">
            <StatusPill
              status={data.status.state}
              reason={data.status.reason}
            />
          </div>
          {data.capabilities.length ? (
            <ul className="grid gap-x-8 sm:grid-cols-2">
              {data.capabilities.map((capability) => (
                <MemberCapability
                  key={`${capability.capability}-${capability.intent}`}
                  capability={capability}
                />
              ))}
            </ul>
          ) : (
            <EmptyState
              title="No models are available to your account yet"
              description="Ask the gateway owner to connect a model and grant your account access."
            />
          )}
          <div className="flex flex-wrap gap-3">
            <Button asChild>
              <Link href="/home">Check your connection settings</Link>
            </Button>
            <Button variant="outline" asChild>
              <Link href="/try">Try a request</Link>
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

export function StatusView() {
  const viewer = useViewer();
  return viewer.admin ? <StatusClient /> : <MemberStatus />;
}
