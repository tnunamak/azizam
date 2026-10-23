"use client";

import Link from "next/link";
import { useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { History } from "lucide-react";
import { toast } from "@/lib/toast";
import { accountApi } from "@/lib/account-api";
import { EmptyState } from "@/components/product/empty-state";
import { ErrorState } from "@/components/product/error-state";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useModelName } from "@/lib/use-model-name";
import {
  periodWords,
  requestHistoryKey,
  requestHistoryListKey,
  requestKind,
  statusWords,
  type RequestHistoryItem,
} from "@/lib/request-history";
import { HistoryDetail } from "./history-detail";
import { RequestTime } from "./request-time";

const rowClass =
  "flex min-h-11 w-full min-w-0 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-left hover:bg-muted/50 focus-visible:bg-muted/50";

/** The person's kept requests, newest first, with the one-request and delete-all actions. */
export function HistoryList() {
  const queryClient = useQueryClient();
  const nameModel = useModelName();
  const [openId, setOpenId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"all" | { one: string } | null>(null);
  const choice = useQuery({
    queryKey: requestHistoryKey,
    queryFn: accountApi.requestHistory,
  });
  const list = useInfiniteQuery({
    queryKey: requestHistoryListKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => accountApi.requestHistoryPage(pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: false,
  });
  const refreshList = () =>
    queryClient.invalidateQueries({ queryKey: requestHistoryListKey });
  const removeOne = useMutation({
    mutationFn: (recordId: string) =>
      accountApi.deleteRequestHistoryRecord(recordId),
    onSuccess: () => {
      setOpenId(null);
      toast.success("Deleted this request.");
      void refreshList();
    },
    onError: (failure) => {
      toast.error(
        failure instanceof Error && failure.message
          ? failure.message
          : "This request wasn’t deleted. Try again.",
      );
      void refreshList();
    },
  });
  const removeAll = useMutation({
    mutationFn: accountApi.deleteAllRequestHistory,
    onSuccess: (result) => {
      toast.success(
        result.pending
          ? "Deletion pending. Azizam will keep retrying."
          : "Deleted all request history.",
      );
      void refreshList();
    },
    onError: (failure) => {
      toast.error(
        failure instanceof Error && failure.message
          ? failure.message
          : "Your history wasn’t deleted. Try again.",
      );
      void refreshList();
    },
  });

  if (list.isPending)
    return (
      <div role="status" aria-label="Loading your history" aria-busy="true">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="mt-2 h-16 w-full" />
      </div>
    );
  if (list.isError)
    return (
      <ErrorState
        heading="Couldn’t load your history"
        error={list.error}
        onRetry={() => void list.refetch()}
        retrying={list.isRefetching}
      />
    );

  const rows: RequestHistoryItem[] =
    list.data?.pages.flatMap((page) => page.requests) ?? [];
  // The owner's switch comes first: when it is off for everyone, the member's own choice changes nothing.
  const householdOff = choice.data?.household_off === true;
  const keeps = !householdOff && (choice.data?.keep ?? true);
  // The period is the gateway's; until the choice has loaded, the screen says nothing about it.
  const period = choice.data ? periodWords(choice.data.retention) : null;

  if (rows.length === 0)
    return (
      <EmptyState
        icon={History}
        title={householdOff ? "History is off on this gateway" : keeps ? "No requests yet" : "History is off"}
        description={
          householdOff
            ? "The owner has turned request history off for everyone, so nothing new is kept."
            : keeps
              ? `Requests your apps send are kept here${period ? ` for ${period}` : ""}. Try a request to see one.`
              : "Azizam doesn’t keep new requests. Turn it on in Your settings to keep them here."
        }
        action={
          householdOff ? undefined : keeps ? (
            <Button asChild size="sm">
              <Link prefetch={false} href="/try">
                Open Try
              </Link>
            </Button>
          ) : (
            <Button asChild size="sm">
              <Link prefetch={false} href="/account#history">
                Open Your settings
              </Link>
            </Button>
          )
        }
      />
    );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground text-meta">
          {householdOff
            ? "History is off on this gateway. Nothing new is kept."
            : keeps
              ? period
                ? `Kept for ${period}.`
                : "Kept for as long as your settings allow."
              : "History is off. Nothing new is kept."}
        </p>
        <Button
          variant="outline"
          size="sm"
          disabled={removeAll.isPending}
          onClick={() => setConfirm("all")}
        >
          Delete all history
        </Button>
      </div>
      <ul aria-label="Kept requests" className="divide-border divide-y rounded-lg border">
        {rows.map((item) => {
          const kind = requestKind(item.summary.route);
          const model = item.summary.model ? nameModel(item.summary.model) : null;
          // The model's name leads, and what was asked is the line under it. The kind stands in when nothing else does.
          const title = model ?? kind.label;
          const asked = item.summary.preview ?? (model ? kind.label : null);
          return (
            <li key={item.record_id}>
              <button
                type="button"
                className={rowClass}
                onClick={() => setOpenId(item.record_id)}
              >
                <RequestTime
                  at={item.created_at}
                  className="text-muted-foreground text-meta w-full sm:w-36 sm:shrink-0"
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{title}</span>
                  {asked ? (
                    <span className="text-muted-foreground text-meta block truncate">
                      {asked}
                    </span>
                  ) : null}
                </span>
                <span className="text-muted-foreground text-meta w-24 truncate">
                  {item.app_name ?? "Browser"}
                </span>
                <span className="text-meta w-24 text-right">
                  {statusWords(item.summary.status)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {list.hasNextPage ? (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={list.isFetchingNextPage}
            onClick={() => void list.fetchNextPage()}
          >
            {list.isFetchingNextPage ? "Loading more…" : "Load more"}
          </Button>
        </div>
      ) : null}
      <HistoryDetail
        recordId={openId}
        app={rows.find((row) => row.record_id === openId)?.app_name ?? null}
        onOpenChange={(open) => !open && setOpenId(null)}
        onRequestDelete={(recordId) => setConfirm({ one: recordId })}
      />
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(open) => !open && setConfirm(null)}
        title={
          confirm === "all"
            ? "Delete all request history?"
            : "Delete this request?"
        }
        description={
          confirm === "all"
            ? "Azizam deletes every request and reply it kept for you. Pictures, audio and video in Library stay. This can’t be undone."
            : "Azizam deletes what it kept for this request: what was sent and what came back. Receipts and Library items stay."
        }
        confirmLabel={confirm === "all" ? "Delete all" : "Delete request"}
        onConfirm={async () => {
          if (confirm === "all") await removeAll.mutateAsync();
          else if (confirm) await removeOne.mutateAsync(confirm.one);
        }}
      />
    </div>
  );
}
