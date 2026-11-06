"use client";

import { MoreHorizontal, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { RelativeTime } from "@/components/product/relative-time";
import type { AccountApp } from "@/lib/account-api";
import type { Credential } from "@/lib/api";
import { keyStart } from "@/lib/key-hint";
import type { TrustedAddress } from "@/lib/trusted-addresses";

/*
 * What an app owns, shown inside its group on the Apps page: its keys, and for an owner its keyless addresses.
 * Both sit under the app's name, indented to line up with it, so each app reads as one block.
 */

/** The app name's left edge: the 16 px pad, the 16 px icon and its 12 px gap. Child lines start here. */
const childIndent = "pl-11";
/** The band that sets a child line apart from the app's header. */
const childBand = "border-t bg-muted/30";

/** A separator between the parts of one line. */
function Dot() {
  return (
    <span aria-hidden="true" className="text-muted-foreground">
      ·
    </span>
  );
}

/**
 * The app's keys, one row each, then a footer that adds a key. An app with no
 * keys shows only the footer; its header's meta line already says "0 keys".
 */
export function AppKeyRows({
  app,
  appName,
  busy,
  blockedReason,
  onAddKey,
  onRotate,
  onRevoke,
}: {
  app: AccountApp;
  appName: string;
  busy: boolean;
  /** Why this app cannot get a key, when it cannot. */
  blockedReason: string | null;
  /** Opens the key flow. The opener gets focus back when it closes. */
  onAddKey: (opener: HTMLElement) => void;
  onRotate: (key: Credential) => void;
  onRevoke: (key: Credential) => void;
}) {
  const noKeys = app.credentials.length === 0;
  return (
    <div className={`${childBand} divide-y`}>
      {noKeys ? null : (
        <ul aria-label={`Keys for ${appName}`} className="divide-y">
          {app.credentials.map((key) => (
            <KeyRow
              key={key.id}
              app={app}
              credential={key}
              busy={busy}
              onRotate={onRotate}
              onRevoke={onRevoke}
            />
          ))}
        </ul>
      )}
      <div
        className={`${childIndent} text-meta flex flex-wrap items-center gap-x-3 gap-y-1 py-2 pr-4`}
      >
        {/* The -ml-3 lines the icon up with the key names above it, with or without keys. */}
        <Button
          variant="ghost"
          size="sm"
          className="-ml-3 text-muted-foreground"
          disabled={busy || !app.can_mint_keys}
          aria-label={`Add key to ${appName}`}
          onClick={(event) => onAddKey(event.currentTarget)}
        >
          <Plus aria-hidden="true" />
          Add key
        </Button>
        {blockedReason ? (
          <span className="text-muted-foreground">{blockedReason}</span>
        ) : null}
      </div>
    </div>
  );
}

/**
 * One key: its name, its masked start, when it was made and when it was last
 * used. On a wide screen Rotate and Revoke sit beside it; on a phone they move
 * into one menu, with 44 px targets.
 */
function KeyRow({
  app,
  credential,
  busy,
  onRotate,
  onRevoke,
}: {
  app: AccountApp;
  credential: Credential;
  busy: boolean;
  onRotate: (key: Credential) => void;
  onRevoke: (key: Credential) => void;
}) {
  const rotateDisabled = busy || !app.can_mint_keys;
  return (
    <li
      className={`${childIndent} flex items-center justify-between gap-x-3 gap-y-1 py-2 pr-4`}
    >
      <div className="min-w-0">
        <p className="text-body truncate">{credential.display_name}</p>
        <p className="text-muted-foreground text-meta flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className="font-mono">{keyStart(credential.prefix)}</span>
          <Dot />
          <span>
            Created <RelativeTime ts={credential.created_at} />
          </span>
          <Dot />
          <span>
            {credential.last_used_at != null ? (
              <>
                Last used <RelativeTime ts={credential.last_used_at} />
              </>
            ) : (
              "Never used"
            )}
          </span>
        </p>
      </div>
      <div className="hidden shrink-0 items-center gap-1 sm:flex">
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground"
          disabled={rotateDisabled}
          aria-label={`Rotate key ${credential.display_name}`}
          onClick={() => onRotate(credential)}
        >
          Rotate
        </Button>
        {/* The ghost variant sets its own text colour, and cn does not merge classes, so the red is marked important. */}
        <Button
          variant="ghost"
          size="sm"
          className="text-destructive!"
          disabled={busy}
          aria-label={`Revoke key ${credential.display_name}`}
          onClick={() => onRevoke(credential)}
        >
          Revoke
        </Button>
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="sm:hidden"
            aria-label={`Options for key ${credential.display_name}`}
          >
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            disabled={rotateDisabled}
            onSelect={() => onRotate(credential)}
            className="max-sm:min-h-11"
          >
            Rotate key
          </DropdownMenuItem>
          <DropdownMenuItem
            variant="destructive"
            disabled={busy}
            onSelect={() => onRevoke(credential)}
            className="max-sm:min-h-11"
          >
            Revoke key
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

/**
 * An owner's keyless addresses for one app: the addresses a request may come
 * from to use the app with no key. The row shows only when there are some. The
 * hint that explains them goes under the first app that has the row.
 */
export function KeylessAccessRow({
  appName,
  addresses,
  failed,
  showHint,
}: {
  appName: string;
  addresses: TrustedAddress[];
  /** The list could not be read; the row says so rather than hiding. */
  failed: boolean;
  showHint: boolean;
}) {
  if (failed) {
    return (
      <p
        role="alert"
        className={`${childBand} ${childIndent} text-destructive text-meta py-2 pr-4`}
      >
        Keyless access couldn&apos;t be loaded.
      </p>
    );
  }
  if (addresses.length === 0) return null;
  return (
    <div
      role="group"
      aria-label={`Keyless access for ${appName}`}
      className={`${childBand} ${childIndent} text-meta py-2 pr-4`}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-muted-foreground">Keyless access from:</span>
        {addresses.map((entry) => (
          <AddressChip key={entry.id} entry={entry} />
        ))}
      </div>
      {showHint ? (
        <p className="text-muted-foreground mt-1">
          Requests from these addresses use this app&apos;s plan without a key.
        </p>
      ) : null}
    </div>
  );
}

function AddressChip({ entry }: { entry: TrustedAddress }) {
  return (
    <span className="bg-card inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5">
      <span className="font-mono">{entry.address}</span>
      {entry.cutover !== undefined ? (
        <span className="text-warning">Needs a check</span>
      ) : null}
    </span>
  );
}
