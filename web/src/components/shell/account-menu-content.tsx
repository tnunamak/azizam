"use client";

import Link from "next/link";
import { networkFetch } from "@/lib/network-error";
import { useEffect, useRef, useState } from "react";
import {
  ArrowLeftRight,
  ChevronDown,
  Braces,
  CircleHelp,
  CircleUserRound,
  LogOut,
  UserRoundCog,
} from "lucide-react";
import { changeBrowserPerson, endSession } from "@/lib/session-end";
import { unsavedWorkNow } from "@/lib/unsaved-work";
import {
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
import { AvatarPickerWithGeneration } from "./avatar-generation";
import { useViewer } from "./session";
import { SwitchConfirm, type LeaveRequest } from "./switch-confirm";

type BrowserPerson = { id: string; name: string; current: boolean; live: boolean; privileged: boolean };

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
  avatarPictureUrl = null,
  avatarSignInPictureUrl = null,
  avatarMadePictureUrl = null,
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
  /** The picture the avatar shows now (the sign-in picture, or the one the gateway made). */
  avatarPictureUrl?: string | null;
  avatarSignInPictureUrl?: string | null;
  avatarMadePictureUrl?: string | null;
  onAvatarChange?: (selection: AvatarSelection) => void;
  avatarPending?: boolean;
  avatarError?: string | null;
}) {
  const viewer = useViewer();
  const [people, setPeople] = useState<BrowserPerson[]>([]);
  const [peopleError, setPeopleError] = useState(false);
  useEffect(() => {
    if (!open || !viewer.authenticated) return;
    let cancelled = false;
    networkFetch("/auth/api/browser-people", { credentials: "same-origin", cache: "no-store" })
      .then((response) => { if (!response.ok) throw new Error("people unavailable"); return response.json(); })
      .then((body) => { if (!cancelled) { setPeople(body.people ?? []); setPeopleError(false); } })
      .catch(() => { if (!cancelled) setPeopleError(true); });
    return () => { cancelled = true; };
  }, [open, viewer.authenticated]);
  const [avatarPickerOpen, setAvatarPickerOpen] = useState(false);
  const [leaving, setLeaving] = useState<LeaveRequest | null>(null);
  const accountTriggerRef = useRef<HTMLButtonElement>(null);
  const avatarDialogRequested = useRef(false);
  const leaveRequested = useRef(false);
  const canChangeAvatar = Boolean(
    viewer.authenticated && !viewer.serviceAdmin && avatarPersonId && onAvatarChange,
  );

  /** Changing person and signing out confirm work that would be lost first (DESIGN.md 4.2). */
  function leave(end: LeaveRequest["end"], event: Event, proceed = () => { void endSession(end); }) {
    const lost = unsavedWorkNow();
    if (lost.length === 0) {
      event.preventDefault();
      proceed();
      return;
    }
    leaveRequested.current = true;
    setLeaving({ end, lost, opener: accountTriggerRef.current, proceed });
  }

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
              pictureUrl={avatarPictureUrl}
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
            if (avatarDialogRequested.current || leaveRequested.current) event.preventDefault();
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
            {viewer.authenticated && !viewer.serviceAdmin && (
              <DropdownMenuItem asChild>
                <Link prefetch={false} href="/account">
                  <UserRoundCog />
                  Your settings
                </Link>
              </DropdownMenuItem>
            )}
            <DropdownMenuItem asChild>
              <a href="https://azizam.dev/docs">
                <CircleHelp />
                Help
              </a>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <a href="/docs">
                <Braces />
                API reference
              </a>
            </DropdownMenuItem>
            {!viewer.authenticated && (
              <DropdownMenuItem asChild>
                {/* react-doctor-disable-next-line react-doctor/no-hydration-branch-on-browser-global -- AccountMenuContent mounts only after the hydrated placeholder opens it; focus and hover only preload its chunk. */}
                <a
                  href={`/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/" : window.location.pathname + window.location.search)}`}
                >
                  <LogOut />
                  Sign in
                </a>
              </DropdownMenuItem>
            )}
          </DropdownMenuGroup>
          {viewer.authenticated && (
            <>
              <DropdownMenuSeparator />
              {peopleError && <DropdownMenuLabel>Couldn’t load signed-in people. Open the menu to try again.</DropdownMenuLabel>}
              {people.map((person) => (
                <DropdownMenuGroup key={person.id}>
                  <DropdownMenuItem
                    disabled={person.current}
                    onSelect={(event) => leave("switch", event, () => { void changeBrowserPerson("switch", person.id); })}
                    className={person.live ? "" : "text-muted-foreground"}
                  >
                    <span><span className="block">{person.name}{person.current ? " · Current" : ""}</span>
                    {!person.live && <span className="block text-xs">Sign in again</span>}
                    {person.privileged && <span className="block text-xs">Asks to sign in again</span>}</span>
                  </DropdownMenuItem>
                  <DropdownMenuItem onSelect={(event) => leave("sign-out", event, () => { void changeBrowserPerson("remove", person.id); })}>Remove {person.name}</DropdownMenuItem>
                </DropdownMenuGroup>
              ))}
              <DropdownMenuItem onSelect={(event) => leave("switch", event)}>
                <ArrowLeftRight />
                Add another person
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={(event) => leave("sign-out", event)}>
                <LogOut />
                Sign out
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={(event) => leave("sign-out", event, () => { void changeBrowserPerson("all"); })}>
                Sign out of everyone on this browser
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {canChangeAvatar && avatarPersonId && onAvatarChange && (
        <Dialog open={avatarPickerOpen} onOpenChange={setAvatarPickerOpen}>
          {/*
            Anchored at a fixed top offset, so the dialog does not jump between states. On a phone it is a bottom
            sheet: at most 85% of the screen, its body scrolls, and Done stays in view. The close button is Done.
          */}
          <DialogContent
            showCloseButton={false}
            className="grid-rows-[auto_minmax(0,1fr)_auto] max-h-[85dvh] top-24! translate-y-0! sm:max-w-sm max-sm:top-auto! max-sm:bottom-0! max-sm:left-0! max-sm:right-0! max-sm:w-full! max-sm:max-w-none! max-sm:translate-x-0! max-sm:rounded-b-none!"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              avatarDialogRequested.current = false;
              accountTriggerRef.current?.focus();
            }}
          >
            <DialogHeader className="text-left!">
              <DialogTitle>Your avatar</DialogTitle>
              <DialogDescription>
                Choose how you appear to other people.
              </DialogDescription>
            </DialogHeader>
            <div className="min-h-0 space-y-4 overflow-y-auto">
              <div className="flex justify-center">
                <PersonAvatar
                  personId={avatarPersonId}
                  name={viewer.name}
                  selection={avatarSelection}
                  pictureUrl={avatarPictureUrl}
                  size="xl"
                />
              </div>
              <AvatarPickerWithGeneration
                personId={avatarPersonId}
                name={viewer.name}
                value={avatarSelection}
                signInPictureUrl={avatarSignInPictureUrl}
                madePictureUrl={avatarMadePictureUrl}
                onChange={onAvatarChange}
                pending={avatarPending}
                error={avatarError}
              />
            </div>
            <DialogFooter>
              <DialogClose asChild>
                <Button size="md">Done</Button>
              </DialogClose>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      <SwitchConfirm
        request={leaving}
        onClose={() => {
          leaveRequested.current = false;
          setLeaving(null);
        }}
      />
    </>
  );
}
