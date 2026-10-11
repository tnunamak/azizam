"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { adminFetch } from "@/lib/api";
import { useViewer } from "@/components/shell/session";
import { SettingsSection, SettingRow } from "@/components/product/setting-row";
import { ErrorState } from "@/components/product/error-state";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/lib/toast";

const receiptsKey = ["settings", "receipts"] as const;
const choices = {
  forever: "Forever",
  "365": "1 year",
  "90": "90 days",
  "30": "30 days",
};
type Choice = keyof typeof choices;
interface RetentionResponse {
  retention_days: number | null;
  revision: string;
  deleted_count?: number;
  cleanup_pending?: boolean;
}
interface Preview {
  cutoff: number | null;
  delete_count: number;
}

export function ReceiptsSection() {
  const owner = useViewer().role === "owner";
  const {
    data: queryData,
    isPending: queryIsPending,
    isError: queryIsError,
    error: queryError,
    refetch: queryRefetch,
  } = useQuery({
    queryKey: receiptsKey,
    queryFn: () =>
      adminFetch<RetentionResponse>("/admin/api/settings/receipts"),
    enabled: owner,
  });
  return (
    <SettingsSection
      id="receipts"
      title="Receipts"
      description="Request metadata keeps your usage and spend history. Prompts are not stored in receipts."
    >
      {!owner ? (
        <p className="text-muted-foreground text-meta p-4">
          Only the owner can change this.
        </p>
      ) : queryIsPending ? (
        <p role="status" className="text-muted-foreground text-meta p-4">
          Loading receipt settings…
        </p>
      ) : queryIsError ? (
        <div className="p-4">
          <ErrorState
            heading="Couldn’t load receipt settings"
            error={queryError}
            onRetry={() => void queryRefetch()}
          />
        </div>
      ) : (
        <RetentionForm key={queryData.revision} initial={queryData} />
      )}
    </SettingsSection>
  );
}

function RetentionForm({ initial }: { initial: RetentionResponse }) {
  const client = useQueryClient();
  const saved =
    initial.retention_days === null
      ? "forever"
      : (String(initial.retention_days) as Choice);
  const [choice, setChoice] = useState<Choice>(saved);
  const changed = choice !== saved;
  const finite = choice !== "forever";
  const {
    data: previewData,
    isFetching: previewIsFetching,
    isError: previewIsError,
    error: previewError,
    refetch: previewRefetch,
  } = useQuery({
    queryKey: ["settings", "receipts-preview", choice, initial.revision],
    queryFn: () =>
      adminFetch<Preview>(
        `/admin/api/settings/receipts/preview?retention_days=${choice}`,
      ),
    enabled: changed && finite,
    staleTime: 0,
  });
  const save = useMutation({
    mutationFn: () =>
      adminFetch<RetentionResponse>("/admin/api/settings/receipts", {
        method: "PUT",
        body: {
          retention_days: finite ? Number(choice) : null,
          preview: finite ? previewData : null,
        },
        headers: { "If-Match": initial.revision },
      }),
    onSuccess: (data) => {
      client.setQueryData(receiptsKey, data);
      void client.invalidateQueries({ queryKey: ["usage"] });
      void client.invalidateQueries({ queryKey: ["settings", "effective"] });
      toast.success(
        data.cleanup_pending
          ? "Receipt setting saved. Cleanup is pending and will retry."
          : "Receipt setting saved",
      );
    },
    onError: () => {
      void client.invalidateQueries({ queryKey: receiptsKey });
      void previewRefetch();
    },
  });
  const ready =
    changed &&
    (!finite || (previewData && !previewIsFetching && !previewIsError));
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) save.mutate();
      }}
    >
      <SettingRow
        label="Keep receipts"
        htmlFor="keep-receipts"
        description={`Saved: ${choices[saved]}. Shorter periods delete usage and spend history.`}
        control={
          <Select
            value={choice}
            disabled={save.isPending}
            onValueChange={(next) => setChoice(next as Choice)}
          >
            <SelectTrigger
              id="keep-receipts"
              aria-label="Keep receipts"
              className="sm:min-w-32"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(["forever", "365", "90", "30"] as const).map((value) => (
                <SelectItem key={value} value={value}>
                  {choices[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />
      <div className="space-y-3 p-4">
        {changed &&
          finite &&
          (previewIsFetching ? (
            <p role="status">Counting older receipts…</p>
          ) : previewIsError ? (
            <ErrorState
              heading="Couldn’t preview deletion"
              error={previewError}
              onRetry={() => void previewRefetch()}
            />
          ) : previewData ? (
            <p role="status">
              Saving will permanently delete{" "}
              {previewData.delete_count.toLocaleString()} receipts older than{" "}
              {choices[choice].toLowerCase()}. Nothing is deleted until you
              save.
            </p>
          ) : null)}
        {changed && !finite && (
          <p role="status">
            Receipts will be kept until you delete them. Deleted history cannot
            be restored.
          </p>
        )}
        {save.isError && (
          <p role="alert" className="text-destructive text-meta">
            {save.error instanceof Error
              ? save.error.message
              : "The setting wasn’t saved. Try again."}
          </p>
        )}
        <Button type="submit" disabled={!ready || save.isPending}>
          {save.isPending ? "Saving…" : "Save receipt setting"}
        </Button>
      </div>
    </form>
  );
}
