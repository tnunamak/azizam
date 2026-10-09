"use client";

import { useQuery } from "@tanstack/react-query";
import { homeApi } from "@/app/home/home-api";

/**
 * Who looks after this gateway, from `/me/api/home` (`gateway.owner_name`),
 * so a member is told to ask a person ("Ask Sam"), not a role (VOICE). Shares
 * Home's query; null while loading, when signed out, or when the gateway
 * has no name for its owner.
 */
export function useOwnerName(): string | null {
  const { data } = useQuery({
    queryKey: ["account", "home"],
    queryFn: homeApi.home,
  });
  if (!data?.authenticated) return null;
  return data.gateway?.owner_name?.trim() || null;
}

/** "Ask Sam" when the owner has a name, else "Ask the gateway owner". */
export function askOwner(name: string | null): string {
  return name ? `Ask ${name}` : "Ask the gateway owner";
}
