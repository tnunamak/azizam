"use client";

import { useEffect, useRef } from "react";
import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  choiceFromSelection,
  selectionFromChoice,
  type AvatarSelection,
} from "@/components/product/person-avatar";
import { ApiError } from "@/lib/api";
import { accountApi, type AvatarSetting } from "@/lib/account-api";
import { useViewer, type Viewer } from "./session";

/** How often the page reads the avatar while the automatic picture is being made. */
const MAKING_POLL_MS = 2_000;

/** Show a saved avatar in the viewer. The lists that draw other people's avatars are left as they are. */
export function showSavedAvatar(client: QueryClient, saved: AvatarSetting) {
  // A reply without the avatar's choice is not an avatar: the page keeps what it shows.
  if (typeof saved.choice !== "string") return;
  client.setQueryData<Viewer>(["foundation", "viewer"], (current) =>
    current
      ? {
          ...current,
          avatar: {
            choice: saved.choice,
            picture_url: saved.picture_url,
            sign_in_picture_url: saved.sign_in_picture_url ?? null,
            made_picture_url: saved.made_picture_url ?? null,
            generating: saved.generating === true,
          },
        }
      : current,
  );
}

/** Show a saved avatar in the viewer, and refresh the lists that draw other people's avatars. */
export function applySavedAvatar(client: QueryClient, saved: AvatarSetting) {
  showSavedAvatar(client, saved);
  void client.invalidateQueries({ queryKey: ["avatars"] });
}

/**
 * The automatic picture for a person with no sign-in picture and no chosen avatar. The page asks the gateway once
 * per load; the gateway makes that one attempt and never repeats it. While the picture is being made, the page
 * reads the avatar until it has settled, so the picture shows once it is kept.
 */
function useAutomaticAvatar(): void {
  const viewer = useViewer();
  const client = useQueryClient();
  const avatar = viewer.avatar;
  const askGateway = Boolean(viewer.id) && avatar?.choice === "auto" && !avatar.picture_url && !avatar.generating;
  const making = avatar?.generating === true;
  // Ask once per person on this page load. A session refresh that reads the avatar before the gateway has
  // recorded the make must not ask again; the gateway would refuse a second attempt anyway.
  const askedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!askGateway || askedFor.current === viewer.id) return;
    askedFor.current = viewer.id ?? null;
    accountApi
      .startDefaultAvatar()
      .then((saved) => showSavedAvatar(client, saved))
      .catch(() => undefined);
  }, [askGateway, client, viewer.id]);

  useEffect(() => {
    if (!making) return;
    const timer = window.setInterval(() => {
      accountApi
        .avatar()
        .then((saved) => showSavedAvatar(client, saved))
        .catch(() => undefined);
    }, MAKING_POLL_MS);
    return () => window.clearInterval(timer);
  }, [making, client]);
}

/** Asks for the automatic picture once per page load. Mount it once, inside the viewer's provider. */
export function AutomaticAvatar(): null {
  useAutomaticAvatar();
  return null;
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
