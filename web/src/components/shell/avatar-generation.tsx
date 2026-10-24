"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  AvatarPicker,
  type AvatarGenerationView,
  type AvatarPickerProps,
} from "@/components/product/person-avatar";
import { accountApi, type AvatarSetting } from "@/lib/account-api";
import { applySavedAvatar } from "./avatar-setting";

/** The make job states after which it makes no more pictures. */
const STOPPED = new Set(["succeeded", "failed", "canceled", "cancelled"]);

/**
 * The make-my-avatar flow: whether an image route exists, one attempt at a time, the pictures it made, and
 * keeping or removing one. `onSaved` receives the avatar the gateway saved.
 */
function useAvatarGeneration(onSaved: (saved: AvatarSetting) => void): AvatarGenerationView {
  const [jobId, setJobId] = useState<string | null>(null);
  const { data: generatorData } = useQuery({
    queryKey: ["avatars", "generator"],
    queryFn: accountApi.avatarGenerator,
    retry: false,
    staleTime: 60_000,
  });
  const start = useMutation({
    mutationFn: accountApi.startAvatarGeneration,
    onSuccess: ({ job }) => setJobId(job.job_id),
  });
  const { data: jobData } = useQuery({
    queryKey: ["avatars", "job", jobId],
    queryFn: () => accountApi.makeJob(jobId ?? ""),
    enabled: jobId !== null,
    retry: false,
    refetchInterval: (query) =>
      query.state.data && STOPPED.has(query.state.data.job.status) ? false : 1_000,
  });
  const status = jobData?.job.status ?? "";
  const succeeded = status === "succeeded";
  const { data: pictureData, isError: picturesFailed } = useQuery({
    queryKey: ["avatars", "pictures", jobId],
    queryFn: async () => {
      const made = (jobData?.job.assets ?? []).filter((asset) => asset.media_type?.startsWith("image/"));
      const links = await Promise.all(
        made.map(async (asset) => ({
          assetId: asset.asset_id,
          src: await accountApi.mediaDownloadUrl(asset.asset_id),
        })),
      );
      return links.filter((link): link is { assetId: string; src: string } => Boolean(link.src));
    },
    enabled: succeeded,
    retry: false,
  });
  const keep = useMutation({
    mutationFn: (assetId: string) => accountApi.keepGeneratedAvatar(assetId),
    onSuccess: (saved) => {
      setJobId(null);
      onSaved(saved);
    },
  });
  const remove = useMutation({
    mutationFn: () => accountApi.removeGeneratedAvatar(),
    onSuccess: (saved) => {
      setJobId(null);
      onSaved(saved);
    },
  });
  return {
    available: generatorData?.available === true,
    making: start.isPending || (jobId !== null && !STOPPED.has(status)),
    results: succeeded ? (pictureData ?? []) : [],
    failed:
      start.isError || status === "failed" || status === "canceled" || status === "cancelled" || picturesFailed,
    onMake: () => {
      setJobId(null);
      start.mutate();
    },
    onKeep: (assetId) => keep.mutate(assetId),
    onRemove: () => remove.mutate(),
  };
}

/**
 * The avatar picker with the make-my-avatar flow. It mounts only while the picker is open, so the gateway is
 * asked whether it can make a picture only when someone is choosing an avatar.
 */
export function AvatarPickerWithGeneration(props: Omit<AvatarPickerProps, "generation">) {
  const client = useQueryClient();
  const generation = useAvatarGeneration((saved) => applySavedAvatar(client, saved));
  return <AvatarPicker {...props} generation={generation} />;
}
