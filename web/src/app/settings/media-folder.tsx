"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useViewer } from "@/components/shell/session";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { Button } from "@/components/ui/button";
import {
  folderChangedSentence,
  folderToConfirm,
  libraryEvidenceSentence,
  noneHereSentence,
  type MediaFolderView,
} from "@/lib/media-folder";
import { toast } from "@/lib/toast";

const mediaFolderKey = ["settings", "media-folder"] as const;

// The gateway's own sentence for a refusal, shown as it is.
class Refused extends Error {}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const failure: { error?: { code?: unknown; message?: unknown } } | null =
      await response.json().catch(() => null);
    const error = failure?.error;
    if (
      typeof error?.code === "string" &&
      error.code.startsWith("media_folder_") &&
      typeof error.message === "string"
    )
      throw new Refused(error.message);
    throw new Error("We couldn’t confirm the media folder. Try again.");
  }
  return (await response.json()) as T;
}

/**
 * The owner's one-time confirmation of where the Library's files are kept,
 * outside the Library and its dialogs. A member's Remove missing waits for
 * it, and the owner is otherwise asked only when removing their own missing
 * items. Shown only while the folder is unconfirmed or looks different.
 */
export function MediaFolderSection() {
  const owner = useViewer().role === "owner";
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: mediaFolderKey,
    enabled: owner,
    staleTime: 15_000,
    retry: false,
    queryFn: async () => {
      const page = await request<{ media_folder?: MediaFolderView | null }>(
        "/me/api/media?limit=1",
      );
      return page.media_folder ?? null;
    },
  });
  const confirm = useMutation({
    mutationFn: async () => {
      const session = await request<{ csrf_token: string }>("/me/api/session");
      return request<{ confirmed: boolean }>("/me/api/media/confirm-folder", {
        method: "POST",
        headers: {
          "x-azizam-user-action": "1",
          "x-azizam-csrf": session.csrf_token,
        },
      });
    },
    onSuccess: () => {
      toast.success("Media folder confirmed.");
      void queryClient.invalidateQueries({ queryKey: mediaFolderKey });
    },
    onError: (error) =>
      toast.error(
        error instanceof Error && error.message
          ? error.message
          : "We couldn’t confirm the media folder. Try again.",
      ),
  });

  const folder = folderToConfirm(data);
  if (!owner || !folder || folder.library_items == null) return null;
  if (folder.library_items === 0 && !folder.uncheckable_items) return null;
  const path = folder.path ?? "the media folder";
  const checked = folder.library_items;
  const here = folder.items_here ?? 0;
  const noneHere = checked > 0 && here === 0;
  return (
    <SettingsSection
      id="media-folder"
      title="Media folder"
      description="Where Azizam keeps the Library’s files."
    >
      <SettingRow
        label="Confirm the media folder"
        wrapDescription
        description={
          <>
            {folder.state === "mismatch" ? (
              <span className="block">{folderChangedSentence(path)}</span>
            ) : null}
            <span className="block">
              {checked === 0
                ? `Azizam can't check the existing Library entries against ${path}, so it can't tell whether this is the media folder. Only confirm if your media disk is connected and this is where your photos, videos and audio are kept.`
                : noneHere
                ? noneHereSentence(path, checked)
                : libraryEvidenceSentence(path, checked, here)}
            </span>
            <span className="block">
              Until you confirm it, members can’t remove their missing items.
              Confirming removes nothing.
            </span>
          </>
        }
        control={
          <Button
            size="sm"
            disabled={noneHere || confirm.isPending}
            onClick={() => confirm.mutate()}
          >
            {confirm.isPending ? (
              <Loader2 className="animate-spin" aria-hidden />
            ) : null}
            Confirm the media folder
          </Button>
        }
      />
    </SettingsSection>
  );
}
