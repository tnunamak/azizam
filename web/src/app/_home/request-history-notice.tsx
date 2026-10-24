"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { Callout } from "@/components/product/status-ui";
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
    <Callout
      severity="info"
      title="Request history is available"
      actions={
        <>
          <Button
            size="sm"
            disabled={busy}
            onClick={() => turnOn.mutate(data)}
          >
            Turn on
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => dismiss.mutate(undefined)}
          >
            Not now
          </Button>
        </>
      }
    >
      <p>
        Azizam can now keep your request history so you can look back at what
        you asked. It’s off for you until you turn it on.
      </p>
    </Callout>
  );
}
