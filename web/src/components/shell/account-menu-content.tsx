"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import {
  ChevronDown,
  CircleHelp,
  CircleUserRound,
  KeyRound,
  LogOut,
  UserRoundCog,
} from "lucide-react";
import { endSession } from "@/lib/session-end";
import {
  AvatarPicker,
  PersonAvatar,
  type AvatarSelection,
} from "@/components/product/person-avatar";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
  avatarPersonId = null,
  avatarSelection,
  signInPictureUrl,
  onAvatarChange,
  avatarPending = false,
  avatarError,
}: {
  compact?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Wire the stable application person ID when the session API exposes it. */
  avatarPersonId?: string | null;
  avatarSelection?: AvatarSelection | null;
  signInPictureUrl?: string | null;
  onAvatarChange?: (selection: AvatarSelection) => void;
  avatarPending?: boolean;
  avatarError?: string | null;
}) {
  const viewer = useViewer();
  const [avatarPickerOpen, setAvatarPickerOpen] = useState(false);
  const accountTriggerRef = useRef<HTMLButtonElement>(null);
  const avatarDialogRequested = useRef(false);
  const canChangeAvatar = Boolean(
    viewer.authenticated && !viewer.serviceAdmin && avatarPersonId && onAvatarChange,
  );

  return (
    <>
      <DropdownMenu open={open} onOpenChange={onOpenChange}>
        <DropdownMenuTrigger asChild>
          <button
            ref={accountTriggerRef}
            type="button"
            className={
              compact
                ? "rounded-control flex size-11 items-center justify-center"
                : "shell-nav-footer w-full justify-center px-2 lg:justify-start lg:px-3"
            }
            aria-label={`Account menu: ${viewer.name}`}
          >
            <PersonAvatar
              personId={avatarPersonId}
              name={viewer.name}
              selection={avatarSelection}
              pictureUrl={signInPictureUrl}
              size={compact ? "md" : "sm"}
              decorative
            />
            {!compact && (
              <span className="shell-nav-label text-chrome-foreground min-w-0 flex-1 truncate text-left font-medium">
                {viewer.name}
              </span>
            )}
            {!compact && <ChevronDown className="shell-nav-label size-4" />}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          onCloseAutoFocus={(event) => {
            if (avatarDialogRequested.current) event.preventDefault();
          }}
        >
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
            {canChangeAvatar && (
              <DropdownMenuItem
                onSelect={() => {
                  avatarDialogRequested.current = true;
                  setAvatarPickerOpen(true);
                }}
              >
                <CircleUserRound />
                Change avatar
              </DropdownMenuItem>
            )}
            {!viewer.serviceAdmin && (
              <DropdownMenuItem asChild>
                <Link prefetch={false} href="/apps">
                  <KeyRound />
                  Apps & keys
                </Link>
              </DropdownMenuItem>
            )}
            {viewer.authenticated && !viewer.serviceAdmin && (
              <DropdownMenuItem asChild>
                <Link prefetch={false} href="/account">
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
                onSelect={(event) => {
                  event.preventDefault();
                  void endSession("sign-out");
                }}
              >
                <LogOut />
                Sign out
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem asChild>
                <a
                  href={`/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/" : window.location.pathname + window.location.search)}`}
                >
                  <LogOut />
                  Sign in
                </a>
              </DropdownMenuItem>
            )}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      {canChangeAvatar && avatarPersonId && onAvatarChange && (
        <Dialog open={avatarPickerOpen} onOpenChange={setAvatarPickerOpen}>
          <DialogContent
            className="sm:max-w-sm"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              avatarDialogRequested.current = false;
              accountTriggerRef.current?.focus();
            }}
          >
            <DialogHeader>
              <DialogTitle>Your avatar</DialogTitle>
              <DialogDescription>
                Choose how you appear to other people.
              </DialogDescription>
            </DialogHeader>
            <div className="flex justify-center">
              <PersonAvatar
                personId={avatarPersonId}
                name={viewer.name}
                selection={avatarSelection}
                pictureUrl={signInPictureUrl}
                size="xl"
              />
            </div>
            <AvatarPicker
              personId={avatarPersonId}
              name={viewer.name}
              value={avatarSelection}
              pictureUrl={signInPictureUrl}
              onChange={onAvatarChange}
              pending={avatarPending}
              error={avatarError}
            />
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline" size="md">
                  Done
                </Button>
              </DialogClose>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}
