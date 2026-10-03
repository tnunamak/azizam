"use client";

import { createContext, useContext } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { formatPersonName } from "@/lib/format";

export interface Viewer {
  authenticated: boolean;
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
  const response = await fetch("/me/api/session", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Your account could not be loaded.");
  const snapshot = await response.json();
  if (snapshot.authenticated && snapshot.user) {
    const role = snapshot.user.role;
    return {
      authenticated: true,
      name: formatPersonName(snapshot.user.name, "Your account"),
      email: snapshot.user.email,
      role,
      admin: ["owner", "superadmin", "admin"].includes(role),
    };
  }
  // Older owner sign-in cookies are admin principals, without a member row.
  try {
    const { actor } = await api.session();
    return {
      authenticated: true,
      name: "Gateway owner",
      email: actor.email,
      role: actor.roles[0] || "member",
      admin: actor.roles.some((role) =>
        ["owner", "admin", "superadmin"].includes(role),
      ),
    };
  } catch (error) {
    if (!(error instanceof ApiError) || ![401, 403].includes(error.status))
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
