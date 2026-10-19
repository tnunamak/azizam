"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "@/lib/toast";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { ErrorState } from "@/components/product/error-state";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiError } from "@/lib/api";
import {
  capLabel,
  defaultCap,
  householdPromptMemoryApi,
  isKnownRetention,
  isRetention,
  isShorter,
  promptMemoryKey,
  purgeStatus,
  retentionChoices,
  type HouseholdPromptMemory,
  type HouseholdPromptMemoryResponse,
  type Retention,
  unofferedRetentionNote,
} from "@/lib/prompt-memory";

const householdKey = ["settings", "prompt-memory"] as const;

type Pending = { kind: "never-keep" } | { kind: "cap"; value: Retention };

/**
 * The household's prompt rules on the owner's Settings: the kill switch,
 * the starting choice for new people, and the longest time anyone's prompts
 * are kept. Self-contained, so the Settings page only places it.
 */
export function HouseholdPromptsSection() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<Pending | null>(null);
  const { data, isPending, isError, error, refetch, isRefetching } = useQuery({
    queryKey: householdKey,
    queryFn: householdPromptMemoryApi.get,
    refetchInterval: (query) =>
      query.state.data?.prompt_purge?.pending ? 15_000 : false,
  });
  const save = useMutation({
    mutationFn: (value: HouseholdPromptMemory) =>
      householdPromptMemoryApi.put(value, data?.revision ?? ""),
    onSuccess: (next) => {
      queryClient.setQueryData<HouseholdPromptMemoryResponse>(
        householdKey,
        next,
      );
      // The owner's own Your settings page reads the household's rules.
      void queryClient.invalidateQueries({ queryKey: promptMemoryKey });
    },
    onError: (failure) => {
      toast.error(
        failure instanceof ApiError && failure.status === 409
          ? "Settings changed elsewhere. The latest are shown; try again."
          : failure instanceof Error && failure.message
            ? failure.message
            : "The setting wasn’t saved. Try again.",
      );
      void refetch();
    },
  });

  return (
    <div>
      <SettingsSection
        id="prompts"
        title="Prompts"
        description="Each person’s prompts are visible only to them."
      >
        {isPending ? (
          <div
            role="status"
            aria-label="Loading prompt settings"
            aria-busy="true"
            className="space-y-2 p-4"
          >
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-3/4" />
          </div>
        ) : error instanceof ApiError && error.status === 403 ? (
          <p role="alert" className="text-muted-foreground text-meta p-4">
            Only the owner can change this.
          </p>
        ) : isError || !data?.prompt_memory ? (
          <div className="p-4">
            <ErrorState
              heading="Couldn’t load prompt settings"
              error={error}
              viewerRole="owner"
              onRetry={() => void refetch()}
              retrying={isRefetching}
            />
          </div>
        ) : (
          <Rows
            value={data.prompt_memory}
            unofferedCap={data.unoffered?.retention_cap}
            status={purgeStatus(data.prompt_purge)}
            busy={save.isPending}
            onChange={(next) => void save.mutateAsync(next).catch(() => {})}
            onAsk={setPending}
          />
        )}
      </SettingsSection>
      <p className="text-muted-foreground text-meta mt-3">
        No Azizam screen shows anyone’s prompts to anyone else, you included.
        You run this gateway, so you could still read its files. Your own choice
        is in{" "}
        <Link
          prefetch={false}
          href="/account"
          className="text-foreground underline underline-offset-4"
        >
          Your settings
        </Link>
        .
      </p>
      {data?.prompt_memory ? (
        <ConfirmDialog
          open={pending !== null}
          onOpenChange={(open) => !open && setPending(null)}
          title={
            pending?.kind === "cap"
              ? `Keep prompts for at most ${capLabel(pending.value).toLowerCase()}?`
              : "Never keep prompts on this gateway?"
          }
          description={
            pending?.kind === "cap"
              ? `Everyone’s prompts older than ${capLabel(pending.value).toLowerCase()} are deleted now. Pictures, audio and video stay in Library.`
              : "Nothing new will be kept. Deletion of everyone’s saved prompts may continue in the background. Turning this off later can’t bring them back. Pictures, audio and video stay in Library. Azizam cannot erase an engine’s own history, external output folders, backups, or downloaded copies."
          }
          confirmLabel={
            pending?.kind === "cap"
              ? "Delete older prompts"
              : "Never keep prompts"
          }
          onConfirm={async () => {
            if (!pending) return;
            await save.mutateAsync(
              pending.kind === "cap"
                ? { ...data.prompt_memory, retention_cap: pending.value }
                : { ...data.prompt_memory, never_keep: true },
            );
          }}
        />
      ) : null}
    </div>
  );
}

function Rows({
  value,
  unofferedCap,
  status,
  busy,
  onChange,
  onAsk,
}: {
  value: HouseholdPromptMemory;
  unofferedCap?: Retention;
  status: string | null;
  busy: boolean;
  onChange: (next: HouseholdPromptMemory) => void;
  onAsk: (pending: Pending) => void;
}) {
  const cap = isKnownRetention(value.retention_cap)
    ? value.retention_cap
    : defaultCap();
  const legacyNote = unofferedRetentionNote(unofferedCap, cap, capLabel);
  return (
    <>
      <SettingRow
        label="Never keep prompts"
        htmlFor="never-keep-prompts"
        description="Deletes saved prompts and wins over everyone’s own choice."
        control={
          <Switch
            id="never-keep-prompts"
            checked={value.never_keep}
            disabled={busy}
            onCheckedChange={(next) =>
              next
                ? onAsk({ kind: "never-keep" })
                : onChange({ ...value, never_keep: false })
            }
          />
        }
      />
      <SettingRow
        label="Remember prompts for new people"
        htmlFor="default-remember-prompts"
        description="They can change it later."
        control={
          <Switch
            id="default-remember-prompts"
            checked={value.default_remember}
            disabled={busy || value.never_keep}
            onCheckedChange={(next) =>
              onChange({ ...value, default_remember: next })
            }
          />
        }
      />
      <SettingRow
        label="Keep prompts for"
        // One line on a phone too: the label truncates before the
        // control wraps under it (DESIGN: phone rows are 64).
        className="max-sm:flex-nowrap"
        description={legacyNote ?? "The longest, for everyone."}
        wrapDescription={Boolean(legacyNote)}
        control={
          <Select
            value={cap}
            disabled={busy || value.never_keep}
            onValueChange={(next) => {
              if (!isRetention(next) || next === cap) return;
              if (isShorter(next, cap)) onAsk({ kind: "cap", value: next });
              else onChange({ ...value, retention_cap: next });
            }}
          >
            <SelectTrigger
              size="sm"
              aria-label="Keep prompts for"
              className="sm:min-w-32"
            >
              <SelectValue>{capLabel(cap)}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {retentionChoices().map((option) => (
                <SelectItem key={option} value={option}>
                  {capLabel(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />
      {status ? (
        <div role="status" className="flex gap-3 px-4 py-3">
          <Loader2
            aria-hidden="true"
            className="text-muted-foreground mt-0.5 size-4 shrink-0 animate-spin motion-reduce:animate-none"
          />
          <div className="min-w-0">
            <p className="text-body font-medium">Deletion pending</p>
            <p className="text-muted-foreground text-meta">{status}</p>
          </div>
        </div>
      ) : null}
    </>
  );
}
