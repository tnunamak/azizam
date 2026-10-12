"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import {
  ChevronDown,
  CircleHelp,
  KeyRound,
  LogOut,
  UserRoundCog,
} from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "@/lib/toast";
import { PersonMark } from "@/components/product/person-mark";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useViewer } from "./session";

const roleLabels: Record<string, string> = {
  owner: "Owner",
  superadmin: "Owner",
  admin: "Admin",
};

export function AccountMenuContent({
  compact = false,
  open,
  onOpenChange,
}: {
  compact?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const viewer = useViewer();
  const signingOutRef = useRef(false);
  const [signingOut, setSigningOut] = useState(false);

  async function signOut() {
    if (signingOutRef.current) return;
    signingOutRef.current = true;
    setSigningOut(true);
    try {
      const member = await fetch("/me/api/session", {
        credentials: "same-origin",
        cache: "no-store",
      });
      const snapshot = await member.json();
      const csrf =
        snapshot.csrf_token || (await api.session()).actor.csrf_token;
      const response = await fetch("/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "x-azizam-csrf": csrf, accept: "application/json" },
      });
      if (!response.ok) throw new Error("Sign out failed");
      window.location.assign("/ui/sign-in/");
    } catch {
      signingOutRef.current = false;
      setSigningOut(false);
      toast.error("We couldn't confirm you were signed out. Refresh this page and try again.");
    }
  }

  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={
            compact
              ? "rounded-control flex size-11 items-center justify-center"
              : "shell-nav-footer w-full justify-center px-2 lg:justify-start lg:px-3"
          }
          aria-label={`Account menu: ${viewer.name}`}
        >
          <PersonMark name={viewer.name} size={compact ? "md" : "sm"} />
          {!compact && (
            <span className="shell-nav-label text-chrome-foreground min-w-0 flex-1 truncate text-left font-medium">
              {viewer.name}
            </span>
          )}
          {!compact && <ChevronDown className="shell-nav-label size-4" />}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel className="font-normal">
          <span className="block font-medium">{viewer.name}</span>
          <span className="text-muted-foreground block text-xs">
            {viewer.serviceAdmin
              ? "Admin key"
              : (roleLabels[viewer.role] ?? "Member")}
          </span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          {!viewer.serviceAdmin && (
            <DropdownMenuItem asChild>
              <Link href="/apps">
                <KeyRound />
                Apps & keys
              </Link>
            </DropdownMenuItem>
          )}
          {viewer.authenticated && !viewer.serviceAdmin && (
            <DropdownMenuItem asChild>
              <Link href="/account">
                <UserRoundCog />
                Your settings
              </Link>
            </DropdownMenuItem>
          )}
          <DropdownMenuItem asChild>
            <a href="/docs">
              <CircleHelp />
              Help
            </a>
          </DropdownMenuItem>
          {viewer.authenticated ? (
            <DropdownMenuItem
              disabled={signingOut}
              onSelect={(event) => {
                event.preventDefault();
                void signOut();
              }}
            >
              <LogOut />
              {signingOut ? "Signing out…" : "Sign out"}
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem asChild>
              <a
                href={`/ui/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/ui/home/" : window.location.pathname + window.location.search)}`}
              >
                <LogOut />
                Sign in
              </a>
            </DropdownMenuItem>
          )}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
