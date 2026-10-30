"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "@/lib/toast";
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
import { SignOutEverywhereSection } from "./sign-out-everywhere";
import { AccessSection } from "./access-section";
import { RequestHistorySection } from "./request-history-section";
import { accountApi } from "@/lib/account-api";
import { useOwnerName } from "@/lib/use-owner-name";
import {
  capLabel,
  defaultCap,
  defaultRetention,
  isKnownRetention,
  isRetention,
  isShorter,
  keepsForever,
  memberRetentionLabel,
  memberUnofferedNote,
  promptMemoryKey,
  purgeStatus,
  retentionChoices,
  withinCap,
  type MemberPromptMemoryResponse,
  type Retention,
  unofferedRetentionNote,
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
 * do. Owners use it for their own words too.
 */
export default function AccountPage() {
  const viewer = useViewer();
  return (
    // The settings template: a 720 column, the header 24 above sections that
    // are 32 apart.
    <div className="page-read">
      <PageHeader title="Your settings" />
      <div className="page-stack">
        {viewer.serviceAdmin ? (
          <ErrorState
            severity="info"
            heading="Sign in as a person"
            message="This session uses a key, which has no saved words or chats of its own."
            action={{ label: "Sign in", href: "/sign-in/" }}
          />
        ) : (
          <>
            <AccessSection />
            <PromptsSection />
            <RequestHistorySection />
            <SignOutEverywhereSection />
          </>
        )}
      </div>
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
            ? "You had no saved words or chats."
            : `Deleted words for ${count} ${count === 1 ? "creation" : "creations"}.`,
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
        heading="Couldn’t load these settings"
        error={error}
        onRetry={() => void refetch()}
        retrying={isRefetching}
      />
    );

  const memory = data.prompt_memory;
  const household = memory.household;
  const cap = isKnownRetention(household.retention_cap)
    ? household.retention_cap
    : defaultCap();
  const retention = isKnownRetention(memory.effective_retention)
    ? memory.effective_retention
    : defaultRetention();
  const legacyNote = memberUnofferedNote(
    data.unoffered?.retention,
    retention,
    cap,
  );
  const capLegacyNote = unofferedRetentionNote(
    data.unoffered?.retention_cap,
    cap,
    capLabel,
  );
  const retentionDescription =
    legacyNote || capLegacyNote
      ? [legacyNote, capLegacyNote && `Gateway limit: ${capLegacyNote}`]
          .filter(Boolean)
          .join(" ")
      : keepsForever(cap)
        ? "Creations stay in Library."
        : `Up to ${capLabel(cap).toLowerCase()} here.`;
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
        title="Saved words and chats"
        description="Library keeps the words you used to make each creation, so you can find it and make it again. Chats an app asks Azizam to keep follow this setting too."
      >
        {household.never_keep ? (
          <SettingRow
            label="Keep the words you used to make things"
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
                  aria-label="Keep the words you used to make things"
                />
              ) : (
                <Button asChild variant="outline">
                  <Link prefetch={false} href="/settings#prompts">
                    Open Settings
                  </Link>
                </Button>
              )
            }
          />
        ) : (
          <>
            <SettingRow
              label="Keep the words you used to make things"
              htmlFor="remember-prompts"
              description={
                memory.remember
                  ? "Turning it off deletes saved words and chats."
                  : "No words or chats are kept."
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
              label="Forget them after"
              // One line on a phone too: the label truncates before the
              // control wraps under it (DESIGN: phone rows are 64).
              className="max-sm:flex-nowrap"
              description={retentionDescription}
              wrapDescription={Boolean(legacyNote || capLegacyNote)}
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
                    aria-label="Forget them after"
                    className="sm:min-w-32"
                  >
                    <SelectValue>{memberRetentionLabel(retention)}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {retentionChoices().map((value) => (
                      <SelectItem
                        key={value}
                        value={value}
                        disabled={!withinCap(value, cap)}
                      >
                        {memberRetentionLabel(value)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              }
            />
            <SettingRow
              label="Delete saved words and chats"
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
          ? `No Azizam screen shows your words or chats to anyone else, ${owner} included. ${owner} runs this gateway, so they could still read its files or sign in as you with a reset link.`
          : "No Azizam screen shows your words or chats to anyone else. Pictures, audio and video stay in Library whatever you choose here."}
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
            ? "Stop keeping words and chats?"
            : pending?.kind === "retention"
              ? `Forget words and chats after ${memberRetentionLabel(pending.value).toLowerCase()}?`
              : "Delete your saved words and chats?"
        }
        description={
          pending?.kind === "off"
            ? "New words and chats won’t be saved. Deletion of saved ones may continue in the background. Your pictures, audio and videos stay in Library."
            : pending?.kind === "retention"
              ? `Words and chats older than ${memberRetentionLabel(pending.value).toLowerCase()} are deleted now. Your pictures, audio and videos stay in Library.`
              : "Every saved word and chat is deleted. Your pictures, audio and videos stay in Library. This can’t be undone."
        }
        confirmLabel={
          pending?.kind === "off"
            ? "Turn off"
            : pending?.kind === "retention"
              ? "Forget older words and chats"
              : "Delete words and chats"
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
