"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { ErrorState } from "@/components/product/error-state";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { accountApi } from "@/lib/account-api";
import {
  periodWords,
  requestHistoryKey,
  type RequestHistoryChoice,
} from "@/lib/request-history";

type Pending = { kind: "off" } | { kind: "period"; value: string };

type Change = Partial<Pick<RequestHistoryChoice, "keep" | "retention">> & {
  replies?: boolean;
};

/**
 * The person's request history choice (Your settings). Turning it off, or shortening the period, deletes what
 * was kept now, so each of those asks first.
 */
export function RequestHistorySection() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<Pending | null>(null);
  const { data, isPending, isError, error, refetch, isRefetching } = useQuery({
    queryKey: requestHistoryKey,
    queryFn: accountApi.requestHistory,
  });
  const save = useMutation({
    mutationFn: accountApi.saveRequestHistory,
    onSuccess: (next) => {
      queryClient.setQueryData(requestHistoryKey, next);
      void queryClient.invalidateQueries({ queryKey: ["account", "request-history", "list"] });
    },
    onError: (failure) => {
      toast.error(
        failure instanceof Error && failure.message
          ? failure.message
          : "Your setting wasn’t saved. Try again.",
      );
      void queryClient.invalidateQueries({ queryKey: requestHistoryKey });
    },
  });

  if (isPending)
    return (
      <div role="status" aria-label="Loading request history" aria-busy="true">
        <Skeleton className="mb-3 h-6 w-40" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  if (isError || !data)
    return (
      <ErrorState
        heading="Couldn’t load your request history"
        error={error}
        onRetry={() => void refetch()}
        retrying={isRefetching}
      />
    );

  const current: RequestHistoryChoice = data;
  const busy = save.isPending;
  const repliesOffered = current.allowed_parts.includes("received");
  const keepsReplies = current.parts.includes("received");

  function put(change: Change) {
    const replies = change.replies ?? keepsReplies;
    return save.mutateAsync({
      keep: change.keep ?? current.keep,
      retention: change.retention ?? current.retention,
      parts: replies ? ["accepted", "received"] : ["accepted"],
    });
  }

  function choosePeriod(value: string) {
    if (value === current.retention) return;
    const shorter =
      current.choices.indexOf(value) < current.choices.indexOf(current.retention);
    if (shorter) setPending({ kind: "period", value });
    else void put({ retention: value }).catch(() => {});
  }

  return (
    <div>
      <SettingsSection
        id="history"
        title="Request history"
        description="Choose whether Azizam keeps what your apps send, and for how long."
      >
        <SettingRow
          label="Keep request history"
          htmlFor="request-history-keep"
          description={
            data.keep
              ? "Azizam keeps what your apps send so you can look back at it."
              : "Nothing your apps send is kept."
          }
          control={
            <Switch
              id="request-history-keep"
              checked={data.keep}
              disabled={busy}
              onCheckedChange={(next) => {
                if (next) void put({ keep: true }).catch(() => {});
                else setPending({ kind: "off" });
              }}
            />
          }
        />
        {repliesOffered ? (
          <SettingRow
            label="Also keep replies"
            htmlFor="request-history-replies"
            description={
              data.keep
                ? "Keeps what came back, not only what was sent."
                : "Turn on request history to change this."
            }
            control={
              <Switch
                id="request-history-replies"
                checked={keepsReplies}
                disabled={busy || !data.keep}
                onCheckedChange={(next) => {
                  void put({ replies: next }).catch(() => {});
                }}
              />
            }
          />
        ) : null}
        <SettingRow
          label="Forget requests after"
          // One line on a phone too: the label truncates before the control wraps under it.
          className="max-sm:flex-nowrap"
          description={
            data.keep
              ? "Older requests are deleted."
              : "Turn on request history to change this."
          }
          control={
            <Select
              value={data.retention}
              disabled={busy || !data.keep}
              onValueChange={(value) => choosePeriod(value)}
            >
              <SelectTrigger
                size="sm"
                aria-label="Forget requests after"
                className="sm:min-w-32"
              >
                <SelectValue>{periodWords(data.retention)}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {data.choices.map((value) => (
                  <SelectItem key={value} value={value}>
                    {periodWords(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          }
        />
        <SettingRow
          label="Your history"
          description="Look back at what was sent and what came back."
          control={
            <Button asChild variant="outline">
              <Link prefetch={false} href="/history">
                Open History
              </Link>
            </Button>
          }
        />
      </SettingsSection>
      <p className="text-muted-foreground text-meta mt-3">
        Only you can see your request history. To keep one request out of it, turn on Incognito in Try.
      </p>
      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => !open && setPending(null)}
        title={
          pending?.kind === "off"
            ? "Stop keeping request history?"
            : `Forget requests after ${periodWords(pending?.value ?? "")}?`
        }
        description={
          pending?.kind === "off"
            ? "Azizam deletes the requests and replies it kept for you now. Pictures, audio and video in Library stay. This can’t be undone."
            : `Requests older than ${periodWords(pending?.value ?? "")} are deleted now. This can’t be undone.`
        }
        confirmLabel={pending?.kind === "off" ? "Turn off" : "Forget older requests"}
        onConfirm={async () => {
          if (!pending) return;
          if (pending.kind === "off") await put({ keep: false });
          else await put({ retention: pending.value });
        }}
      />
    </div>
  );
}
