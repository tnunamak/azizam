"use client";
import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  previewSignIn,
  signInKeys,
  type SignInProposal,
} from "@/lib/signin-api";

export const PREVIEW_DEBOUNCE_MS = 300;

/**
 * Asks the server what saving would do, 300 ms after the last change. The
 * answer is the server's: the page does not work out who gets in. `settled` is
 * false while the shown answer is for an older form.
 */
export function usePreview(
  proposal: SignInProposal,
  testId: string | null,
  enabled: boolean,
) {
  const now = JSON.stringify([proposal, testId]);
  const [debounced, setDebounced] = useState(now);
  useEffect(() => {
    if (now === debounced) return;
    const timer = setTimeout(() => setDebounced(now), PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [now, debounced]);

  const query = useQuery({
    queryKey: [...signInKeys.snapshot, "preview", debounced],
    queryFn: () => {
      const [p, id] = JSON.parse(debounced) as [SignInProposal, string | null];
      return previewSignIn(p, id);
    },
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 0,
    retry: false,
  });
  return {
    preview: query.data,
    failed: query.isError,
    settled: debounced === now && !query.isFetching && !query.isPlaceholderData,
  };
}
