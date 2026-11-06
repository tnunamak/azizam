"use client";
import { useEffect, useState } from "react";
import { discoverProvider, type Discovery } from "@/lib/signin-api";

export type DiscoveryState = "none" | "wait" | "ok" | "bad";

export const DISCOVERY_DEBOUNCE_MS = 600;

/**
 * Looks for a sign-in provider at the address, 600 ms after the owner stops
 * typing. The saved address counts as found without a request: the page does
 * not call out to the provider just by opening.
 */
export function useDiscovery(address: string, savedAddress: string, enabled: boolean) {
  const typed = address.trim();
  const [settled, setSettled] = useState<{
    address: string;
    found: Discovery | null;
  } | null>(null);

  useEffect(() => {
    if (!enabled || !typed || typed === savedAddress.trim()) return;
    let current = true;
    const timer = setTimeout(() => {
      discoverProvider(typed).then(
        (found) => current && setSettled({ address: typed, found }),
        () => current && setSettled({ address: typed, found: null }),
      );
    }, DISCOVERY_DEBOUNCE_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [typed, savedAddress, enabled]);

  const here = settled?.address === typed ? settled : null;
  let state: DiscoveryState;
  if (!typed) state = "none";
  else if (here) state = here.found?.ok ? "ok" : "bad";
  else if (typed === savedAddress.trim()) state = "ok";
  else state = "wait";
  return { state, found: here?.found?.ok ? here.found : null };
}
