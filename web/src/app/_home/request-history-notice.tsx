"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Info, X } from "lucide-react";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { accountApi } from "@/lib/account-api";
import { toast } from "@/lib/toast";
import {
  requestHistoryKey,
  type RequestHistoryChoice,
} from "@/lib/request-history";

/**
 * The one-time notice for a member who predates request history (REQUEST_RETENTION.md section 12, item 3). Turning
 * history on answers it, and so does dismissing it; either way it does not come back.
 */
export function RequestHistoryNotice() {
  const viewer = useViewer();
  const queryClient = useQueryClient();
  const enabled = viewer.authenticated && !viewer.serviceAdmin;
  const { data } = useQuery({
    queryKey: requestHistoryKey,
    queryFn: accountApi.requestHistory,
    enabled,
  });
  const turnOn = useMutation({
    mutationFn: (choice: RequestHistoryChoice) =>
      accountApi.saveRequestHistory({
        keep: true,
        retention: choice.retention,
        parts: choice.parts,
      }),
    onSuccess: (next) => queryClient.setQueryData(requestHistoryKey, next),
    onError: () => toast.error("Request history wasn’t turned on. Try again."),
  });
  const dismiss = useMutation({
    mutationFn: accountApi.dismissRequestHistoryNotice,
    onSuccess: () =>
      queryClient.setQueryData<RequestHistoryChoice>(
        requestHistoryKey,
        (current) => current && { ...current, notice_due: false },
      ),
    onError: () => toast.error("Couldn’t save that. Try again."),
  });

  // With the owner's switch off for everyone, there is nothing to turn on, so the notice is not offered. It stays due
  // for the member, and comes back if the owner turns request history on later.
  if (!enabled || !data?.notice_due || data.household_off) return null;
  const busy = turnOn.isPending || dismiss.isPending;
  return (
    // A slim one-line banner on the page tint, not a card (judge round 2): it asks once, and Make stays the
    // page's one primary action, so Turn on is an outline button and Not now a text button.
    <section
      aria-labelledby="history-notice-title"
      className="bg-muted rounded-panel flex min-h-12 flex-wrap items-center gap-x-3 gap-y-1 py-1.5 pr-1.5 pl-4"
    >
      <Info aria-hidden="true" className="text-info size-4 shrink-0" />
      <p className="text-meta min-w-0 flex-1">
        <span id="history-notice-title" role="heading" aria-level={2} className="font-medium">
          Request history is available.
        </span>{" "}
        <span className="text-muted-foreground">
          Look back at what you asked. It’s off for you until you turn it on.
        </span>
      </p>
      <div className="flex shrink-0 items-center gap-1">
        <Button size="sm" variant="outline" disabled={busy} onClick={() => turnOn.mutate(data)}>
          Turn on
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => dismiss.mutate(undefined)}>
          Not now
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Dismiss"
          className="text-muted-foreground"
          disabled={busy}
          onClick={() => dismiss.mutate(undefined)}
        >
          <X aria-hidden="true" />
        </Button>
      </div>
    </section>
  );
}
