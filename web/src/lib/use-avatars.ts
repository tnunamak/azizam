"use client";

import { useQuery } from "@tanstack/react-query";
import { accountApi } from "./account-api";

/**
 * The avatar choices the viewer may see, by person id: their own, and
 * everyone's for an owner or admin. A person missing from it has the
 * automatic avatar. People and Library draw their marks from it.
 */
export function useAvatarDirectory(enabled = true) {
  const { data } = useQuery({
    queryKey: ["avatars", "directory"],
    queryFn: accountApi.avatars,
    enabled,
    retry: false,
    staleTime: 60_000,
  });
  return data;
}
