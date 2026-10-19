"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  choiceFromSelection,
  selectionFromChoice,
  type AvatarSelection,
} from "@/components/product/person-avatar";
import { ApiError } from "@/lib/api";
import { accountApi } from "@/lib/account-api";
import { useViewer, type Viewer } from "./session";

/** The props the account menu's avatar picker takes, from the viewer and one save. */
export function useAvatarSetting() {
  const viewer = useViewer();
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: (choice: string) => accountApi.saveAvatar(choice),
    onSuccess: (saved) => {
      client.setQueryData<Viewer>(["foundation", "viewer"], (current) =>
        current
          ? {
              ...current,
              avatar: { choice: saved.choice, picture_url: saved.picture_url },
            }
          : current,
      );
      void client.invalidateQueries({ queryKey: ["avatars"] });
    },
  });
  return {
    avatarPersonId: viewer.id ?? null,
    avatarSelection: selectionFromChoice(viewer.avatar?.choice),
    signInPictureUrl: viewer.avatar?.picture_url ?? null,
    onAvatarChange: (selection: AvatarSelection) =>
      save.mutate(choiceFromSelection(selection)),
    avatarPending: save.isPending,
    avatarError: save.isError
      ? save.error instanceof ApiError && save.error.status === 409
        ? save.error.message
        : "We couldn't save your avatar. Try again."
      : null,
  };
}
