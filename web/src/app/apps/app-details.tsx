"use client";

import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RelativeTime } from "@/components/product/relative-time";
import type { AccountApp } from "@/lib/account-api";
import type { Credential } from "@/lib/api";
import { keyStart } from "./rename";
import {
  listTrustedAddresses,
  trustedAddressesKey,
  type TrustedAddress,
} from "./trusted-addresses";

/*
 * What an app owns, shown inside its group on the Apps page: its keys, and for an owner its trusted addresses.
 * Both sit under the app's name, indented to line up with it, so each app reads as one block.
 */

/** The app name's left edge: the 16 px pad, the 16 px icon and its 12 px gap. Child lines start here. */
const childIndent = "pl-11";
/** The band that sets a child line apart from the app's header. */
const childBand = "border-t bg-muted/30";

/** The app's keys, one quiet row each: "Key · name", the masked start, and when it was last used. */
export function AppKeyRows({
  app,
  appName,
  busy,
  onRotate,
  onRevoke,
}: {
  app: AccountApp;
  appName: string;
  busy: boolean;
  onRotate: (key: Credential) => void;
  onRevoke: (key: Credential) => void;
}) {
  if (!app.credentials.length) {
    return (
      <p className={`${childBand} ${childIndent} text-muted-foreground text-meta py-2 pr-4`}>
        No keys yet
      </p>
    );
  }
  return (
    <ul aria-label={`Keys for ${appName}`} className={`${childBand} divide-y`}>
      {app.credentials.map((key) => (
        <li
          key={key.id}
          className={`${childIndent} flex flex-wrap items-center justify-between gap-x-3 gap-y-1 py-2 pr-4`}
        >
          <div className="min-w-0">
            <p className="text-body truncate">{`Key · ${key.display_name}`}</p>
            <p className="text-muted-foreground text-meta">
              <span className="font-mono">{keyStart(key.prefix)}</span>
              {" · "}
              {key.last_used_at != null ? (
                <>
                  Last used <RelativeTime ts={key.last_used_at} />
                </>
              ) : (
                "Never used"
              )}
            </p>
          </div>
          {/* On a phone, when the buttons wrap under the key, their text lines up with the key's name, not inside it. */}
          <div className="flex shrink-0 items-center gap-1 max-sm:-ml-3">
            <Button
              variant="ghost"
              size="sm"
              className="text-muted-foreground"
              disabled={busy || !app.can_mint_keys}
              aria-label={`Rotate key ${key.display_name}`}
              onClick={() => onRotate(key)}
            >
              Rotate
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="text-muted-foreground"
              disabled={busy}
              aria-label={`Revoke key ${key.display_name}`}
              onClick={() => onRevoke(key)}
            >
              Revoke
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );
}

/**
 * An owner's trusted addresses on one line under the app's keys: the addresses, or "none", then Manage. The
 * explanation is not repeated here; the page's About text and the Manage dialog give it. A request from one of
 * these addresses, with no key, uses this app's access plan.
 */
export function AppTrustedLine({
  app,
  appName,
  onManage,
}: {
  app: AccountApp;
  appName: string;
  onManage: () => void;
}) {
  const { data, isPending, isError } = useQuery({
    queryKey: trustedAddressesKey(app.id),
    queryFn: () => listTrustedAddresses(app.id),
  });
  return (
    <div
      className={`${childBand} ${childIndent} text-meta flex flex-wrap items-center gap-x-2 gap-y-1 py-2 pr-4`}
    >
      <span className="text-muted-foreground">Trusted addresses:</span>
      {isPending ? (
        <Skeleton className="h-5 w-28" />
      ) : isError ? (
        <span role="alert" className="text-destructive">
          Could not be loaded.
        </span>
      ) : data.length ? (
        data.map((entry) => <AddressChip key={entry.id} entry={entry} />)
      ) : (
        <span>none</span>
      )}
      {/* The separator and Manage wrap together, so a line never ends on a dangling dot. */}
      <span className="inline-flex items-center gap-2 whitespace-nowrap">
        <span aria-hidden="true" className="text-muted-foreground">
          ·
        </span>
        <Button
          variant="link"
          size="sm"
          aria-label={`Manage trusted addresses for ${appName}`}
          onClick={onManage}
        >
          Manage
        </Button>
      </span>
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
