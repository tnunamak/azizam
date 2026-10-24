"use client";

import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  choiceFromSelection,
  selectionFromChoice,
  type AvatarSelection,
} from "@/components/product/person-avatar";
import { ApiError } from "@/lib/api";
import { accountApi, type AvatarSetting } from "@/lib/account-api";
import { useViewer, type Viewer } from "./session";

/** Show a saved avatar in the viewer, and refresh the lists that draw other people's avatars. */
export function applySavedAvatar(client: QueryClient, saved: AvatarSetting) {
  client.setQueryData<Viewer>(["foundation", "viewer"], (current) =>
    current
      ? {
          ...current,
          avatar: {
            choice: saved.choice,
            picture_url: saved.picture_url,
            sign_in_picture_url: saved.sign_in_picture_url ?? null,
            made_picture_url: saved.made_picture_url ?? null,
          },
        }
      : current,
  );
  void client.invalidateQueries({ queryKey: ["avatars"] });
}

/** The props the account menu's avatar picker takes, from the viewer and one save. */
export function useAvatarSetting() {
  const viewer = useViewer();
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: (choice: string) => accountApi.saveAvatar(choice),
    onSuccess: (saved) => applySavedAvatar(client, saved),
  });
  return {
    avatarPersonId: viewer.id ?? null,
    avatarSelection: selectionFromChoice(viewer.avatar?.choice),
    /** The picture the avatar shows now: the sign-in picture, or the one the gateway made. */
    avatarPictureUrl: viewer.avatar?.picture_url ?? null,
    avatarSignInPictureUrl: viewer.avatar?.sign_in_picture_url ?? null,
    avatarMadePictureUrl: viewer.avatar?.made_picture_url ?? null,
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
