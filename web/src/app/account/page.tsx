"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { useViewer } from "@/components/shell/session";
import { PageHeader } from "@/components/product/page-header";
import { ErrorState } from "@/components/product/error-state";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { Button } from "@/components/ui/button";
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
import { accountApi } from "@/lib/account-api";
import { useOwnerName } from "@/lib/use-owner-name";
import {
  RETENTIONS,
  capLabel,
  isRetention,
  isShorter,
  memberRetentionLabel,
  promptMemoryKey,
  purgeStatus,
  withinCap,
  type MemberPromptMemoryResponse,
  type Retention,
} from "@/lib/prompt-memory";

type Pending =
  | { kind: "off" }
  | { kind: "retention"; value: Retention }
  | { kind: "delete" };

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

/**
 * A person's own settings (DESIGN "Settings page"): a 720 column, one card
 * of rows per section. Members have no Settings in the sidebar, so this
 * lives under the account menu, as Linear's and Vercel's personal settings
 * do. Owners use it for their own prompts too.
 */
export default function AccountPage() {
  const viewer = useViewer();
  return (
    <div className="page-read page-stack">
      <PageHeader title="Your settings" />
      {viewer.serviceAdmin ? (
        <ErrorState
          severity="info"
          heading="Sign in as a person"
          message="This session uses a key, which has no prompts of its own."
          action={{ label: "Sign in", href: "/ui/sign-in/" }}
        />
      ) : (
        <PromptsSection />
      )}
    </div>
  );
}

function PromptsSection() {
  const viewer = useViewer();
  const ownerName = useOwnerName();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<Pending | null>(null);
  const { data, isPending, isError, error, refetch, isRefetching } = useQuery({
    queryKey: promptMemoryKey,
    queryFn: accountApi.promptMemory,
    // Cleanup of older files runs in the background; follow it until done.
    refetchInterval: (query) =>
      query.state.data?.prompt_purge?.pending ? 15_000 : false,
  });
  const save = useMutation({
    mutationFn: accountApi.savePromptMemory,
    onSuccess: (next) => {
      queryClient.setQueryData<MemberPromptMemoryResponse>(
        promptMemoryKey,
        next,
      );
    },
    onError: (failure) => {
      toast.error(
        errorMessage(failure, "Your setting wasn’t saved. Try again."),
      );
      void queryClient.invalidateQueries({ queryKey: promptMemoryKey });
    },
  });
  const remove = useMutation({
    mutationFn: accountApi.deleteSavedPrompts,
    onSuccess: (result) => {
      const count = result.deleted_prompts;
      toast.success(
        result.prompt_purge?.pending
          ? "Deletion pending. Azizam will keep retrying."
          : count === 0
            ? "You had no saved prompts."
            : `Deleted ${count} saved ${count === 1 ? "prompt" : "prompts"}.`,
      );
      void queryClient.invalidateQueries({ queryKey: promptMemoryKey });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: promptMemoryKey });
    },
  });

  if (isPending)
    return (
      <div role="status" aria-label="Loading your settings" aria-busy="true">
        <Skeleton className="mb-3 h-6 w-32" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  if (isError || !data?.prompt_memory)
    return (
      <ErrorState
        heading="Couldn’t load your prompt settings"
        error={error}
        onRetry={() => void refetch()}
        retrying={isRefetching}
      />
    );

  const memory = data.prompt_memory;
  const household = memory.household;
  const cap = isRetention(household.retention_cap)
    ? household.retention_cap
    : "never";
  const retention = isRetention(memory.effective_retention)
    ? memory.effective_retention
    : "never";
  const status = purgeStatus(data.prompt_purge);
  const owner = viewer.admin ? null : (ownerName ?? "The gateway owner");
  const busy = save.isPending || remove.isPending;

  function put(remember: boolean, nextRetention: Retention) {
    return save.mutateAsync({ remember, retention: nextRetention });
  }

  return (
    <div>
      <SettingsSection
        id="prompts"
        title="Prompts"
        description="Choose whether Azizam saves what you type with what it makes."
      >
        {household.never_keep ? (
          <SettingRow
            label="Remember what I type"
            description={
              owner
                ? `${owner} turned this off for everyone.`
                : "Off for everyone, in Settings."
            }
            control={
              owner ? (
                <Switch
                  checked={false}
                  disabled
                  aria-label="Remember what I type"
                />
              ) : (
                <Button asChild variant="outline">
                  <Link href="/settings#prompts">Open Settings</Link>
                </Button>
              )
            }
          />
        ) : (
          <>
            <SettingRow
              label="Remember what I type"
              htmlFor="remember-prompts"
              description={
                memory.remember
                  ? "Turning it off deletes saved prompts."
                  : "Nothing you type is kept."
              }
              control={
                <Switch
                  id="remember-prompts"
                  checked={memory.remember}
                  disabled={busy}
                  onCheckedChange={(next) => {
                    if (next) void put(true, memory.retention).catch(() => {});
                    else setPending({ kind: "off" });
                  }}
                />
              }
            />
            <SettingRow
              label="Forget prompts after"
              // One line on a phone too: the label truncates before the
              // control wraps under it (DESIGN: phone rows are 64).
              className="max-sm:flex-nowrap"
              description={
                cap === "never"
                  ? "Creations stay in Library."
                  : `Up to ${capLabel[cap].toLowerCase()} here.`
              }
              control={
                <Select
                  value={retention}
                  disabled={busy || !memory.remember}
                  onValueChange={(value) => {
                    if (!isRetention(value) || value === retention) return;
                    if (isShorter(value, retention))
                      setPending({ kind: "retention", value });
                    else void put(memory.remember, value).catch(() => {});
                  }}
                >
                  <SelectTrigger
                    size="sm"
                    aria-label="Forget prompts after"
                    className="sm:min-w-32"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {RETENTIONS.map((value) => (
                      <SelectItem
                        key={value}
                        value={value}
                        disabled={!withinCap(value, cap)}
                      >
                        {memberRetentionLabel[value]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              }
            />
            <SettingRow
              label="Delete my saved prompts"
              // One line on a phone too: the label truncates before the
              // control wraps under it (DESIGN: phone rows are 64).
              className="max-sm:flex-nowrap"
              description="Deletes all of them now."
              control={
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => setPending({ kind: "delete" })}
                >
                  {remove.isPending ? (
                    <Loader2 aria-hidden="true" className="animate-spin" />
                  ) : null}
                  Delete…
                </Button>
              }
            />
          </>
        )}
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
      </SettingsSection>
      <p className="text-muted-foreground text-meta mt-3">
        {owner
          ? `No Azizam screen shows your prompts to anyone else, ${owner} included. ${owner} runs this gateway, so they could still read its files or sign in as you with a reset link.`
          : "No Azizam screen shows your prompts to anyone else. Pictures, audio and video stay in Library whatever you choose here."}
      </p>
      <p className="text-muted-foreground text-meta mt-3">
        Azizam cannot erase an engine’s own history, external output folders,
        backups, or downloaded copies.
      </p>
      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => !open && setPending(null)}
        title={
          pending?.kind === "off"
            ? "Stop remembering what you type?"
            : pending?.kind === "retention"
              ? `Forget prompts after ${memberRetentionLabel[pending.value].toLowerCase()}?`
              : "Delete your saved prompts?"
        }
        description={
          pending?.kind === "off"
            ? "New prompts won’t be saved. Deletion of saved prompts may continue in the background. Your pictures, audio and videos stay in Library."
            : pending?.kind === "retention"
              ? `Prompts older than ${memberRetentionLabel[pending.value].toLowerCase()} are deleted now. Your pictures, audio and videos stay in Library.`
              : "Every prompt saved for you is deleted. Your pictures, audio and videos stay in Library. This can’t be undone."
        }
        confirmLabel={
          pending?.kind === "off"
            ? "Turn off"
            : pending?.kind === "retention"
              ? "Forget older prompts"
              : "Delete prompts"
        }
        onConfirm={async () => {
          if (!pending) return;
          if (pending.kind === "off") await put(false, memory.retention);
          else if (pending.kind === "retention")
            await put(memory.remember, pending.value);
          else
            await remove.mutateAsync().catch((failure) => {
              toast.error(
                errorMessage(
                  failure,
                  "Deletion could not be confirmed. Reload Your settings to see whether it is pending.",
                ),
              );
              throw failure;
            });
        }}
      />
    </div>
  );
}
