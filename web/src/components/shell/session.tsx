"use client";

import { createContext, useContext } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { REAUTH_REQUIRED_CODE } from "@/lib/reauth";
import { networkFetch } from "@/lib/network-error";
import { observeViewer, personOf } from "@/lib/session-end";
import { formatPersonName } from "@/lib/format";

export interface Viewer {
  authenticated: boolean;
  /** The signed-in person's id; absent for service keys. */
  id?: string | null;
  /** The signed-in person's avatar: the choice and a gateway path for the sign-in picture. */
  avatar?: { choice: string; picture_url: string | null } | null;
  serviceAdmin?: boolean;
  name: string;
  email: string | null;
  role: string;
  admin: boolean;
}
export const ViewerContext = createContext<Viewer>({
  authenticated: false,
  name: "Your account",
  email: null,
  role: "member",
  admin: false,
});
export const useViewer = () => useContext(ViewerContext);

export async function fetchViewer(): Promise<Viewer> {
  const response = await networkFetch(
    "/me/api/session",
    { credentials: "same-origin", cache: "no-store" },
    true,
  );
  if (!response.ok) throw new Error("Your account could not be loaded.");
  const snapshot = await response.json();
  observeViewer({
    authenticated: snapshot.authenticated === true,
    epoch: typeof snapshot.session_view_id === "string" ? snapshot.session_view_id : null,
    person: personOf(snapshot),
  });
  if (snapshot.authenticated && snapshot.user) {
    const role = snapshot.user.role;
    return {
      authenticated: true,
      id: snapshot.user.person_id ?? snapshot.user.id ?? null,
      ...(snapshot.user.avatar && {
        avatar: {
          choice: snapshot.user.avatar.choice,
          picture_url: snapshot.user.avatar.picture_url ?? null,
        },
      }),
      name: formatPersonName(snapshot.user.name, "Your account"),
      email: snapshot.user.email,
      role,
      admin: ["owner", "superadmin", "admin"].includes(role),
    };
  }
  // Admin service credentials have no person or member-owned apps.
  try {
    const { actor } = await api.session();
    const serviceAdmin = Boolean(actor.kind && actor.kind !== "user");
    return {
      authenticated: true,
      serviceAdmin,
      name: serviceAdmin ? actor.label || "Service admin" : "Gateway owner",
      email: actor.email,
      role: actor.roles[0] || "member",
      admin: actor.roles.some((role) =>
        ["owner", "admin", "superadmin"].includes(role),
      ),
    };
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      ![401, 403].includes(error.status) ||
      error.code === REAUTH_REQUIRED_CODE
    )
      throw error;
    return {
      authenticated: false,
      name: "Your account",
      email: null,
      role: "member",
      admin: false,
    };
  }
}
export function useViewerQuery() {
  return useQuery({ queryKey: ["foundation", "viewer"], queryFn: fetchViewer });
}
